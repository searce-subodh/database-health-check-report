import json
import re
import csv
import os
from datetime import datetime, timezone
import google.auth
from googleapiclient import discovery
from google.cloud import monitoring_v3

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
    if not tier:
        return ("N/A", "N/A")
        
    if tier in TIER_MAP:
        vcpu, mem = TIER_MAP[tier]
        return (str(vcpu), mem)

    custom_match = re.search(r"-(\d+)-(\d+)$", tier)
    if custom_match:
        vcpu = custom_match.group(1)
        memory_mb = int(custom_match.group(2))
        memory_gb = round(memory_mb / 1024, 2)
        return (vcpu, f"{memory_gb} GB")

    suffix_match = re.search(r"-(\d+)$", tier)
    if suffix_match:
        return (suffix_match.group(1), "N/A")

    return ("N/A", "N/A")

def get_cloudsql_summary(service, project_id: str, instance_name: str) -> dict:
    try:
        req = service.instances().get(project=project_id, instance=instance_name)
        inst = req.execute()
    except Exception as e:
        return {"Error": f"Failed to fetch instance details: {str(e)}"}

    settings = inst.get("settings", {})

    vcpu, memory = parse_compute_specs(settings.get("tier"))

    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    storage_str = f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip()

    availability = settings.get("availabilityType", "N/A").title()

    replica_count = len(inst.get("replicaNames", []))

    last_backup_time = "No backups found"
    try:
        backup_req = service.backupRuns().list(
            project=project_id, 
            instance=instance_name, 
            maxResults=1
        )
        backups = backup_req.execute().get("items", [])
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = backups[0]["windowStartTime"]
    except Exception:
        pass

    return {
        "Engine": inst.get("databaseVersion", "N/A").replace("_", " "),
        "Edition": settings.get("edition", "ENTERPRISE"),
        "CPU": vcpu,
        "Memory": memory,
        "Storage": storage_str,
        "Availability": availability,
        "Read Replica count": replica_count,
        "Last Backup time": last_backup_time,
    }


def extract_typed_value(typed_value):
    val_type = typed_value._pb.WhichOneof("value")
    if val_type == "double_value":
        return typed_value.double_value
    elif val_type == "int64_value":
        return float(typed_value.int64_value)
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
        mean_val: mean({value_col}), 
        max_val: max({value_col}), 
        p95: percentile({value_col}, 95), 
        p99: percentile({value_col}, 99)
      ] 
    | every 24h
    """

    request = monitoring_v3.QueryTimeSeriesRequest(
        name=f"projects/{project_id}",
        query=mql_query
    )

    try:
        response = client.query_time_series(request=request)
        for series_data in response:
            if not series_data.point_data:
                continue
            
            point = series_data.point_data[0]
            mean_val = extract_typed_value(point.values[0])
            max_val  = extract_typed_value(point.values[1])
            p95_val  = extract_typed_value(point.values[2])
            p99_val  = extract_typed_value(point.values[3])
            
            if "utilization" in metric_key:
                mean_val *= 100
                max_val  *= 100
                p95_val  *= 100
                p99_val  *= 100

            return {
                "mean": round(mean_val, 2),
                "max": round(max_val, 2),
                "p95": round(p95_val, 2),
                "p99": round(p99_val, 2)
            }
            
    except Exception as e:
        pass 

    return {"mean": None, "max": None, "p95": None, "p99": None}

def main():
    sqladmin_service = discovery.build('sqladmin', 'v1', cache_discovery=False)
    monitoring_client = monitoring_v3.QueryServiceClient()
    
    consolidated_output = {}
    config_file = 'config.csv'

    if not os.path.exists(config_file):
        print(f"Error: {config_file} not found in the current directory.")
        return

    with open(config_file, mode='r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        
        for row in reader:
            project_id = row.get('project_id', '').strip()
            inst = row.get('instance_name', '').strip()
            
            if not project_id or not inst:
                continue # Skip empty or malformed rows

            output_key = f"{project_id}:{inst}"

            static_details = get_cloudsql_summary(sqladmin_service, project_id, inst)
            
            metrics_data = {}
            for m_key, m_type in METRIC_TYPES.items():
                metrics_data[m_key] = fetch_mql_metric(monitoring_client, project_id, inst, m_key, m_type)

            consolidated_output[output_key] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "project_id": project_id,
                "instance_name": inst,
                "static_parameters": static_details,
                "metrics": metrics_data
            }

    print(json.dumps(consolidated_output, indent=2))

if __name__ == "__main__":
    main()