import json
import concurrent.futures
from datetime import datetime, timezone, timedelta
from google.cloud import monitoring_v3
from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp

# Metric mapping dictionary
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

ALIGNER_NAMES = {
    "ALIGN_MEAN": "mean",
    "ALIGN_MAX":  "max"
}

def extract_point_value(point):
    """Dynamically pull numeric value regardless of int64, double, or distribution type."""
    val_type = point.value._pb.WhichOneof("value")
    if val_type == "double_value":
        return point.value.double_value
    elif val_type == "int64_value":
        return float(point.value.int64_value)
    elif val_type == "distribution_value":
        return point.value.distribution_value.mean
    return 0.0

def fetch_metric(client, project_id, instance_id, metric_key, metric_type, start_dt, end_dt, aligner):
    start_time = Timestamp()
    start_time.FromDatetime(start_dt)
    end_time = Timestamp()
    end_time.FromDatetime(end_dt)

    interval = monitoring_v3.TimeInterval(
        start_time=start_time,
        end_time=end_time,
    )

    aligner_enum = getattr(monitoring_v3.Aggregation.Aligner, aligner)

    aligner = f'monitoring_v3.Aggregation.Aligner.{aligner}'
    aggregation = monitoring_v3.Aggregation(
        alignment_period=Duration(seconds=86400),
        per_series_aligner=aligner_enum,
    )

    filter_expr = (
        f'metric.type="{metric_type}" AND '
        f'resource.labels.database_id="{project_id}:{instance_id}"'
    )

    request = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project_id}",
        filter=filter_expr,
        interval=interval,
        aggregation=aggregation,
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    )

    results = []
    try:
        response = client.list_time_series(request=request)
        for series in response:
            for point in series.points:
                raw_val = extract_point_value(point)
                
                if "utilization" in metric_key:
                    formatted_val = round(raw_val * 100, 2)
                else:
                    formatted_val = round(raw_val, 2)

                results.append({
                    "instance_id": instance_id,
                    "metric": metric_key,
                    "timestamp": point.interval.end_time.isoformat(),
                    "value": formatted_val
                })
    except Exception as e:
        print(f"Error fetching {metric_key} for {instance_id}: {e}")

    return results



def main():
    project_id = "chtp-g3-staging-1-7fa98a9d"
    instances = ["target-restore"]

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=24)

    aligner_list = ['ALIGN_MEAN','ALIGN_MAX']

    client = monitoring_v3.MetricServiceClient()
    consolidated_output = {}

    for inst in instances:
        consolidated_output[inst] = {
            "timestamp": None,
            "metrics": {}
        }
        
        for m_key, m_type in METRIC_TYPES.items():
            for aligner in aligner_list:
                res = fetch_metric(client, project_id, inst, m_key, m_type, start_dt, end_dt, aligner)
                if res:
                    if not consolidated_output[inst]["timestamp"]:
                        consolidated_output[inst]["timestamp"] = res[0]["timestamp"]

                    if m_key not in consolidated_output[inst]["metrics"]:
                        consolidated_output[inst]["metrics"][m_key] = {}

                    clean_aligner = ALIGNER_NAMES.get(aligner, aligner.lower())
                    consolidated_output[inst]["metrics"][m_key][clean_aligner] = res[0]["value"]

    print(json.dumps(consolidated_output, indent=2))


if __name__ == "__main__":
    main()