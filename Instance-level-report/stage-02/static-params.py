import json
import re
from googleapiclient import discovery
import google.auth

TIER_MAP = {
    "db-f1-micro": (1, "0.614 GB"),
    "db-g1-small": (1, "1.7 GB"),
}

def parse_compute_specs(tier: str) -> tuple:
    if not tier:
        return ("N/A", "N/A")
        
    if tier in TIER_MAP:
        vcpu, mem = TIER_MAP[tier]
        return (str(vcpu), mem)

    # Handles custom tiers like db-custom-4-16384
    custom_match = re.search(r"-(\d+)-(\d+)$", tier)
    if custom_match:
        vcpu = custom_match.group(1)
        memory_mb = int(custom_match.group(2))
        memory_gb = round(memory_mb / 1024, 2)
        return (vcpu, f"{memory_gb} GB")

    suffix_match = re.search(r"-(\d+)$", tier)
    if suffix_match:
        return (suffix_match.group(1), "N/A")

    return ("N/A", "N/A")


def get_cloudsql_summary(project_id: str, instance_name: str) -> dict:
    # Initialize the Cloud SQL Admin client
    service = discovery.build('sqladmin', 'v1', cache_discovery=False)

    # 1. Fetch Instance Metadata
    try:
        req = service.instances().get(project=project_id, instance=instance_name)
        inst = req.execute()
    except Exception as e:
        return {"Error": f"Failed to fetch instance details: {str(e)}"}

    settings = inst.get("settings", {})

    # Parse compute
    vcpu, memory = parse_compute_specs(settings.get("tier"))

    # Format Storage string
    disk_type = settings.get("dataDiskType", "").replace("PD_", "")
    storage_str = f"{settings.get('dataDiskSizeGb', 'N/A')} GB {disk_type}".strip()

    # Format Availability
    availability = settings.get("availabilityType", "N/A").title()

    # Read Replicas
    replica_count = len(inst.get("replicaNames", []))

    # 2. Fetch Last Backup Time
    last_backup_time = "No backups found"
    try:
        backup_req = service.backupRuns().list(
            project=project_id, 
            instance=instance_name, 
            maxResults=1
        )
        backups = backup_req.execute().get("items", [])
        if backups and "windowStartTime" in backups[0]:
            last_backup_time = backups[0]["windowStartTime"]
    except Exception:
        pass

    return {
        "Engine": inst.get("databaseVersion", "N/A").replace("_", " "),
        "Edition": settings.get("edition", "ENTERPRISE"),
        "CPU": vcpu,
        "Memory": memory,
        "Storage": storage_str,
        "Availability": availability,
        "Read Replica count": replica_count,
        "Last Backup time": last_backup_time,
    }


if __name__ == "__main__":
    # Automatically gets your active project ID in Cloud Shell
    try:
        credentials, project_id = google.auth.default()
    except Exception:
        project_id = "data-analyst-504209" # Fallback to your project

    INSTANCE_NAME = "test-mysql"
    
    print(f"Fetching details for {INSTANCE_NAME} in project {project_id}...\n")
    
    details = get_cloudsql_summary(project_id, INSTANCE_NAME)
    
    for key, value in details.items():
        print(f"{key}: {value}")