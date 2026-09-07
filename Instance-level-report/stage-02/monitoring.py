import json
from datetime import datetime, timezone, timedelta
from google.cloud import monitoring_v3

# Your metric definitions remain exactly the same
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

def extract_typed_value(typed_value):
    """Safely extracts numeric values from the MQL TypedValue object."""
    val_type = typed_value._pb.WhichOneof("value")
    if val_type == "double_value":
        return typed_value.double_value
    elif val_type == "int64_value":
        return float(typed_value.int64_value)
    return 0.0

def fetch_mql_metric(client, project_id, instance_id, metric_key, metric_type):
    """Executes an MQL query to fetch Mean, Max, P95, and P99 natively."""
    
    # MQL requires the exact value column name (e.g., 'value.utilization' or 'value.read_ops_count')
    # We dynamically extract this suffix from the end of your metric string.
    metric_suffix = metric_type.split('/')[-1]
    value_col = f"value.{metric_suffix}"
    
    # Construct the MQL query string for the last 24 hours
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

    # We use QueryTimeSeriesRequest instead of ListTimeSeriesRequest
    request = monitoring_v3.QueryTimeSeriesRequest(
        name=f"projects/{project_id}",
        query=mql_query
    )


    try:
        response = client.query_time_series(request=request)
        # MQL returns an iterable of TimeSeriesData
        for series_data in response:
            if not series_data.point_data:
                continue
            
            # The 'every 24h' command ensures there is exactly ONE point returned
            point = series_data.point_data[0]
            
            # Extract the 4 values in the exact order we defined in our group_by array
            mean_val = extract_typed_value(point.values[0])
            max_val  = extract_typed_value(point.values[1])
            p95_val  = extract_typed_value(point.values[2])
            p99_val  = extract_typed_value(point.values[3])
            
            # Convert utilization decimals (0.08) to percentages (8.00)
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
        print(f"Error fetching MQL for {metric_key}: {e}")

    # Fallback if the metric returned no data during the window
    return {"mean": None, "max": None, "p95": None, "p99": None}


def main():
    project_id = "data-analyst-504209"
    instances = ["csql-mysql-db"]

    client = monitoring_v3.QueryServiceClient()
    consolidated_output = {}

    for inst in instances:
        consolidated_output[inst] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": {}
        }
        
        for m_key, m_type in METRIC_TYPES.items():
            metrics_data = fetch_mql_metric(client, project_id, inst, m_key, m_type)
            consolidated_output[inst]["metrics"][m_key] = metrics_data

    print(json.dumps(consolidated_output, indent=2))

if __name__ == "__main__":
    main()