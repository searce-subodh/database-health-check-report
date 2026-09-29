import numpy as np
from google.cloud import monitoring_v3

# Helper function to extract the value from GCP's TypedValue object
def extract_typed_value(typed_value):
    if typed_value.HasField("double_value"):
        return typed_value.double_value
    elif typed_value.HasField("int64_value"):
        return typed_value.int64_value
    return 0.0

def fetch_mql_metric_fixed(client, project_id, instance_id, metric_key, metric_type):
    metric_suffix = metric_type.split("/")[-1]
    value_col = f"value.{metric_suffix}"

    # Single query to fetch a time series of 1-minute points
    mql_query = f"""
    fetch cloudsql_database
    | metric '{metric_type}'
    | filter (resource.database_id == '{project_id}:{instance_id}')
    | within 24h
    | group_by [], [val: sum({value_col})]
    | every 1m
    """
    
    def scale_value(val):
        if val is None: return None
        if "utilization" in metric_key: return val * 100
        elif "bytes_used" in metric_key: return val / (1024 ** 3)
        elif "ops" in metric_key: return val / 60
        return val

    result = {"mean": None, "p95": None, "p99": None, "max": None}
    data_points = []

    try:
        request = monitoring_v3.QueryTimeSeriesRequest(
            name=f"projects/{project_id}", query=mql_query
        )
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

if __name__ == "__main__":
    PROJECT_ID = "ata-analyst-504209"
    INSTANCE_ID = "csql-psql-db"
    
    COMMON_METRICS = {
        "cpu_utilization": "cloudsql.googleapis.com/database/cpu/utilization",
        "memory_utilization": "cloudsql.googleapis.com/database/memory/utilization",
        "disk_utilization": "cloudsql.googleapis.com/database/disk/utilization",
        "disk_read_ops": "cloudsql.googleapis.com/database/disk/read_ops_count",
        "disk_write_ops": "cloudsql.googleapis.com/database/disk/write_ops_count",
        "disk_bytes_used": "cloudsql.googleapis.com/database/disk/bytes_used",
    }

    # MYSQL_METRICS = {
    #     "connections": "cloudsql.googleapis.com/database/network/connections",
    # }

    POSTGRES_METRICS = {
        "connections": "cloudsql.googleapis.com/database/postgresql/num_backends",
    }

    # Combine common and postgres metrics for this test run
    active_metrics = {**COMMON_METRICS, **POSTGRES_METRICS}

    # 3. Initialize the client
    client = monitoring_v3.MetricServiceClient()
    
    print(f"Fetching 24h data for Project: {PROJECT_ID} | Instance: {INSTANCE_ID}...\n")
    print("-" * 65)
    print(f"{'METRIC':<20} | {'MEAN':<8} | {'P95':<8} | {'P99':<8} | {'MAX':<8}")
    print("-" * 65)

    # 4. Loop through each metric and print the results in a table format
    for metric_key, metric_type in active_metrics.items():
        metrics = fetch_mql_metric_fixed(client, PROJECT_ID, INSTANCE_ID, metric_key, metric_type)
        
        # Handle cases where no data was returned (e.g., empty dictionary)
        mean_val = metrics.get('mean') if metrics.get('mean') is not None else "N/A"
        p95_val = metrics.get('p95') if metrics.get('p95') is not None else "N/A"
        p99_val = metrics.get('p99') if metrics.get('p99') is not None else "N/A"
        max_val = metrics.get('max') if metrics.get('max') is not None else "N/A"
        
        print(f"{metric_key:<20} | {str(mean_val):<8} | {str(p95_val):<8} | {str(p99_val):<8} | {str(max_val):<8}")
    
    print("-" * 65)