gcloud sql instances describe test-mysql \
  --format="value(databaseVersion, settings.edition)"




-- TIER  (CPU, Memory)
import json
import re
import subprocess

# 1. Map for specific predefined tiers (vCPUs, RAM in GB)
TIER_MAP = {
    # Shared Core Tiers
    "db-f1-micro": (1, 0.614),
    "db-g1-small": (1, 1.7),

    # N2 Performance-Optimized Tiers
    "db-perf-optimized-N-2": (2, 16.0),
    "db-perf-optimized-N-4": (4, 32.0),
    "db-perf-optimized-N-8": (8, 64.0),
    "db-perf-optimized-N-16": (16, 128.0),
    "db-perf-optimized-N-32": (32, 256.0),
    "db-perf-optimized-N-48": (48, 384.0),
    "db-perf-optimized-N-64": (64, 512.0),
    "db-perf-optimized-N-80": (80, 640.0),
    "db-perf-optimized-N-96": (96, 768.0),
    "db-perf-optimized-N-128": (128, 864.0),

    # C4A Highmem Tiers
    "db-c4a-highmem-2": (2, 16.0),
    "db-c4a-highmem-4": (4, 32.0),
    "db-c4a-highmem-8": (8, 64.0),
    "db-c4a-highmem-16": (16, 128.0),
    "db-c4a-highmem-32": (32, 256.0),
    "db-c4a-highmem-48": (48, 384.0),
    "db-c4a-highmem-64": (64, 512.0),
    "db-c4a-highmem-72": (72, 576.0),
}


def get_sql_instance_resources(instance_name):
    # Fetch tier from gcloud describe command
    cmd = f"gcloud sql instances describe {instance_name} --format='value(settings.tier)'"
    try:
        tier = subprocess.check_output(cmd, shell=True, text=True).strip()
    except subprocess.CalledProcessError as e:
        return {"error": f"Failed to fetch details for instance {instance_name}: {str(e)}"}

    if not tier:
        return {"error": f"No tier returned for instance {instance_name}"}

    # Case 1: Predefined Tiers from Map
    if tier in TIER_MAP:
        vcpu, ram_gb = TIER_MAP[tier]
        return {
            "tier": tier,
            "type": "predefined",
            "vcpu": vcpu,
            "ram_gb": ram_gb
        }

    # Case 2: N4 Custom Tiers (pattern: db-custom-N4-<vcpu>-<ram_mb>)
    n4_match = re.match(r"^db-custom-N4-(\d+)-(\d+)$", tier)
    if n4_match:
        vcpu = int(n4_match.group(1))
        ram_mb = int(n4_match.group(2))
        return {
            "tier": tier,
            "type": "custom_n4",
            "vcpu": vcpu,
            "ram_gb": round(ram_mb / 1024, 2)
        }

    # Case 3: Standard Custom Tiers (pattern: db-custom-<vcpu>-<ram_mb>)
    custom_match = re.match(r"^db-custom-(\d+)-(\d+)$", tier)
    if custom_match:
        vcpu = int(custom_match.group(1))
        ram_mb = int(custom_match.group(2))
        return {
            "tier": tier,
            "type": "custom",
            "vcpu": vcpu,
            "ram_gb": round(ram_mb / 1024, 2)
        }

    # Case 4: Fallback for Standard Named Predefined Tiers (e.g. db-n1-standard-4)
    suffix_match = re.search(r"-(\d+)$", tier)
    if suffix_match:
        vcpu = int(suffix_match.group(1))
        return {
            "tier": tier,
            "type": "predefined_parsed",
            "vcpu": vcpu,
            "ram_gb": "Check API for RAM"
        }

    return {"tier": tier, "type": "unknown", "error": "Unrecognized tier layout"}


# Usage Example:
if __name__ == "__main__":
    INSTANCE_NAME = "YOUR_INSTANCE_NAME"
    result = get_sql_instance_resources(INSTANCE_NAME)
    print(json.dumps(result, indent=2))



gcloud sql instances describe test-mysql \
  --format="value(settings.dataDiskSizeGb, settings.dataDiskType)"


  gcloud sql instances describe test-mysql \
  --format="value(settings.availabilityType)"

gcloud sql instances describe test-mysql --format="value(replicaNames,replicaNames.len())"


cloud sql backups list --instance=test-mysql \
  --sort-by=~windowStartTime \
  --limit=1 \
  --format="value(windowStartTime)"