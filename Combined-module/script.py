import json
import re
import csv
import os
from datetime import datetime, timezone
from googleapiclient import discovery
from google.cloud import monitoring_v3
import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes

TIER_MAP = {
    "db-f1-micro": (1, "0.614 GB"),
    "db-g1-small": (1, "1.7 GB"),
}

METRIC_TYPES = {
    "cpu_utilization":   "cloudsql.googleapis.com/database/cpu/utilization",
    "memory_utilization": "cloudsql.googleapis.com/database/memory/utilization",
    "disk_utilization":   "cloudsql.googleapis.com/database/disk/utilization",
    "disk_read_ops":      "cloudsql.googleapis.com/database/disk/read_ops_count",
    "disk_write_ops":     "cloudsql.googleapis.com/database/disk/write_ops_count",
    "disk_bytes_used":    "cloudsql.googleapis.com/database/disk/bytes_used",
    "connections":        "cloudsql.googleapis.com/database/network/connections",
    "queries":            "cloudsql.googleapis.com/database/mysql/queries",
}

def parse_compute_specs(tier: str) -> tuple:
    if not tier: return ("N/A", "N/A")
    if tier in TIER_MAP: return (str(TIER_MAP[tier][0]), TIER_MAP[tier][1])
    custom_match = re.search(r"-(\d+)-(\d+)$", tier)
    if custom_match:
        memory_gb = round(int(custom_match.group(2)) / 1024, 2)
        return (custom_match.group(1), f"{memory_gb} GB")
    suffix_match = re.search(r"-(\d+)$", tier)
    return (suffix_match.group(1), "N/A") if suffix_match else ("N/A", "N/A")

def get_instance_details(service, project_id: str, instance_name: str) -> tuple:
    try:
        req = service.instances().get(project=project_id, instance=instance_name)
        inst = req.execute()
    except Exception as e:
        return {"Error": f"Failed to fetch instance details: {str(e)}"}, None, None

    settings = inst.get("settings", {})
    vcpu, memory = parse_compute_specs(settings.get("tier"))
    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    
    db_version = inst.get("databaseVersion", "UNKNOWN")
    db_type = "mysql" if "MYSQL" in db_version else "postgres" if "POSTGRES" in db_version else "unknown"
    connection_name = inst.get("connectionName")

    last_backup_time = "No backups found"
    try:
        backup_req = service.backupRuns().list(project=project_id, instance=instance_name, maxResults=1)
        backups = backup_req.execute().get("items", [])
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = backups[0]["windowStartTime"]
    except Exception:
        pass

    static_params = {
        "Engine": db_version.replace("_", " "),
        "Edition": settings.get("edition", "ENTERPRISE"),
        "CPU": vcpu,
        "Memory": memory,
        "Storage": f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip(),
        "Availability": settings.get("availabilityType", "N/A").title(),
        "Read Replica count": len(inst.get("replicaNames", [])),
        "Last Backup time": last_backup_time,
    }

    return static_params, connection_name, db_type

def extract_typed_value(typed_value):
    val_type = typed_value._pb.WhichOneof("value")
    if val_type == "double_value": return typed_value.double_value
    elif val_type == "int64_value": return float(typed_value.int64_value)
    return 0.0

def fetch_mql_metric(client, project_id, instance_id, metric_key, metric_type):
    metric_suffix = metric_type.split('/')[-1]
    value_col = f"value.{metric_suffix}"
    
    mql_query = f"""
    fetch cloudsql_database 
    | metric '{metric_type}' 
    | filter (resource.database_id == '{project_id}:{instance_id}') 
    | within 24h 
    | group_by [], [
        mean_val: mean({value_col}), max_val: max({value_col}), 
        p95: percentile({value_col}, 95), p99: percentile({value_col}, 99)
      ] 
    | every 24h
    """
    request = monitoring_v3.QueryTimeSeriesRequest(name=f"projects/{project_id}", query=mql_query)
    
    try:
        response = client.query_time_series(request=request)
        for series_data in response:
            if not series_data.point_data: continue
            point = series_data.point_data[0]
            vals = [extract_typed_value(point.values[i]) for i in range(4)]
            
            if "utilization" in metric_key:
                vals = [v * 100 for v in vals]

            return {"mean": round(vals[0], 2), "max": round(vals[1], 2), "p95": round(vals[2], 2), "p99": round(vals[3], 2)}
    except Exception:
        pass 
    return {"mean": None, "max": None, "p95": None, "p99": None}

def get_db_engine(connector: Connector, connection_name: str, db_type: str, db_user: str, db_pass: str) -> sqlalchemy.engine.base.Engine:
    conn_kwargs = {"user": db_user, "password": db_pass, "ip_type": IPTypes.PRIVATE}

    if db_type == "mysql":
        def getconn(): return connector.connect(connection_name, "pymysql", **conn_kwargs)
        db_url = "mysql+pymysql://"
    elif db_type == "postgres":
        conn_kwargs["db"] = "postgres"  # Default DB for postgres connections
        def getconn(): return connector.connect(connection_name, "pg8000", **conn_kwargs)
        db_url = "postgresql+pg8000://"
    else:
        raise ValueError(f"Unsupported dynamic database type: {db_type}")
        
    return sqlalchemy.create_engine(db_url, creator=getconn)

def main():
    config_file = 'config.csv'
    queries_file = 'queries.json'
    output_file = 'structured_health_report.json'

    if not os.path.exists(config_file) or not os.path.exists(queries_file):
        print("Error: config.csv or queries.json missing.")
        return

    with open(queries_file, "r") as f:
        all_database_queries = json.load(f)

    sqladmin_service = discovery.build('sqladmin', 'v1', cache_discovery=False)
    monitoring_client = monitoring_v3.QueryServiceClient()
    
    final_output = {}

    with Connector(refresh_strategy="LAZY") as connector:
        with open(config_file, mode='r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            
            for row in reader:
                project_id = row.get('project_id', '').strip()
                instance_name = row.get('instance_name', '').strip()
                db_user = row.get('db_user', '').strip()
                db_pass = row.get('db_pass', '').strip()
                
                if not project_id or not instance_name:
                    continue
                
                print(f"Processing: {instance_name}...")
                
                final_output[instance_name] = {
                    "provisioned_specs": {},
                    "resource_utilization": {},
                    "health_checks": {}
                }

                static_params, conn_name, db_type = get_instance_details(sqladmin_service, project_id, instance_name)
                final_output[instance_name]["provisioned_specs"] = static_params

                if "Error" in static_params or not conn_name:
                    print(f"  -> Skipping {instance_name}: Could not fetch instance details.")
                    continue

                metrics_data = {}
                for m_key, m_type in METRIC_TYPES.items():
                    metrics_data[m_key] = fetch_mql_metric(monitoring_client, project_id, instance_name, m_key, m_type)
                final_output[instance_name]["resource_utilization"] = metrics_data

                engine_queries = all_database_queries.get(db_type)
                if not engine_queries:
                    final_output[instance_name]["health_checks"]["error"] = f"No queries found for type: {db_type}"
                    continue

                try:
                    engine = get_db_engine(connector, conn_name, db_type, db_user, db_pass)
                    with engine.connect() as connection:
                        for category, queries in engine_queries.items():
                            final_output[instance_name]["health_checks"][category] = {}
                            for key, sql in queries.items():
                                try:
                                    result = connection.execute(sqlalchemy.text(sql))
                                    final_output[instance_name]["health_checks"][category][key] = [dict(r) for r in result.mappings()]
                                except Exception as query_err:
                                    final_output[instance_name]["health_checks"][category][key] = {"error": str(query_err)}
                except Exception as conn_err:
                    print(f"  -> DB Connection Failed: {conn_err}")
                    final_output[instance_name]["health_checks"]["connection_error"] = str(conn_err)
                finally:
                    if 'engine' in locals():
                        engine.dispose()

    with open(output_file, "w") as f:
        json.dump(final_output, f, indent=4, default=str)
    
    print(f"\nDone. Output structured and saved to {output_file}")

if __name__ == "__main__":
    main()