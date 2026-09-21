from datetime import datetime, timedelta, timezone
import json
import re
import csv
import os
import sqlalchemy
from google.cloud import monitoring_v3
from google.cloud.sql.connector import Connector, IPTypes
from googleapiclient import discovery
import pg8000
import pymysql
import pymysql.cursors

# ─────────────────────────────────────────────
# TIGHTLY BOUND INTERNAL QUERIES
# ─────────────────────────────────────────────

INTERNAL_QUERIES = {
    "mysql": {
        "uptime": "SHOW GLOBAL STATUS LIKE 'Uptime';"
    },
    "postgres": {
        "uptime": "SELECT pg_postmaster_start_time() AS start_time;"
    },
}

# ─────────────────────────────────────────────
# TIER MAP & METRIC GROUPS
# ─────────────────────────────────────────────

TIER_MAP = {
    "db-f1-micro": (1, "0.614 GB"),
    "db-g1-small": (1, "1.7 GB"),
}

COMMON_METRICS = {
    "cpu_utilization": "cloudsql.googleapis.com/database/cpu/utilization",
    "memory_utilization": "cloudsql.googleapis.com/database/memory/utilization",
    "disk_utilization": "cloudsql.googleapis.com/database/disk/utilization",
    "disk_read_ops": "cloudsql.googleapis.com/database/disk/read_ops_count",
    "disk_write_ops": "cloudsql.googleapis.com/database/disk/write_ops_count",
    "disk_bytes_used": "cloudsql.googleapis.com/database/disk/bytes_used",
}

MYSQL_METRICS = {
    "connections": "cloudsql.googleapis.com/database/network/connections",
}

POSTGRES_METRICS = {
    "connections": "cloudsql.googleapis.com/database/postgresql/num_backends",
}


def get_metrics_for_engine(db_type: str) -> dict:
    """Combines common metrics with engine-specific metric types."""
    metrics = COMMON_METRICS.copy()
    
    if db_type == "mysql":
        metrics.update(MYSQL_METRICS)
    elif db_type == "postgres":
        metrics.update(POSTGRES_METRICS)
        
    return metrics


# ─────────────────────────────────────────────
# HELPER: FORMAT UPTIME
# ─────────────────────────────────────────────

def format_uptime(uptime_raw: list, db_type: str) -> str:
    """Converts raw uptime query outputs to human-readable strings."""
    if not uptime_raw or not isinstance(uptime_raw, list) or len(uptime_raw) == 0:
        return "N/A"
    
    row = uptime_raw[0]
    db_type_lower = db_type.lower()
    uptime_seconds = None
    
    try:
        if db_type_lower == "mysql":
            val = row.get("Value") or row.get("value")
            if val is not None:
                uptime_seconds = float(val)
                
        elif db_type_lower == "postgres":
            start_time = row.get("start_time")
            if start_time:
                # Handle string timestamps
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                
                # Compare against current timezone-aware UTC time
                if getattr(start_time, "tzinfo", None):
                    now = datetime.now(timezone.utc)
                else:
                    now = datetime.now()
                    
                uptime_seconds = (now - start_time).total_seconds()
                
        # Convert validated seconds into readable output
        if uptime_seconds is not None and uptime_seconds >= 0:
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            
            parts = []
            if days > 0:
                parts.append(f"{days} days")
            if hours > 0:
                parts.append(f"{hours} hours")
            parts.append(f"{minutes} mins")
            
            if parts:
                return ", ".join(parts)
            else:
                return "0 mins"
                
    except Exception as e:
        print(f"      [!] Uptime parse error: {e}")

    return "N/A"


# ─────────────────────────────────────────────
# INFRASTRUCTURE DETAILS
# ─────────────────────────────────────────────

def parse_compute_specs(tier: str) -> tuple:
    if not tier:
        return ("N/A", "N/A")
        
    if tier in TIER_MAP:
        return (str(TIER_MAP[tier][0]), TIER_MAP[tier][1])
        
    custom_match = re.search(r"-(\d+)-(\d+)$", tier)
    if custom_match:
        memory_gb = round(int(custom_match.group(2)) / 1024, 2)
        return (custom_match.group(1), f"{memory_gb} GB")
        
    suffix_match = re.search(r"-(\d+)$", tier)
    if suffix_match:
        return (suffix_match.group(1), "N/A")
        
    return ("N/A", "N/A")


def format_timestamp(ts: str) -> str:
    if not ts or ts == "No backups found":
        return ts
        
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return ts


def get_instance_details(service, project_id: str, instance_name: str) -> tuple:
    try:
        inst = service.instances().get(project=project_id, instance=instance_name).execute()
    except Exception as e:
        return ({"Error": f"Failed to fetch instance details: {str(e)}"}, None, None, None, None, None)

    settings = inst.get("settings", {})
    vcpu, memory = parse_compute_specs(settings.get("tier"))
    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    db_version = inst.get("databaseVersion", "UNKNOWN")

    # Determine engine type uniformly
    if "MYSQL" in db_version:
        db_type = "mysql"
    elif "POSTGRES" in db_version:
        db_type = "postgres"
    else:
        db_type = "sql_server"

    connection_name = inst.get("connectionName")
    region = inst.get("region")

    # Find the best IP address (prefer PRIVATE)
    ip_addresses = inst.get("ipAddresses", [])
    host = None
    
    for ip in ip_addresses:
        if ip.get("type") == "PRIVATE":
            host = ip.get("ipAddress")
            break
            
    # Fallback to the first available IP if no private IP exists
    if not host and ip_addresses:
        host = ip_addresses[0].get("ipAddress")

    # Set default port uniformly
    if db_type == "postgres":
        port = 5432
    elif db_type == "mysql":
        port = 3306
    else:
        port = 1433

    # Extract latest backup time
    last_backup_time = "No backups found"
    try:
        request = service.backupRuns().list(project=project_id, instance=instance_name, maxResults=1)
        response = request.execute()
        backups = response.get("items", [])
        
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = format_timestamp(backups[0]["windowStartTime"])
    except Exception:
        pass

    specs = {
        "Engine": db_version.replace("_", " "),
        "Edition": settings.get("edition", "ENTERPRISE"),
        "CPU": vcpu,
        "Memory": memory,
        "Storage": f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip(),
        "Availability": settings.get("availabilityType", "N/A").title(),
        "Read Replica count": len(inst.get("replicaNames", [])),
        "Last Backup time": last_backup_time,
    }

    return specs, connection_name, db_type, region, host, port


def extract_typed_value(typed_value):
    val_type = typed_value._pb.WhichOneof("value")
    
    if val_type == "double_value":
        return typed_value.double_value
    if val_type == "int64_value":
        return float(typed_value.int64_value)
        
    return 0.0


def fetch_mql_metric(client, project_id, instance_id, metric_key, metric_type):
    metric_suffix = metric_type.split('/')[-1]
    value_col = f"value.{metric_suffix}"

    mql_aggregated = f"""
        fetch cloudsql_database 
        | metric '{metric_type}' 
        | filter (resource.database_id == '{project_id}:{instance_id}')
        | within 24h 
        | group_by [], [
            mean_val: mean({value_col}), 
            p95: percentile({value_col}, 95), 
            p99: percentile({value_col}, 99)
        ] 
        | every 24h
    """
    
    mql_max = f"""
        fetch cloudsql_database 
        | metric '{metric_type}' 
        | filter (resource.database_id == '{project_id}:{instance_id}')
        | within 24h 
        | group_by [], [
            max_val: max({value_col})
        ] 
        | every 1m
    """

    result = {"mean": None, "max": None, "p95": None, "p99": None}

    # Execute Aggregated Query (Mean, P95, P99)
    try:
        agg_request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", 
            query=mql_aggregated
        )
        response = client.query_time_series(request=agg_request)
        
        for series_data in response:
            if series_data.point_data:
                point = series_data.point_data[0]
                mean_val = extract_typed_value(point.values[0])
                p95_val = extract_typed_value(point.values[1])
                p99_val = extract_typed_value(point.values[2])
                
                if "utilization" in metric_key:
                    mean_val *= 100
                    p95_val *= 100
                    p99_val *= 100
                    
                result["mean"] = round(mean_val, 2)
                result["p95"] = round(p95_val, 2)
                result["p99"] = round(p99_val, 2)
    except Exception:
        pass

    # Execute Max Query
    try:
        max_request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", 
            query=mql_max
        )
        response = client.query_time_series(request=max_request)
        true_max = None
        
        for series_data in response:
            for point in series_data.point_data:
                val = extract_typed_value(point.values[0])
                
                if "utilization" in metric_key:
                    val *= 100
                    
                if true_max is None or val > true_max:
                    true_max = val
                    
        if true_max is not None:
            result["max"] = round(true_max, 2)
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────
# DATABASE ENGINES
# ─────────────────────────────────────────────

def get_iam_engine(target, connector):
    db_type = target.get("db_type").lower()
    instance_name = f"{target['project_id']}:{target['region']}:{target['instance']}"
    
    # Engine Check Standardized to 'postgres'
    if db_type == "postgres":
        driver = "pg8000"
        dialect = "postgresql+pg8000://"
    else:
        driver = "pymysql"
        dialect = "mysql+pymysql://"
  
    def _getconn():
        return connector.connect(
            instance_name, 
            driver, 
            user=target["user"], 
            db=target["database"], 
            enable_iam_auth=True
        )

    return sqlalchemy.create_engine(dialect, creator=_getconn, pool_pre_ping=True)


def get_native_engine(target):
    db_type = target["db_type"].lower()
    user = target["user"]
    password = target["password"]
    host = target["host"]
    port = target["port"]
    db = target["database"]
    
    # Engine Check Standardized to 'postgres'
    if db_type == "postgres":
        url = f"postgresql+pg8000://{user}:{password}@{host}:{port}/{db}"
    else:
        url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}"
        
    return sqlalchemy.create_engine(url, pool_pre_ping=True)


# ─────────────────────────────────────────────
# UNIFIED QUERY RUNNER
# ─────────────────────────────────────────────

def run_queries(engine, db_type: str, user_queries: dict, internal_queries: dict) -> tuple:
    health_checks = {}
    internal_results = {}
    
    db_user_queries = user_queries.get(db_type, {})
    db_internal_queries = internal_queries.get(db_type, {})

    with engine.connect() as conn:
        # 1. Execute Dynamic Checks from queries.json
        for category, category_queries in db_user_queries.items():
            health_checks[category] = {}
            for key, sql in category_queries.items():
                if key.startswith("_"):
                    continue
                    
                try:
                    with conn.begin_nested():
                        result = conn.execute(sqlalchemy.text(sql))
                        if result.returns_rows:
                            health_checks[category][key] = [dict(row._mapping) for row in result.fetchall()]
                        else:
                            health_checks[category][key] = []
                except Exception as e:
                    print(f"    Query failed [{category} -> {key}]: {e}")
                    health_checks[category][key] = [{"error": str(e), "status": "FAILED"}]

        # 2. Execute Hardcoded Internal Queries within the same connection block
        for key, sql in db_internal_queries.items():
            try:
                with conn.begin_nested():
                    result = conn.execute(sqlalchemy.text(sql))
                    if result.returns_rows:
                        internal_results[key] = [dict(row._mapping) for row in result.fetchall()]
                    else:
                        internal_results[key] = []
            except Exception as e:
                print(f"    Internal Query failed [{key}]: {e}")
                internal_results[key] = [{"error": str(e), "status": "FAILED"}]

    return health_checks, internal_results


# ─────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────

def main():
    # Load dynamic JSON queries
    with open("queries.json", "r") as f:
        all_queries = json.load(f)
      
    sqladmin = discovery.build("sqladmin", "v1", cache_discovery=False)
    mon_client = monitoring_v3.QueryServiceClient()
    report = {}

    # Define reporting time window (Last 24 hours)
    report_end = datetime.now(timezone.utc)
    report_start = report_end - timedelta(hours=24)
    report_window = {
        "from": report_start.strftime("%d %b %Y, %I:%M %p UTC"),
        "to": report_end.strftime("%d %b %Y, %I:%M %p UTC"),
    }

    # Initialize Cloud SQL Connector and iterate through CSV targets
    with Connector(refresh_strategy="LAZY") as connector:
        with open("config.csv", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            
            for row in reader:
                project_id = row.get("project_id", "").strip()
                instance_name = row.get("instance_name", "").strip()
                
                if not project_id or not instance_name:
                    continue

                print(f"\nInstance: {instance_name} (project: {project_id})")
                
                # Initialize instance payload
                report[instance_name] = {
                    "report_window": report_window, 
                    "provisioned_specs": {}, 
                    "resource_utilization": {}, 
                    "health_checks": {}
                }

                # Step 1: Fetch Instance Settings
                specs, conn_name, db_type, region, host, port = get_instance_details(sqladmin, project_id, instance_name)
                
                if "Error" in specs or not conn_name:
                    report[instance_name]["provisioned_specs"] = specs
                    print("  Skipping: Invalid specs or connection name.")
                    continue

                report[instance_name]["provisioned_specs"] = specs
                print(f"  Engine: {db_type} | Connection: {conn_name}")

                # Step 2: Fetch Cloud Monitoring metrics
                print("  Fetching Cloud Monitoring metrics...")
                engine_metrics = get_metrics_for_engine(db_type)
                for m_key, m_type in engine_metrics.items():
                    report[instance_name]["resource_utilization"][m_key] = fetch_mql_metric(
                        mon_client, project_id, instance_name, m_key, m_type
                    )

                # Step 3: Establish Database Connection and Execute Queries
                try:
                    auth_type = row.get("auth_type", "native").strip().lower()
                    
                    # Target Database defaults to "postgres" for Postgres engines, else "mysql"
                    target_database = "postgres" if db_type == "postgres" else "mysql"
                    
                    if auth_type == "iam":
                        target_config = {
                            "project_id": project_id,
                            "region": region,
                            "instance": instance_name,
                            "db_type": db_type,
                            "user": row.get("db_user", "").strip(),
                            "database": target_database
                        }
                        engine = get_iam_engine(target_config, connector)
                    else:
                        target_config = {
                            "db_type": db_type,
                            "user": row.get("db_user", "").strip(),
                            "password": row.get("db_pass", "").strip(),
                            "host": host,
                            "port": port,
                            "database": target_database
                        }
                        engine = get_native_engine(target_config)

                    print("  Executing Database Audits & Internal Queries...")
                    
                    # Unpack tuple returned by unified query runner
                    health_checks, internal_results = run_queries(
                        engine, db_type, all_queries, INTERNAL_QUERIES
                    )

                    # Format and assign tightly-bound results
                    report[instance_name]["health_checks"] = health_checks
                    report[instance_name]["provisioned_specs"]["Uptime"] = format_uptime(
                        internal_results.get("uptime"), db_type
                    )
                    
                    print("    Done.")

                except Exception as e:
                    print(f"    Failed: {e}")
                    report[instance_name]["health_checks"] = {"error": str(e)}

    # Save output to disk
    with open("database_health_report.json", "w") as f:
        json.dump(report, f, indent=4, default=str)
        
    print("\nReport saved to database_health_report.json")

if __name__ == "__main__":
    main()