from datetime import datetime, timedelta, timezone
import json
import re
import os
import yaml
import sqlalchemy
from sqlalchemy.engine import URL
from google.cloud import monitoring_v3
from google.cloud.sql.connector import Connector, IPTypes
from googleapiclient import discovery
import pg8000
import pymysql
import pymysql.cursors



METRIC_LABELS = {
    "cpu_utilization": "CPU Utilization (%)",
    "memory_utilization": "Memory Utilization (%)",
    "disk_utilization": "Disk Utilization (%)",
    "disk_read_ops": "Disk Read (IOPS)",
    "disk_write_ops": "Disk Write (IOPS)",
    "disk_bytes_used": "Disk Bytes Used (GB)",
    "connections": "Connections (Count)",
}
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
# TIER MAP
# ─────────────────────────────────────────────

TIER_MAP = {
    "db-f1-micro": (1, "0.614 GB"),
    "db-g1-small": (1, "1.7 GB"),
}

# ─────────────────────────────────────────────
# CLOUD MONITORING METRIC GROUPS
# ─────────────────────────────────────────────

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


def get_metrics_for_engine(db_type: str, requested_metrics: list) -> dict:
    metrics = {}
    for metric in requested_metrics:
        if metric in COMMON_METRICS:
            metrics[metric] = COMMON_METRICS[metric]
        elif metric == "connections":
            if db_type == "mysql":
                metrics[metric] = MYSQL_METRICS["connections"]
            elif db_type == "postgres":
                metrics[metric] = POSTGRES_METRICS["connections"]
    return metrics


# ─────────────────────────────────────────────
# HELPER: FORMAT UPTIME
# ─────────────────────────────────────────────

def format_uptime(uptime_raw: list, db_type: str) -> str:
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
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                
                if getattr(start_time, "tzinfo", None):
                    now = datetime.now(timezone.utc)
                else:
                    start_time = start_time.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    
                uptime_seconds = (now - start_time).total_seconds()
                
        if uptime_seconds is not None and uptime_seconds >= 0:
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            
            parts = []
            if days > 0:
                parts.append(f"{days}d")
            if hours > 0:
                parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            
            return " ".join(parts) if parts else "0m"
                
    except Exception as e:
        print(f"      [!] Uptime parse error: {e}")

    return "N/A"


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
    return (suffix_match.group(1), "N/A") if suffix_match else ("N/A", "N/A")


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
        inst = (
            service.instances()
            .get(project=project_id, instance=instance_name)
            .execute()
        )
    except Exception as e:
        return ({"Error": f"Failed to fetch details: {str(e)}"}, None, None, None, None, None)

    settings = inst.get("settings", {})
    vcpu, memory = parse_compute_specs(settings.get("tier"))
    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    db_version = inst.get("databaseVersion", "UNKNOWN")

    db_type = "mysql" if "MYSQL" in db_version else "postgres" if "POSTGRES" in db_version else "sql_server"
    connection_name = inst.get("connectionName")
    region = inst.get("region")

    ip_addresses = inst.get("ipAddresses", [])
    host = next((ip.get("ipAddress") for ip in ip_addresses if ip.get("type") == "PRIVATE"), "")
    if not host and ip_addresses:
        host = ip_addresses[0].get("ipAddress")

    port = 5432 if db_type == "postgres" else 3306 if db_type == "mysql" else 1433

    last_backup_time = "No backups found"
    try:
        backups = (
            service.backupRuns()
            .list(project=project_id, instance=instance_name, maxResults=1)
            .execute()
            .get("items", [])
        )
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
        "Replicas": len(inst.get("replicaNames", [])),
        "Last Backup": last_backup_time,
    }

    return specs, connection_name, db_type, region, host, port


# ─────────────────────────────────────────────
# FETCH MONITORING METRICS (SCALED & 2 QUERIES)
# ─────────────────────────────────────────────

def extract_typed_value(typed_value):
    val_type = typed_value._pb.WhichOneof("value")
    if val_type == "double_value":
        return typed_value.double_value
    elif val_type == "int64_value":
        return float(typed_value.int64_value)
    return 0.0


def fetch_mql_metric(client, project_id, instance_id, metric_key, metric_type):
    metric_suffix = metric_type.split("/")[-1]
    value_col = f"value.{metric_suffix}"

    # Query 1 — Mean, P95, P99
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

    # Query 2 — True max via per-minute points
    mql_max = f"""
    fetch cloudsql_database
    | metric '{metric_type}'
    | filter (resource.database_id == '{project_id}:{instance_id}')
    | within 24h
    | group_by [], [max_val: max({value_col})]
    | every 1m
    """
    result = {"mean": None, "p95": None, "p99": None, "max": None}

    # Apply mathematical scaling based on metric type
    def scale_value(val):
        if val is None:
            return None
        if "utilization" in metric_key:
            return val * 100
        elif "bytes_used" in metric_key:
            return val / (1024 ** 3) # Convert Bytes to GB
        elif "ops" in metric_key:
            return val / 60 # Convert ops/min to IOPS (ops/sec)
        return val

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", query=mql_aggregated
        )
        response = client.query_time_series(request=request)
        for series_data in response:
            if not series_data.point_data:
                continue
            point = series_data.point_data[0]
            
            raw_mean = extract_typed_value(point.values[0]) if len(point.values) > 0 else 0.0
            raw_p95 = extract_typed_value(point.values[1]) if len(point.values) > 0 else 0.0
            raw_p99 = extract_typed_value(point.values[2]) if len(point.values) > 0 else 0.0
            
            result["mean"] = round(scale_value(raw_mean), 2)
            result["p95"] = round(scale_value(raw_p95), 2)
            result["p99"] = round(scale_value(raw_p99), 2)  
    except Exception as e:
        print(f"      [!] Aggregated metric query failed for '{metric_key}': {e}")

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", query=mql_max
        )
        response = client.query_time_series(request=request)
        true_max = None
        for series_data in response:
            for point in series_data.point_data:
                val = extract_typed_value(point.values[0])
                if true_max is None or val > true_max:
                    true_max = val
        if true_max is not None:
            result["max"] = round(scale_value(true_max), 2)
    except Exception as e:
        print(f"      [!] Max metric query failed for '{metric_key}': {e}")

    return result


# ─────────────────────────────────────────────
# DB AUTH ENGINES & QUERY RUNNER
# ─────────────────────────────────────────────

def get_iam_engine(target, connector):
    db_type = target.get("db_type").lower()
    instance_name = f"{target['project_id']}:{target['region']}:{target['instance']}"

    if db_type == "postgres":
        driver, dialect = "pg8000", "postgresql+pg8000://"
    elif db_type == "mysql":
        driver, dialect = "pymysql", "mysql+pymysql://"
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

    def _getconn():
        return connector.connect(
            instance_name, driver, user=target["user"], db=target["database"], enable_iam_auth=True
        )

    return sqlalchemy.create_engine(dialect, creator=_getconn, pool_pre_ping=True)


def get_native_engine(target):
    db_type = target.get("db_type", "").lower()
    drivername = "postgresql+pg8000" if db_type == "postgres" else "mysql+pymysql"
    
    url = URL.create(
        drivername=drivername,
        username=target.get("user", ""),
        password=target.get("password", ""),
        host=target.get("host", ""),
        port=target.get("port"),
        database=target.get("database")
    )
    return sqlalchemy.create_engine(url, pool_pre_ping=True)


def run_queries(engine, db_type: str, user_queries: dict, internal_queries: dict) -> tuple:
    health_checks = {}
    internal_results = {}
    
    db_user_queries = user_queries.get(db_type, {})
    db_internal_queries = internal_queries.get(db_type, {})

    with engine.connect() as conn:
        for category, category_queries in db_user_queries.items():
            health_checks[category] = {}
            for key, sql in category_queries.items():
                if key.startswith("_"): continue
                try:
                    with conn.begin_nested():
                        result = conn.execute(sqlalchemy.text(sql))
                        if result.returns_rows:
                            rows = result.fetchall()
                            health_checks[category][key] = [dict(row._mapping) for row in rows] if rows else [{"message": "0 records returned"}]
                        else:
                            health_checks[category][key] = [{"message": "Query executed successfully"}]
                except Exception as e:
                    print(f"    [!] Query failed [{category} -> {key}]: {e}")
                    health_checks[category][key] = [{"status": "ERROR", "message": "Result unavailable due to execution error"}]

        for key, sql in db_internal_queries.items():
            try:
                with conn.begin_nested():
                    result = conn.execute(sqlalchemy.text(sql))
                    if result.returns_rows:
                        rows = result.fetchall()
                        internal_results[key] = [dict(row._mapping) for row in rows] if rows else [{"message": "0 records returned"}]
                    else:
                        internal_results[key] = [{"message": "Query executed successfully"}]
            except Exception as e:
                print(f"    [!] Internal Query failed [{key}]: {e}")
                internal_results[key] = [{"status": "ERROR", "message": "Result unavailable"}]

    return health_checks, internal_results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("[START] Initializing Cloud SQL Health Check Script...")
    
    try:
        all_queries = json.load(open("queries.json"))
        print(" -> Successfully loaded 'queries.json'")
    except FileNotFoundError:
        print(" [!] Error: 'queries.json' not found. Exiting.")
        return

    try:
        with open("config.yaml", "r", encoding="utf-8") as yaml_file:
            config_data = yaml.safe_load(yaml_file)
            print(" -> Successfully loaded 'config.yaml'")
    except FileNotFoundError:
        print("\n[!] Error: 'config.yaml' not found. Exiting.")
        return
    except yaml.YAMLError as exc:
        print(f"\n[!] Error parsing 'config.yaml': {exc}")
        return

    requested_metrics = config_data.get("monitoring_metrics", [])
    instances = config_data.get("instances", [])

    if not instances:
        print(" [!] No instances found in config.yaml. Exiting.")
        return

    # Create the reports directory if it doesn't exist
    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    print(f" -> Output directory '{reports_dir}/' is ready.")

    print(" -> Building Google Cloud APIs (SQL Admin & Monitoring)...")
    sqladmin = discovery.build("sqladmin", "v1", cache_discovery=False)
    mon_client = monitoring_v3.QueryServiceClient()

    report_end = datetime.now(timezone.utc)
    report_start = report_end - timedelta(hours=24)
    report_window = {
        "from": report_start.strftime("%d %b %Y, %I:%M %p UTC"),
        "to": report_end.strftime("%d %b %Y, %I:%M %p UTC"),
    }

    with Connector(refresh_strategy="LAZY") as connector:
        print(" -> Processing instances from 'config.yaml'...")
        for row in instances:
            project_id = row.get("project_id", "").strip()
            instance_name = row.get("instance_name", "").strip()
            db_user = row.get("db_user", "").strip()
            db_pass = row.get("db_pass", "")
            if isinstance(db_pass, str): db_pass = db_pass.strip()
            auth_type = row.get("auth_type", "native").strip().lower()

            if not project_id or not instance_name: continue
            print(f"\n[PROCESSING] Instance: {instance_name} (Project: {project_id})")

            # Initialize a localized dictionary for THIS instance only
            instance_report = {
                "project_id": project_id,
                "instance_name": instance_name,
                "report_window": report_window,
                "provisioned_specs": {},
                "resource_utilization": {},
                "health_checks": {},
            }

            print("   -> Fetching instance hardware and configuration details...")
            specs, connection_name, db_type, region, host, port = get_instance_details(sqladmin, project_id, instance_name)
            
            if isinstance(specs, dict) and "Error" in specs:
                instance_report["provisioned_specs"] = specs
                print(f"   [!] Skipping {instance_name}: Could not fetch instance details.")
                continue

            instance_report["provisioned_specs"] = specs
            if not connection_name:
                print(f"   [!] Skipping {instance_name}: Valid connection_name not found.")
                continue

            print(f"   -> Engine identified as: {db_type.upper()}")

            print("   -> Fetching requested Cloud Monitoring metrics (24h window)...")
            engine_metrics = get_metrics_for_engine(db_type, requested_metrics)
            
            if not engine_metrics:
                print("   [!] No valid monitoring metrics were found in 'config.yaml' for this engine.")
            else:
                for m_key, m_type in engine_metrics.items():
                    metric_data = fetch_mql_metric(
                        mon_client, project_id, instance_name, m_key, m_type
                    )
                    metric_data["header-name"] = METRIC_LABELS.get(m_key, m_key)
                    instance_report["resource_utilization"][m_key] = metric_data

            print(f"   -> Connecting to database via '{auth_type}' auth to run queries...")
            try:
                if auth_type == "iam":
                    engine = get_iam_engine(
                        {
                            "project_id": project_id, "region": region, "instance": instance_name,
                            "user": db_user, "database": "mysql" if db_type == "mysql" else "postgres", "db_type": db_type,
                        }, connector,
                    )
                else:
                    engine = get_native_engine({
                        "db_type": db_type, "user": db_user, "password": db_pass,
                        "host": host, "port": port, "database": "mysql" if db_type == "mysql" else "postgres",
                    })
                
                print("   Executing Database Audits & Internal Queries...")
                health_checks, internal_results = run_queries(engine, db_type, all_queries, INTERNAL_QUERIES)
                instance_report["health_checks"] = health_checks
                instance_report["provisioned_specs"]["Uptime"] = format_uptime(internal_results.get("uptime"), db_type)
                print(f"   -> Successfully executed health check queries for {instance_name}.")

            except Exception as e:
                print(f"   [!] Connection/Query execution failed: {e}")
                instance_report["health_checks"] = {"error": str(e)}
            finally:
                if 'engine' in locals():
                    engine.dispose() # Clean up connection pool

            # ─────────────────────────────────────────────
            # DUMP INDIVIDUAL JSON FILE
            # ─────────────────────────────────────────────
            filename = f"{project_id}_{instance_name}.json"
            filepath = os.path.join(reports_dir, filename)
            
            with open(filepath, "w") as f:
                json.dump(instance_report, f, indent=4, default=str)
            print(f"   [SUCCESS] Saved report to '{filepath}'")

    print("\n[DONE] All instances processed successfully.\n")

if __name__ == "__main__":
    main()
