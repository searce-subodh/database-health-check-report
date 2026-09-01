import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import pg8000
import csv
import json


def connect_with_connector_db_auth(connector: Connector, row: dict) -> sqlalchemy.engine.base.Engine:
    instance_connection_name = row["connection_name"]
    db_user = row.get("db_user", "postgres")
    db_pass = row["password"]
    db_name = row.get("db_name", "postgres")

    ip_type = IPTypes.PRIVATE if row.get("use_private_ip") else IPTypes.PUBLIC

    def getconn() -> pg8000.dbapi.Connection:
        conn = connector.connect(
            instance_connection_name,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
            enable_iam_auth=False,
            ip_type=ip_type,
        )
        return conn

    engine = sqlalchemy.create_engine(
        "postgresql+pg8000://",
        creator=getconn,
    )
    return engine


def run_queries_for_server(engine: sqlalchemy.engine.base.Engine, database_queries: dict) -> dict:
    server_results = {}

    with engine.connect() as conn:
        raw_conn = conn.connection
        cursor = raw_conn.cursor()

        for category, queries in database_queries.items():
            server_results[category] = {}

            for key, sql in queries.items():
                if key.startswith("_"):
                    continue
                try:
                    cursor.execute(sql)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    server_results[category][key] = [
                        dict(zip(columns, row)) for row in rows
                    ]

                except Exception as e:
                    raw_conn.rollback()
                    print(f"  ⚠️  Query failed [{category} → {key}]: {e}")
                    server_results[category][key] = []

        cursor.close()

    return server_results


def main():
    report_results = {}
    database_queries = json.load(open("queries.json"))

    with Connector(refresh_strategy="LAZY") as connector:
        with open("config.csv", newline="") as csvfile:
            reader = csv.DictReader(csvfile)

            for row in reader:
                server_key = row.get("connection_name", "unknown_server")
                print(f"\n🔍 Connecting to: {server_key}")

                try:
                    engine = connect_with_connector_db_auth(connector, row)
                    report_results[server_key] = run_queries_for_server(engine, database_queries)
                    print(f"  ✅ Done.")

                except Exception as e:
                    print(f"  ❌ Could not connect to {server_key}: {e}")
                    report_results[server_key] = {"error": str(e)}

    with open("database_health_report.json", "w") as f:
        json.dump(report_results, f, indent=4, default=str)

    print("\n📄 Report saved to database_health_report.json")


if __name__ == "__main__":
    main()