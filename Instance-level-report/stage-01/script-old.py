import csv
import json
import subprocess
import urllib.parse
import urllib.request

# ---------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------
CONFIG_FILE = "config.csv"
START_TIME = "2026-08-20T11:30:00Z"
END_TIME = "2026-08-20T23:00:00Z"

# Metrics to fetch: (metric_key, metric_path, is_percentage)
METRIC_CONFIGS = [
    ("CPU_Utilization", "cloudsql.googleapis.com/database/cpu/utilization", True),
    ("Memory_Utilization", "cloudsql.googleapis.com/database/memory/utilization", True),
    ("Disk_Utilization", "cloudsql.googleapis.com/database/disk/utilization", True),
    ("Active_Connections", "cloudsql.googleapis.com/database/network/connections", False),
]


def get_gcloud_auth_token():
    """Fetches an OAuth access token using gcloud CLI."""
    try:
        return (
            subprocess.check_output(
                ["gcloud", "auth", "print-access-token"], stderr=subprocess.DEVNULL
            )
            .decode("utf-8")
            .strip()
        )
    except Exception as e:
        raise RuntimeError(f"Failed to fetch access token: {e}")


def query_metric(project_id, instance_id, metric_path, aligner, is_percentage, token):
    """Executes a query against the Cloud Monitoring API for a specific aligner."""
    filter_str = (
        f'metric.type="{metric_path}" AND '
        f'resource.labels.database_id="{project_id}:{instance_id}"'
    )

    params = {
        "filter": filter_str,
        "interval.startTime": START_TIME,
        "interval.endTime": END_TIME,
        "aggregation.alignmentPeriod": "86400s",
        "aggregation.perSeriesAligner": aligner,
    }

    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            points = data.get("timeSeries", [{}])[0].get("points", [])
            
            if points:
                val_obj = points[0]["value"]
                # Active connections are stored as int64Value; percentage metrics as doubleValue
                raw_val = val_obj.get("doubleValue") if "doubleValue" in val_obj else val_obj.get("int64Value")
                
                if raw_val is not None:
                    raw_val = float(raw_val)
                    return round(raw_val * 100, 2) if is_percentage else round(raw_val, 2)
    except Exception:
        pass

    return None


def main():
    token = get_gcloud_auth_token()
    results = []

    with open(CONFIG_FILE, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            project_id = row["project_id"].strip()
            instance_id = row["instance_id"].strip()

            instance_entry = {
                "project_id": project_id,
                "instance_id": instance_id,
                "window_start": START_TIME,
                "window_end": END_TIME,
                "metrics": {}
            }

            for key, path, is_percentage in METRIC_CONFIGS:
                max_val = query_metric(project_id, instance_id, path, "ALIGN_MAX", is_percentage, token)
                avg_val = query_metric(project_id, instance_id, path, "ALIGN_MEAN", is_percentage, token)

                instance_entry["metrics"][key] = {
                    "max": max_val,
                    "avg": avg_val
                }

            results.append(instance_entry)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()