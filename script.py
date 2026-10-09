from datetime import datetime, timedelta, timezone
import json
import re
import os
import yaml
import numpy as np
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
        "uptime": "SHOW GLOBAL STATUS LIKE 'Uptime';",
        "max_connections": "SHOW GLOBAL VARIABLES LIKE 'max_connections';"
    },
    "postgres": {
        "uptime": "SELECT pg_postmaster_start_time() AS start_time;",
        "max_connections": "SHOW max_connections;",
        "get_databases": "SELECT datname FROM pg_database WHERE datistemplate = false AND has_database_privilege(datname, 'connect');"
    },
}

# ─────────────────────────────────────────────
# TIER MAP & IOPS REFERENCE DATA
# ─────────────────────────────────────────────

TIER_MAP = {
    "db-f1-micro": (1, "0.614 GB"),
    "db-g1-small": (1, "1.7 GB"),
}

MACHINE_LIMITS = {
    "SSD": {
        "ENTERPRISE": {
            "SHARED_CORE": [(0, (12000, 10000))], 
            "DEDICATED_CORE": [
                (1, (12000, 10000)),
                (2, (15000, 15000)),
                (16, (25000, 25000)),
                (32, (60000, 60000)),
                (64, (100000, 100000))
            ]
        },
        "ENTERPRISE_PLUS": {
            "N2": [
                (2, (15000, 15000)),
                (16, (25000, 25000)),
                (32, (60000, 60000)),
                (64, (100000, 80000))
            ]
        }
    },
    "HDD": {
        "ENTERPRISE": {
            "SHARED_CORE": [(0, (1000, 10000))],
            "DEDICATED_CORE": [
                (1, (1000, 10000)),
                (2, (3000, 15000)),
                (8, (5000, 15000)),
                (16, (7500, 15000))
            ]
        }
    }
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
# HELPER: IOPS CALCULATION
# ─────────────────────────────────────────────

def determine_machine_family(machine_tier: str) -> str:
    tier = machine_tier.lower()
    if "shared" in tier or "f1" in tier or "g1" in tier: return "SHARED_CORE"
    if "n4" in tier: return "N4"
    if "n2" in tier: return "N2"
    if "c4a" in tier: return "c4A"
    return "DEDICATED_CORE"

def extract_vcpu(machine_tier: str) -> int:
    parts = machine_tier.split('-')
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return 1

def get_machine_limit(disk_type: str, edition: str, machine_family: str, vcpu: int):
    try:
        checkpoints = MACHINE_LIMITS[disk_type][edition][machine_family]
        max_r, max_w = None, None
        
        for cp_vcpu, limits in checkpoints:
            if vcpu >= cp_vcpu:
                max_r, max_w = limits
            else:
                break
        return max_r, max_w
    except KeyError:
        return None, None

def calculate_iops_limits(machine_tier: str, disk_type: str, storage_gb: int, edition: str, provisioned_iops: int):
    machine_family = determine_machine_family(machine_tier)
    vcpu = extract_vcpu(machine_tier)
    
    is_user_provisioned = machine_family in ["N4", "c4A"]

    if is_user_provisioned:
        max_r, max_w = None, None
    else:
        max_r, max_w = get_machine_limit(disk_type, edition, machine_family, vcpu)

    if is_user_provisioned:
        cap_r = provisioned_iops if provisioned_iops else 0
        cap_w = provisioned_iops if provisioned_iops else 0
    elif disk_type == "SSD":
        cap_r = cap_w = 6000 + (30 * storage_gb)
    elif disk_type == "HDD":
        if storage_gb <= 100:
            cap_r, cap_w = 75, 150
        else:
            cap_r = int(storage_gb * 0.75)
            cap_w = int(storage_gb * 1.5)
    else:
        cap_r, cap_w = 0, 0

    if is_user_provisioned:
        final_r, final_w = cap_r, cap_w
    else:
        final_r = min(cap_r, max_r) if max_r else cap_r
        final_w = min(cap_w, max_w) if max_w else cap_w

    return final_r, final_w


# ─────────────────────────────────────────────
# HELPER: FORMAT UPTIME & EXTRACT SPECS
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
            if days > 0: parts.append(f"{days}d")
            if hours > 0: parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            
            return " ".join(parts) if parts else "0m"
                
    except Exception as e:
        print(f"      [!] Uptime parse error: {e}")

    return "N/A"


def parse_compute_specs(tier: str) -> tuple:
    if not tier: return ("N/A", "N/A")
    if tier in TIER_MAP: return (str(TIER_MAP[tier][0]), TIER_MAP[tier][1])
    
    custom_match = re.search(r"-(\d+)-(\d+)$", tier)
    if custom_match:
        memory_gb = round(int(custom_match.group(2)) / 1024, 2)
        return (custom_match.group(1), f"{memory_gb} GB")
        
    suffix_match = re.search(r"-(\d+)$", tier)
    return (suffix_match.group(1), "N/A") if suffix_match else ("N/A", "N/A")


def format_timestamp(ts: str) -> str:
    if not ts or ts == "No backups found": return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return ts


def get_instance_details(service, project_id: str, instance_name: str) -> tuple:
    try:
        inst = service.instances().get(project=project_id, instance=instance_name).execute()
    except Exception as e:
        return ({"Error": f"Failed to fetch details: {str(e)}"}, None, None, None, None, None, {})

    settings = inst.get("settings", {})
    tier = settings.get("tier", "")
    vcpu, memory = parse_compute_specs(tier)
    disk_type_raw = settings.get("dataDiskType", "")
    disk_type = "SSD" if "SSD" in disk_type_raw else ("HDD" if "HDD" in disk_type_raw else "N/A")
    db_version = inst.get("databaseVersion", "UNKNOWN")
    edition = settings.get("edition", "ENTERPRISE").upper()
    storage_gb = int(settings.get("dataDiskSizeGb", 0))
    provisioned_iops = int(settings.get("dataDiskProvisionedIops", 0)) if settings.get("dataDiskProvisionedIops") else 0

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
        backups = service.backupRuns().list(project=project_id, instance=instance_name, maxResults=1).execute().get("items", [])
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = format_timestamp(backups[0]["windowStartTime"])
    except Exception:
        pass

    specs = {
        "Engine": db_version.replace("_", " "),
        "Edition": edition,
        "CPU": vcpu,
        "Memory": memory,
        "Storage": f"{storage_gb} GB {disk_type_raw.replace('PD_', '')}".strip(),
        "Availability": settings.get("availabilityType", "N/A").title(),
        "Replicas": len(inst.get("replicaNames", [])),
        "Last Backup": last_backup_time,
    }

    raw_hw_details = {
        "tier": tier,
        "disk_type": disk_type,
        "storage_gb": storage_gb,
        "edition": edition,
        "provisioned_iops": provisioned_iops
    }

    return specs, connection_name, db_type, region, host, port, raw_hw_details


# ─────────────────────────────────────────────
# FETCH MONITORING METRICS
# ─────────────────────────────────────────────

def extract_typed_value(typed_value):
    val_type = typed_value._pb.WhichOneof("value")
    if val_type == "double_value": return typed_value.double_value
    elif val_type == "int64_value": return float(typed_value.int64_value)
    return 0.0

def fetch_mql_metric(client, project_id, instance_id, metric_key, metric_type):
    metric_suffix = metric_type.split("/")[-1]
    value_col = f"value.{metric_suffix}"

    mql_query = f"""
    fetch cloudsql_database
    | metric '{metric_type}'
    | filter (resource.database_id == '{project_id}:{instance_id}')
    | within 24h
    | group_by [], [val: sum({value_col})]
    | every 1m
    """
    
    result = {"mean": None, "p95": None, "p99": None, "max": None}
    data_points = []

    def scale_value(val):
        if val is None: return None
        if "utilization" in metric_key: return val * 100
        elif "bytes_used" in metric_key: return val / (1024 ** 3)
        elif "ops" in metric_key: return val / 60
        return val

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(name=f"projects/{project_id}", query=mql_query)
        response = client.query_time_series(request=request)
        
        for series_data in response:
            for point in series_data.point_data:
                raw_val = extract_typed_value(point.values[0])
                data_points.append(scale_value(raw_val))
                
        if data_points:
            result["mean"] = round(np.mean(data_points), 2)
            result["p95"] = round(np.percentile(data_points, 95), 2)
            result["p99"] = round(np.percentile(data_points, 99), 2)
            result["max"] = round(np.max(data_points), 2)
            
    except Exception as e:
        print(f"      [!] Metric query failed for '{metric_key}': {e}")

    return result

# ─────────────────────────────────────────────
# DB AUTH ENGINES
# ─────────────────────────────────────────────

def get_iam_engine(target, connector):
    db_type = target.get("db_type").lower()
    instance_name = f"{target['project_id']}:{target['region']}:{target['instance']}"
    driver, dialect = ("pg8000", "postgresql+pg8000://") if db_type == "postgres" else ("pymysql", "mysql+pymysql://")

    def _getconn():
        return connector.connect(instance_name, driver, user=target["user"], db=target["database"], enable_iam_auth=True, ip_type="private")

    return sqlalchemy.create_engine(dialect, creator=_getconn, pool_pre_ping=True)


def get_native_engine(target):
    db_type = target.get("db_type", "").lower()
    drivername = "postgresql+pg8000" if db_type == "postgres" else "mysql+pymysql"
    
    url = URL.create(
        drivername=drivername, username=target.get("user", ""), password=target.get("password", ""),
        host=target.get("host", ""), port=target.get("port"), database=target.get("database")
    )
    return sqlalchemy.create_engine(url, pool_pre_ping=True)


# ─────────────────────────────────────────────
# QUERY RUNNER FUNCTIONS
# ─────────────────────────────────────────────

def execute_query(conn, primary_sql, fallback_sql, category, key):
    """Helper function to execute a single query with fallback logic."""
    try:
        with conn.begin_nested():
            result = conn.execute(sqlalchemy.text(primary_sql))
            if result.returns_rows:
                rows = result.fetchall()
                return [dict(row._mapping) for row in rows] if rows else []
            else:
                return [{"message": "Query executed successfully"}]
    except Exception as e:
        print(f"    [!] Query failed [{category} -> {key}]: {e}")
        if fallback_sql:
            print(f"    [*] Attempting fallback query for [{category} -> {key}]...")
            try:
                with conn.begin_nested():
                    result = conn.execute(sqlalchemy.text(fallback_sql))
                    if result.returns_rows:
                        rows = result.fetchall()
                        return [dict(row._mapping) for row in rows] if rows else []
                    else:
                        return [{"message": "Fallback query executed successfully"}]
            except Exception as fallback_e:
                print(f"    [!] Fallback Query failed [{category} -> {key}]: {fallback_e}")
                return [{"status": "ERROR", "message": "Primary and fallback queries failed"}]
        else:
            return [{"status": "ERROR", "message": "Result unavailable due to execution error"}]


def run_mysql_flow(engine, user_queries, internal_queries) -> tuple:
    # """Executes MySQL logic: runs everything once on the initial connection."""
    health_checks = {}
    internal_results = {}
    db_user_queries = user_queries.get("mysql", {})
    db_internal_queries = internal_queries.get("mysql", {})

    with engine.connect() as conn:
        # Run standard MySQL queries
        for category, category_queries in db_user_queries.items():
            health_checks[category] = {}
            for key, query_payload in category_queries.items():
                if key.startswith("_"): continue
                
                primary_sql = query_payload.get("preferred_query") if isinstance(query_payload, dict) else query_payload
                fallback_sql = query_payload.get("fallback_query") if isinstance(query_payload, dict) else None

                res = execute_query(conn, primary_sql, fallback_sql, category, key)
                health_checks[category][key] = res if res else [{"message": "0 records returned"}]

        # Run internal MySQL queries
        for key, sql in db_internal_queries.items():
            try:
                with conn.begin_nested():
                    result = conn.execute(sqlalchemy.text(sql))
                    if result.returns_rows:
                        rows = result.fetchall()
                        internal_results[key] = [dict(row._mapping) for row in rows] if rows else []
            except Exception as e:
                print(f"    [!] Internal Query failed [{key}]: {e}")

    return health_checks, internal_results


def run_postgres_flow(engine, user_queries, internal_queries, engine_factory) -> tuple:
    # """Executes PostgreSQL logic: runs global queries first, then iterates accessible databases for local queries."""
    health_checks = {}
    internal_results = {}
    db_user_queries = user_queries.get("postgres", {})
    db_internal_queries = internal_queries.get("postgres", {})

    # PHASE 1: Connect to default DB and execute 'global' scoped & internal queries
    with engine.connect() as conn:
        # Prepare structure and run globals
        for category, category_queries in db_user_queries.items():
            health_checks[category] = {}
            for key, query_payload in category_queries.items():
                if key.startswith("_"): continue
                
                scope = query_payload.get("scope", "global") if isinstance(query_payload, dict) else "global"
                
                # Initialize local queries with empty lists for Phase 2 appending
                if scope == "local":
                    health_checks[category][key] = []
                    continue

                # Execute global queries
                primary_sql = query_payload.get("preferred_query") if isinstance(query_payload, dict) else query_payload
                fallback_sql = query_payload.get("fallback_query") if isinstance(query_payload, dict) else None
                
                res = execute_query(conn, primary_sql, fallback_sql, category, key)
                health_checks[category][key] = res if res else [{"message": "0 records returned"}]

        # Run internal queries (including get_databases)
        for key, sql in db_internal_queries.items():
            try:
                with conn.begin_nested():
                    result = conn.execute(sqlalchemy.text(sql))
                    if result.returns_rows:
                        rows = result.fetchall()
                        internal_results[key] = [dict(row._mapping) for row in rows] if rows else []
            except Exception as e:
                print(f"    [!] Internal Query failed [{key}]: {e}")

    # PHASE 2: Fetch database list and execute 'local' queries per database
    db_list = [row["datname"] for row in internal_results.get("get_databases", []) if "datname" in row]

    for db_name in db_list:
        try:
            local_engine = engine_factory(db_name)
            with local_engine.connect() as local_conn:
                for category, category_queries in db_user_queries.items():
                    for key, query_payload in category_queries.items():
                        if key.startswith("_"): continue
                        
                        scope = query_payload.get("scope", "global") if isinstance(query_payload, dict) else "global"
                        if scope != "local": continue

                        primary_sql = query_payload.get("preferred_query")
                        fallback_sql = query_payload.get("fallback_query")

                        res = execute_query(local_conn, primary_sql, fallback_sql, category, key)
                        
                        # Dynamically inject the database name into each returned row
                        for row in res:
                            if "message" in row or "status" in row:
                                continue # Skip appending raw empty/error messages for individual databases
                            
                            new_row = {"Database Name": db_name}
                            new_row.update(row)
                            health_checks[category][key].append(new_row)
                            
        except Exception as e:
            print(f" [!] Skipping database {db_name}: {e}")
        finally:
            if 'local_engine' in locals():
                local_engine.dispose()

    # PHASE 3: Clean up empty local queries
    for category in health_checks:
        for key in health_checks[category]:
            if not health_checks[category][key]:
                health_checks[category][key] = [{"message": "0 records returned"}]

    return health_checks, internal_results


def run_queries(engine, db_type: str, user_queries: dict, internal_queries: dict, engine_factory=None) -> tuple:
    # """Master router function directing traffic based on engine type."""
    if db_type == "mysql":
        return run_mysql_flow(engine, user_queries, internal_queries)
    elif db_type == "postgres":
        return run_postgres_flow(engine, user_queries, internal_queries, engine_factory)
    else:
        return {}, {}


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

            instance_report = {
                "project_id": project_id,
                "instance_name": instance_name,
                "report_window": report_window,
                "provisioned_specs": {},
                "resource_utilization": {},
                "health_checks": {},
            }

            print("   -> Fetching instance hardware and configuration details...")
            specs, connection_name, db_type, region, host, port, hw_details = get_instance_details(sqladmin, project_id, instance_name)
            
            if isinstance(specs, dict) and "Error" in specs:
                instance_report["provisioned_specs"] = specs
                print(f"   [!] Skipping {instance_name}: Could not fetch instance details.")
                continue

            instance_report["provisioned_specs"] = specs
            if not connection_name:
                print(f"   [!] Skipping {instance_name}: Valid connection_name not found.")
                continue

            final_read_iops, final_write_iops = calculate_iops_limits(
                machine_tier=hw_details.get("tier"),
                disk_type=hw_details.get("disk_type"),
                storage_gb=hw_details.get("storage_gb"),
                edition=hw_details.get("edition"),
                provisioned_iops=hw_details.get("provisioned_iops")
            )

            print(f"   -> Engine identified as: {db_type.upper()}")
            print("   -> Fetching requested Cloud Monitoring metrics (24h window)...")
            
            engine_metrics = get_metrics_for_engine(db_type, requested_metrics)
            
            if not engine_metrics:
                print("   [!] No valid monitoring metrics were found in 'config.yaml' for this engine.")
            else:
                for m_key, m_type in engine_metrics.items():
                    metric_data = fetch_mql_metric(mon_client, project_id, instance_name, m_key, m_type)
                    metric_data["header-name"] = METRIC_LABELS.get(m_key, m_key)
                    
                    if m_key in ["cpu_utilization", "memory_utilization", "disk_utilization"]:
                        metric_data["max_allocated_limit"] = 100
                    elif m_key == "disk_bytes_used":
                        metric_data["max_allocated_limit"] = hw_details.get("storage_gb")
                    elif m_key == "disk_read_ops":
                        metric_data["max_allocated_limit"] = final_read_iops
                    elif m_key == "disk_write_ops":
                        metric_data["max_allocated_limit"] = final_write_iops
                    elif m_key == "connections":
                        metric_data["max_allocated_limit"] = "N/A" 
                        
                    instance_report["resource_utilization"][m_key] = metric_data

            print(f"   -> Connecting to database via '{auth_type}' auth to run queries...")
            
            target_info = {
                "project_id": project_id, 
                "region": region, 
                "instance": instance_name, 
                "user": db_user, 
                "password": db_pass,
                "host": host, 
                "port": port,
                "db_type": db_type
            }

            def create_engine_for_db(db_name):
                target_override = target_info.copy()
                target_override["database"] = db_name
                if auth_type == "iam":
                    return get_iam_engine(target_override, connector)
                else:
                    return get_native_engine(target_override)

            try:
                default_db = "mysql" if db_type == "mysql" else "postgres"
                engine = create_engine_for_db(default_db)
                
                print("   Executing Database Audits & Internal Queries...")
                health_checks, internal_results = run_queries(
                    engine, db_type, all_queries, INTERNAL_QUERIES, engine_factory=create_engine_for_db
                )
                instance_report["health_checks"] = health_checks
                instance_report["provisioned_specs"]["Uptime"] = format_uptime(internal_results.get("uptime"), db_type)
                
                if "connections" in instance_report["resource_utilization"]:
                    max_conn_limit = "N/A"
                    conn_results = internal_results.get("max_connections", [])
                    
                    if conn_results and isinstance(conn_results, list):
                        if db_type == "mysql":
                            for r in conn_results:
                                if r.get("Variable_name", "").lower() == "max_connections":
                                    max_conn_limit = r.get("Value", "N/A")
                                    break
                        elif db_type == "postgres":
                            max_conn_limit = conn_results[0].get("max_connections", "N/A")
                    
                    try:
                        if max_conn_limit != "N/A":
                            max_conn_limit = int(max_conn_limit)
                    except ValueError:
                        pass
                        
                    instance_report["resource_utilization"]["connections"]["max_allocated_limit"] = max_conn_limit

                print(f"   -> Successfully executed health check queries for {instance_name}.")

            except Exception as e:
                print(f"   [!] Connection/Query execution failed: {e}")
                instance_report["health_checks"] = {"error": str(e)}
            finally:
                if 'engine' in locals():
                    engine.dispose() 

            filename = f"{project_id}_{instance_name}.json"
            filepath = os.path.join(reports_dir, filename)
            
            with open(filepath, "w") as f:
                json.dump(instance_report, f, indent=4, default=str)
            print(f"   [SUCCESS] Saved report to '{filepath}'")

    print("\n[DONE] All instances processed successfully.\n")

if __name__ == "__main__":
    main()