import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import pymysql
import csv
import json
import pymysql.cursors



def connect_with_connector_db_auth(connector: Connector, row: dict) -> sqlalchemy.engine.base.Engine:
    instance_connection_name = row["connection_name"]
    db_user = 'root'
    db_pass = row["password"]
    db_name = 'application'

    ip_type = IPTypes.PRIVATE if row.get("use_private_ip") else IPTypes.PUBLIC

    def getconn() -> pymysql.connections.Connection:
        conn: pymysql.connections.Connection = connector.connect(
            instance_connection_name,
            "pymysql",
            user=db_user,
            password=db_pass,
            db=db_name,
            enable_iam_auth=False,
            ip_type=ip_type,
        )
        return conn

    engine = sqlalchemy.create_engine(
        "mysql+pymysql://",
        creator=getconn,
    )
    return engine

def main():
    report_results = {}
    database_queries = json.load(open("queries.json"))
    with Connector(refresh_strategy="LAZY") as connector:
        with open("config.csv", newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                server_key = row.get("instance_connection_name", f"Server_{row.get('connection_name', 'default')}")
                report_results[server_key] = {}
                engine = connect_with_connector_db_auth(connector,row)
                mysql_connection = engine.raw_connection()
                cursor = mysql_connection.cursor(pymysql.cursors.DictCursor)

                for category, queries in database_queries.items():
                    if category not in report_results[server_key]:
                        report_results[server_key][category] = {}
                    for key, sql in queries.items():
                        cursor.execute(sql)
                        query_data = cursor.fetchall()
                        report_results[server_key][category][key] = query_data
                cursor.close()
                mysql_connection.close()
    with open("database_health_report.json", "w") as f:
        json.dump(report_results, f, indent=4, default=str)

if __name__ == "__main__":
    main()




