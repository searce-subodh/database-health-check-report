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


def get_metrics_for_engine(db_type: str) -> dict:
    metrics = COMMON_METRICS.copy()
    if db_type == "mysql":
        metrics.update(MYSQL_METRICS)
    elif db_type in ("postgres", "postgresql"):
        metrics.update(POSTGRES_METRICS)
    return metrics


# ─────────────────────────────────────────────
# PARSE CPU/MEMORY FROM TIER STRING
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
    return (suffix_match.group(1), "N/A") if suffix_match else ("N/A", "N/A")


# ─────────────────────────────────────────────
# FORMAT TIMESTAMP TO HUMAN READABLE
# ─────────────────────────────────────────────


def format_timestamp(ts: str) -> str:
    """Convert ISO timestamp like 2026-09-02T19:00:00Z to 02 Sep 2026, 07:00 PM UTC"""
    if not ts or ts == "No backups found":
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return ts


# ─────────────────────────────────────────────
# FETCH INSTANCE DETAILS FROM CLOUD SQL ADMIN API
# ─────────────────────────────────────────────


def get_instance_details(service, project_id: str, instance_name: str) -> tuple:
    try:
        inst = (
            service.instances()
            .get(project=project_id, instance=instance_name)
            .execute()
        )
    except Exception as e:
        return (
            {"Error": f"Failed to fetch instance details: {str(e)}"},
            None,
            None,
            None,
            None,
            None,
        )

    settings = inst.get("settings", {})
    vcpu, memory = parse_compute_specs(settings.get("tier"))
    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    db_version = inst.get("databaseVersion", "UNKNOWN")

    db_type = (
        "mysql"
        if "MYSQL" in db_version
        else "postgres" if "POSTGRES" in db_version else "sql_server"
    )

    connection_name = inst.get("connectionName")
    region = inst.get("region")

    # Extract IP address (Prefer PRIVATE IP, fallback to PRIMARY/Public)
    ip_addresses = inst.get("ipAddresses", [])
    host = ""
    for ip in ip_addresses:
        if ip.get("type") == "PRIVATE":
            host = ip.get("ipAddress")
            break
    if not host and ip_addresses:
        host = ip_addresses[0].get("ipAddress")

    # Define port based on engine
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
        "Storage": (
            f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip()
        ),
        "Availability": settings.get("availabilityType", "N/A").title(),
        "Replicas": len(inst.get("replicaNames", [])),
        "Last Backup": last_backup_time,
        "Uptime": "N/A",
    }

    return specs, connection_name, db_type, region, host, port


# ─────────────────────────────────────────────
# FETCH MONITORING METRICS (P95/P99/MEAN/MAX)
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
    | group_by [], [max_val: max({value_col})]
    | every 1m
    """

    result = {"mean": None, "max": None, "p95": None, "p99": None}

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", query=mql_aggregated
        )
        response = client.query_time_series(request=request)
        for series_data in response:
            if not series_data.point_data:
                continue
            point = series_data.point_data[0]
            vals = [extract_typed_value(point.values[i]) for i in range(3)]
            if "utilization" in metric_key:
                vals = [v * 100 for v in vals]
            result["mean"] = round(vals[0], 2)
            result["p95"] = round(vals[1], 2)
            result["p99"] = round(vals[2], 2)
    except Exception:
        pass

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", query=mql_max
        )
        response = client.query_time_series(request=request)
        true_max = None
        for series_data in response:
            for point in series_data.point_data:
                val = extract_typed_value(point.values[0])
                if "utilization" in metric_key:
                    val = val * 100
                if true_max is None or val > true_max:
                    true_max = val
        if true_max is not None:
            result["max"] = round(true_max, 2)
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────
# IAM AUTH ENGINE (service account based)
# ─────────────────────────────────────────────


def get_iam_engine(target, connector):
    db_type = target.get("db_type").lower()
    instance_name = (
        f"{target['project_id']}:{target['region']}:{target['instance']}"
    )

    if db_type in ("postgres", "postgresql"):
        driver = "pg8000"
        dialect = "postgresql+pg8000://"
    elif db_type == "mysql":
        driver = "pymysql"
        dialect = "mysql+pymysql://"
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

    def _getconn():
        return connector.connect(
            instance_name,
            driver,
            user=target["user"],
            db=target["database"],
            enable_iam_auth=True,
        )

    return sqlalchemy.create_engine(dialect, creator=_getconn, pool_pre_ping=True)


# ─────────────────────────────────────────────
# NATIVE ENGINE (direct host/port connection)
# ─────────────────────────────────────────────


def get_native_engine(target):
    db_type = target.get("db_type", "").lower()
    user = target.get("native_user", target.get("user"))
    password = target.get("password", "")
    host = target.get("host", "")
    database = target.get("database")
    port = target.get("port")

    from urllib.parse import quote_plus
    user_enc = quote_plus(str(user))
    pass_enc = quote_plus(str(password))

    if db_type in ("postgres", "postgresql"):
        url = f"postgresql+pg8000://{user_enc}:{pass_enc}@{host}:{port}/{database}"
    elif db_type == "mysql":
        url = f"mysql+pymysql://{user_enc}:{pass_enc}@{host}:{port}/{database}"
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

    return sqlalchemy.create_engine(url, pool_pre_ping=True)


# ─────────────────────────────────────────────
# QUERY RUNNER
# ─────────────────────────────────────────────


def run_queries(engine, db_type: str, all_queries: dict) -> dict:
    server_results = {}
    queries = all_queries.get(db_type, {})

    with engine.connect() as conn:
        for category, category_queries in queries.items():
            server_results[category] = {}
            for key, sql in category_queries.items():
                if key.startswith("_"):
                    continue

                try:
                    with conn.begin_nested():
                        result = conn.execute(sqlalchemy.text(sql))
                        if result.returns_rows:
                            server_results[category][key] = [
                                dict(row._mapping) for row in result.fetchall()
                            ]
                        else:
                            server_results[category][key] = []
                except Exception as e:
                    print(f"      [!] Query failed [{category} -> {key}]: {e}")
                    server_results[category][key] = [{
                        "error": str(e),
                        "status": "FAILED",
                    }]

    return server_results


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

    print(" -> Building Google Cloud APIs (SQL Admin & Monitoring)...")
    sqladmin = discovery.build("sqladmin", "v1", cache_discovery=False)
    mon_client = monitoring_v3.QueryServiceClient()
    report = {}

    report_end = datetime.now(timezone.utc)
    report_start = report_end - timedelta(hours=24)
    report_window = {
        "from": report_start.strftime("%d %b %Y, %I:%M %p UTC"),
        "to": report_end.strftime("%d %b %Y, %I:%M %p UTC"),
    }

    with Connector(refresh_strategy="LAZY") as connector:
        try:
            with open("config.csv", newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)

                print(" -> Reading 'config.csv' for instances to process...")

                for row in reader:
                    project_id = row.get("project_id", "").strip()
                    instance_name = row.get("instance_name", "").strip()
                    db_user = row.get("db_user", "").strip()
                    db_pass = row.get("db_pass", "").strip()
                    auth_type = row.get("auth_type", "native").strip().lower()

                    if not project_id or not instance_name:
                        continue

                    print(f"\n[PROCESSING] Instance: {instance_name} (Project: {project_id})")

                    report[instance_name] = {
                        "report_window": report_window,
                        "provisioned_specs": {},
                        "resource_utilization": {},
                        "health_checks": {},
                    }

                    # Step 1 — Instance specs
                    print("   -> Fetching instance hardware and configuration details...")
                    specs, connection_name, db_type, region, host, port = (
                        get_instance_details(sqladmin, project_id, instance_name)
                    )

                    if isinstance(specs, dict) and "Error" in specs:
                        report[instance_name]["provisioned_specs"] = specs
                        print(f"   [!] Skipping {instance_name}: Could not fetch instance details.")
                        continue

                    report[instance_name]["provisioned_specs"] = specs

                    if not connection_name:
                        print(f"   [!] Skipping {instance_name}: Valid connection_name not found.")
                        continue

                    print(f"   -> Engine identified as: {db_type.upper()}")

                    # Step 2 — Fetch Engine-Specific Monitoring metrics
                    print("   -> Fetching Cloud Monitoring metrics (24h window)...")
                    engine_metrics = get_metrics_for_engine(db_type)
                    for m_key, m_type in engine_metrics.items():
                        report[instance_name]["resource_utilization"][m_key] = (
                            fetch_mql_metric(
                                mon_client, project_id, instance_name, m_key, m_type
                            )
                        )

                    # Step 3 — Run health check queries
                    print(f"   -> Connecting to database via '{auth_type}' auth to run queries...")
                    try:
                        if auth_type == "iam":
                            engine = get_iam_engine(
                                {
                                    "project_id": project_id,
                                    "region": region,
                                    "instance": instance_name,
                                    "user": db_user,
                                    "database": "mysql" if db_type == "mysql" else "postgres",
                                    "db_type": db_type,
                                },
                                connector,
                            )
                        else:
                            engine = get_native_engine({
                                "db_type": db_type,
                                "user": db_user,
                                "password": db_pass,
                                "host": host,
                                "port": port,
                                "database": "mysql" if db_type == "mysql" else "postgres",
                            })

                        report[instance_name]["health_checks"] = run_queries(
                            engine, db_type, all_queries
                        )

                        # Fetch uptime and update provisioned specs
                        try:
                            with engine.connect() as conn:
                                if db_type == "postgres":
                                    result = conn.execute(sqlalchemy.text(
                                        "SELECT EXTRACT(EPOCH FROM (NOW() - pg_postmaster_start_time()))::int"
                                    ))
                                    seconds = result.fetchone()[0]
                                else:
                                    result = conn.execute(sqlalchemy.text("SHOW STATUS LIKE 'Uptime'"))
                                    seconds = int(result.fetchone()[1])
                                days    = seconds // 86400
                                hours   = (seconds % 86400) // 3600
                                minutes = (seconds % 3600) // 60
                                parts   = []
                                if days:    parts.append(f"{days}d")
                                if hours:   parts.append(f"{hours}h")
                                if minutes: parts.append(f"{minutes}m")
                                report[instance_name]["provisioned_specs"]["Uptime"] = " ".join(parts) if parts else "< 1m"
                        except Exception:
                            pass  # Uptime stays as "N/A" set in specs dict

                        print(f"   -> Successfully executed health check queries for {instance_name}.")

                    except Exception as e:
                        print(f"   [!] Connection/Query execution failed: {e}")
                        report[instance_name]["health_checks"] = {"error": str(e)}

        except FileNotFoundError:
            print("\n[!] Error: 'config.csv' not found. Exiting.")
            return

    # Final Save
    print("\n[FINISHING] Compiling and saving final report...")
    with open("database_health_report.json", "w") as f:
        json.dump(report, f, indent=4, default=str)

    print("[DONE] Report successfully saved to 'database_health_report.json'.\n")


if __name__ == "__main__":
    main()
