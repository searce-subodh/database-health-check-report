import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import csv
import json

def connect_with_connector_db_auth(connector: Connector, row: dict) -> sqlalchemy.engine.base.Engine:
    # Added .strip() to prevent whitespace issues from the CSV
    database_type = row["db_type"].lower().strip()
    instance_connection_name = row["connection_name"].strip()
    db_user = row["db_user"].strip()
    db_pass = row["db_pass"].strip()
    db_name = row.get("database", "").strip()
    
    ip_type = IPTypes.PRIVATE

    conn_kwargs = {
        "user": db_user,
        "password": db_pass,
        "ip_type": ip_type
    }
    if db_name:
        conn_kwargs["db"] = db_name

    if database_type == "mysql":
        def getconn():
            return connector.connect(
                instance_connection_name,
                "pymysql",
                **conn_kwargs
            )
        db_url = "mysql+pymysql://"
        
    elif database_type == "postgres":
        def getconn():
            # For Postgres, if DB is empty, it usually defaults to "postgres"
            if "db" not in conn_kwargs:
                conn_kwargs["db"] = "postgres"
                
            return connector.connect(
                instance_connection_name,
                "pg8000",
                **conn_kwargs
            )
        db_url = "postgresql+pg8000://"
    else:
        raise ValueError(f"Unsupported database type: {database_type}")
        
    engine = sqlalchemy.create_engine(db_url, creator=getconn)
    return engine


def main():
    report_results = {}
    
    with open("queries.json", "r") as f:
        all_database_queries = json.load(f)

    with Connector(refresh_strategy="LAZY") as connector:
        with open("config.csv", newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            
            for row in reader:
                # --- FIXED LINE BELOW ---
                # Changed from row.get("instance_connection_name") to row["connection_name"]
                server_key = row["connection_name"].strip()
                db_type = row.get("db_type", "").lower().strip()
                
                report_results[server_key] = {}
                
                engine_queries = all_database_queries.get(db_type)
                if not engine_queries:
                    print(f"Skipping {server_key}: No queries found in JSON for db_type '{db_type}'")
                    continue

                try:
                    engine = connect_with_connector_db_auth(connector, row)
                    
                    with engine.connect() as connection:
                        for category, queries in engine_queries.items():
                            report_results[server_key][category] = {}
                            
                            for key, sql in queries.items():
                                try:
                                    result = connection.execute(sqlalchemy.text(sql))
                                    query_data = [dict(r) for r in result.mappings()]
                                    report_results[server_key][category][key] = query_data
                                except Exception as query_err:
                                    print(f"Query failed [{category} -> {key}]: {query_err}")
                                    report_results[server_key][category][key] = {"error": str(query_err)}
                                    
                except Exception as conn_err:
                    print(f"Failed to connect to {server_key}: {conn_err}")
                    report_results[server_key]["connection_error"] = str(conn_err)
                    
                finally:
                    if 'engine' in locals():
                        engine.dispose()

    with open("database_health_combined_report.json", "w") as f:
        json.dump(report_results, f, indent=4, default=str)
    
    print("Health check complete. Results written to database_health_combined_report.json")

if __name__ == "__main__":
    main()