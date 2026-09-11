import json
import re
import csv
import os
import sqlalchemy
from datetime import datetime, timezone, timedelta
from googleapiclient import discovery
from google.cloud import monitoring_v3
from google.cloud.sql.connector import Connector, IPTypes
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
# CLOUD MONITORING METRICS
# ─────────────────────────────────────────────

METRIC_TYPES = {
    "cpu_utilization":    "cloudsql.googleapis.com/database/cpu/utilization",
    "memory_utilization": "cloudsql.googleapis.com/database/memory/utilization",
    "disk_utilization":   "cloudsql.googleapis.com/database/disk/utilization",
    "disk_read_ops":      "cloudsql.googleapis.com/database/disk/read_ops_count",
    "disk_write_ops":     "cloudsql.googleapis.com/database/disk/write_ops_count",
    "disk_bytes_used":    "cloudsql.googleapis.com/database/disk/bytes_used",
    "connections":        "cloudsql.googleapis.com/database/network/connections",
    "queries":            "cloudsql.googleapis.com/database/mysql/queries",
}


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
    """Convert ISO timestamp like 2026-09-02T19:00:00Z to 02 Sep 2026, 07:00 PM"""
    if not ts or ts == "No backups found":
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return ts


# ─────────────────────────────────────────────
# FORMAT UPTIME FROM SECONDS TO HUMAN READABLE
# ─────────────────────────────────────────────

def format_uptime(seconds) -> str:
    """Convert seconds to X days Y hours Z minutes"""
    try:
        seconds = int(seconds)
        days    = seconds // 86400
        hours   = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        parts = []
        if days:    parts.append(f"{days}d")
        if hours:   parts.append(f"{hours}h")
        if minutes: parts.append(f"{minutes}m")
        return " ".join(parts) if parts else "< 1 minute"
    except Exception:
        return str(seconds)


# ─────────────────────────────────────────────
# FETCH INSTANCE DETAILS FROM CLOUD SQL ADMIN API
# ─────────────────────────────────────────────

def get_instance_details(service, project_id: str, instance_name: str) -> tuple:
    try:
        inst = service.instances().get(project=project_id, instance=instance_name).execute()
    except Exception as e:
        return {"Error": f"Failed to fetch instance details: {str(e)}"}, None, None

    settings     = inst.get("settings", {})
    vcpu, memory = parse_compute_specs(settings.get("tier"))
    disk_type    = settings.get("dataDiskType", "").replace("PD_", "")
    db_version   = inst.get("databaseVersion", "UNKNOWN")
    db_type      = "mysql" if "MYSQL" in db_version else "postgresql" if "POSTGRES" in db_version else "unknown"
    connection_name = inst.get("connectionName")

    last_backup_time = "No backups found"
    try:
        backups = service.backupRuns().list(
            project=project_id, instance=instance_name, maxResults=1
        ).execute().get("items", [])
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = format_timestamp(backups[0]["windowStartTime"])
    except Exception:
        pass

    specs = {
        "Engine":             db_version.replace("_", " "),
        "Edition":            settings.get("edition", "ENTERPRISE"),
        "CPU":                vcpu,
        "Memory":             memory,
        "Storage":            f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip(),
        "Availability":       settings.get("availabilityType", "N/A").title(),
        "Read Replica count": len(inst.get("replicaNames", [])),
        "Last Backup time":   last_backup_time,
    }

    return specs, connection_name, db_type


# ─────────────────────────────────────────────
# FETCH UPTIME FROM DATABASE
# ─────────────────────────────────────────────

def fetch_uptime(engine, db_type: str) -> str:
    """Fetch uptime directly from the database and return human readable string"""
    try:
        if db_type == "mysql":
            mysql_conn = engine.raw_connection()
            cursor = mysql_conn.cursor()
            cursor.execute("SHOW STATUS LIKE 'Uptime';")
            row = cursor.fetchone()
            cursor.close()
            mysql_conn.close()
            if row:
                return format_uptime(row[1])

        elif db_type == "postgresql":
            with engine.connect() as conn:
                raw_conn = conn.connection
                cursor = raw_conn.cursor()
                cursor.execute("SELECT EXTRACT(EPOCH FROM (NOW() - pg_postmaster_start_time()))::int AS uptime_seconds;")
                row = cursor.fetchone()
                cursor.close()
                if row:
                    return format_uptime(row[0])
    except Exception:
        pass
    return "N/A"


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
    metric_suffix = metric_type.split('/')[-1]
    value_col     = f"value.{metric_suffix}"

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

    result = {"mean": None, "max": None, "p95": None, "p99": None}

    try:
        request  = monitoring_v3.QueryTimeSeriesRequest(name=f"projects/{project_id}", query=mql_aggregated)
        response = client.query_time_series(request=request)
        for series_data in response:
            if not series_data.point_data:
                continue
            point = series_data.point_data[0]
            vals  = [extract_typed_value(point.values[i]) for i in range(3)]
            if "utilization" in metric_key:
                vals = [v * 100 for v in vals]
            result["mean"] = round(vals[0], 2)
            result["p95"]  = round(vals[1], 2)
            result["p99"]  = round(vals[2], 2)
    except Exception:
        pass

    try:
        request  = monitoring_v3.QueryTimeSeriesRequest(name=f"projects/{project_id}", query=mql_max)
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
# CONNECTION FUNCTIONS
# ─────────────────────────────────────────────

def connect_postgresql(connector: Connector, connection_name: str, db_user: str, db_pass: str, db_name: str) -> sqlalchemy.engine.base.Engine:
    def getconn():
        return connector.connect(
            connection_name, "pg8000",
            user=db_user, password=db_pass, db=db_name,
            enable_iam_auth=False, ip_type=IPTypes.PRIVATE,
        )
    return sqlalchemy.create_engine("postgresql+pg8000://", creator=getconn)


def connect_mysql(connector: Connector, connection_name: str, db_user: str, db_pass: str, db_name: str) -> sqlalchemy.engine.base.Engine:
    def getconn():
        return connector.connect(
            connection_name, "pymysql",
            user=db_user, password=db_pass, db=db_name,
            enable_iam_auth=False, ip_type=IPTypes.PRIVATE,
        )
    return sqlalchemy.create_engine("mysql+pymysql://", creator=getconn)


# ─────────────────────────────────────────────
# IAM AUTH ENGINE (service account based)
# ─────────────────────────────────────────────

def get_iam_engine(target, connector):
    db_type       = target.get("db_type", "mysql").lower()
    instance_name = f"{target['project_id']}:{target['region']}:{target['instance']}"

    if db_type == "postgres":
        driver  = "pg8000"
        dialect = "postgresql+pg8000://"
    elif db_type == "mysql":
        driver  = "pymysql"
        dialect = "mysql+pymysql://"
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

    def _getconn():
        return connector.connect(
            instance_name, driver,
            user=target["user"], db=target["database"],
            enable_iam_auth=True,
        )

    return sqlalchemy.create_engine(dialect, creator=_getconn, pool_pre_ping=True)


# ─────────────────────────────────────────────
# NATIVE ENGINE (direct host/port connection)
# ─────────────────────────────────────────────

def get_native_engine(target):
    db_type  = target.get("db_type", "mysql").lower()
    user     = target.get("native_user", target.get("user"))
    password = target.get("password", "")
    host     = target.get("host", "127.0.0.1")
    database = target.get("database")

    if db_type == "postgres":
        port = target.get("port", 5432)
        url  = f"postgresql+pg8000://{user}:{password}@{host}:{port}/{database}"
    elif db_type == "mysql":
        port = target.get("port", 3306)
        url  = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

    return sqlalchemy.create_engine(url, pool_pre_ping=True)


# ─────────────────────────────────────────────
# RESOLVE DATABASES
# ─────────────────────────────────────────────

def resolve_databases(connector, connection_name, db_user, db_pass, db_type, db_name_field) -> list:
    db_name_field = db_name_field.strip()

    if db_name_field.upper() == "ALL":
        print("  🔎 db_name = ALL — fetching all databases on instance...")
        databases = get_all_databases(connector, connection_name, db_user, db_pass, db_type)
        print(f"  📋 Found: {databases}")
        return databases

    return [db.strip() for db in db_name_field.split(",") if db.strip()]


def get_all_databases(connector, connection_name, db_user, db_pass, db_type) -> list:
    if db_type == "postgresql":
        engine = connect_postgresql(connector, connection_name, db_user, db_pass, "postgres")
        with engine.connect() as conn:
            raw_conn = conn.connection
            cursor   = raw_conn.cursor()
            cursor.execute("""
                SELECT datname FROM pg_database
                WHERE datistemplate = false
                AND datname NOT IN ('postgres', 'cloudsqladmin')
                ORDER BY datname;
            """)
            databases = [r[0] for r in cursor.fetchall()]
            cursor.close()

    elif db_type == "mysql":
        engine     = connect_mysql(connector, connection_name, db_user, db_pass, "mysql")
        mysql_conn = engine.raw_connection()
        cursor     = mysql_conn.cursor()
        cursor.execute("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name NOT IN
            ('mysql', 'information_schema', 'performance_schema', 'sys')
            ORDER BY schema_name;
        """)
        databases = [r[0] for r in cursor.fetchall()]
        cursor.close()
        mysql_conn.close()

    return databases


# ─────────────────────────────────────────────
# QUERY RUNNER
# ─────────────────────────────────────────────

def run_queries(engine, db_type: str, all_queries: dict) -> dict:
    server_results = {}
    queries        = all_queries.get(db_type, {})

    if db_type == "postgresql":
        with engine.connect() as conn:
            raw_conn = conn.connection
            cursor   = raw_conn.cursor()

            print("    🔄 Running ANALYZE...")
            cursor.execute("ANALYZE;")
            raw_conn.commit()
            print("    ✅ ANALYZE done.")

            for category, category_queries in queries.items():
                server_results[category] = {}
                for key, sql in category_queries.items():
                    if key.startswith("_"):
                        continue
                    try:
                        cursor.execute(sql)
                        columns = [desc[0] for desc in cursor.description]
                        records = cursor.fetchall()
                        server_results[category][key] = [
                            dict(zip(columns, record)) for record in records
                        ]
                    except Exception as e:
                        raw_conn.rollback()
                        print(f"    ⚠️  Query failed [{category} → {key}]: {e}")
                        server_results[category][key] = []

            cursor.close()

    elif db_type == "mysql":
        mysql_conn = engine.raw_connection()
        cursor     = mysql_conn.cursor(pymysql.cursors.DictCursor)

        for category, category_queries in queries.items():
            server_results[category] = {}
            for key, sql in category_queries.items():
                if key.startswith("_"):
                    continue
                try:
                    cursor.execute(sql)
                    server_results[category][key] = cursor.fetchall()
                except Exception as e:
                    print(f"    ⚠️  Query failed [{category} → {key}]: {e}")
                    server_results[category][key] = []

        cursor.close()
        mysql_conn.close()

    return server_results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    all_queries = json.load(open("queries.json"))
    sqladmin    = discovery.build("sqladmin", "v1", cache_discovery=False)
    mon_client  = monitoring_v3.QueryServiceClient()
    report      = {}

    # Report time window — last 24 hours
    report_end   = datetime.now(timezone.utc)
    report_start = report_end - timedelta(hours=24)
    report_window = {
        "from": report_start.strftime("%d %b %Y, %I:%M %p UTC"),
        "to":   report_end.strftime("%d %b %Y, %I:%M %p UTC"),
    }

    with Connector(refresh_strategy="LAZY") as connector:
        with open("config.csv", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)

            for row in reader:
                project_id    = row.get("project_id", "").strip()
                instance_name = row.get("instance_name", "").strip()
                db_user       = row.get("db_user", "").strip()
                db_pass       = row.get("db_pass", "").strip()
                db_name_field = row.get("db_name", "ALL").strip()

                if not project_id or not instance_name:
                    continue

                print(f"\n🔍 Instance: {instance_name} (project: {project_id})")

                report[instance_name] = {
                    "report_window":        report_window,
                    "provisioned_specs":    {},
                    "resource_utilization": {},
                    "health_checks":        {},
                }

                # Step 1 — Instance specs
                specs, connection_name, db_type = get_instance_details(sqladmin, project_id, instance_name)
                report[instance_name]["provisioned_specs"] = specs

                if "Error" in specs or not connection_name:
                    print(f"  ❌ Skipping: could not fetch instance details.")
                    continue

                print(f"  📋 Engine: {db_type} | Connection: {connection_name}")

                # Step 2 — Monitoring metrics
                print("  📊 Fetching Cloud Monitoring metrics...")
                for m_key, m_type in METRIC_TYPES.items():
                    report[instance_name]["resource_utilization"][m_key] = fetch_mql_metric(
                        mon_client, project_id, instance_name, m_key, m_type
                    )

                # Step 3 — Resolve databases
                databases = resolve_databases(
                    connector, connection_name, db_user, db_pass, db_type, db_name_field
                )

                if not databases:
                    print("  ⚠️  No user databases found.")
                    continue

                # Step 4 — Fetch uptime from first available database
                try:
                    if db_type == "postgresql":
                        uptime_engine = connect_postgresql(connector, connection_name, db_user, db_pass, "postgres")
                    else:
                        uptime_engine = connect_mysql(connector, connection_name, db_user, db_pass, databases[0])
                    uptime = fetch_uptime(uptime_engine, db_type)
                    report[instance_name]["provisioned_specs"]["Uptime"] = uptime
                except Exception:
                    report[instance_name]["provisioned_specs"]["Uptime"] = "N/A"

                # Step 5 — Run health check queries per database
                for db_name in databases:
                    print(f"\n  🗄️  Checking database: {db_name}")
                    try:
                        auth_type = row.get("auth_type", "").strip().lower()
                        region    = row.get("region", "").strip()
                        host      = row.get("host", "127.0.0.1").strip()
                        port      = row.get("port", "").strip()

                        if auth_type == "iam":
                            engine = get_iam_engine({
                                "project_id": project_id,
                                "region":     region or connection_name.split(":")[1],
                                "instance":   instance_name,
                                "user":       db_user,
                                "database":   db_name,
                                "db_type":    "postgres" if db_type == "postgresql" else "mysql",
                            }, connector)

                        elif auth_type == "native":
                            engine = get_native_engine({
                                "db_type":  "postgres" if db_type == "postgresql" else "mysql",
                                "user":     db_user,
                                "password": db_pass,
                                "host":     host,
                                "port":     int(port) if port else (5432 if db_type == "postgresql" else 3306),
                                "database": db_name,
                            })

                        else:
                            # Default — username + password via Cloud SQL Connector
                            if db_type == "postgresql":
                                engine = connect_postgresql(connector, connection_name, db_user, db_pass, db_name)
                            else:
                                engine = connect_mysql(connector, connection_name, db_user, db_pass, db_name)

                        report[instance_name]["health_checks"][db_name] = run_queries(engine, db_type, all_queries)
                        print(f"    ✅ Done.")

                    except Exception as e:
                        print(f"    ❌ Failed: {e}")
                        report[instance_name]["health_checks"][db_name] = {"error": str(e)}

    with open("database_health_report.json", "w") as f:
        json.dump(report, f, indent=4, default=str)

    print("\n📄 Report saved to database_health_report.json")


if __name__ == "__main__":
    main()
