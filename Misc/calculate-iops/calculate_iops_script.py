import csv
import sys

# 1. Reference Data: Machine Type vCPU Checkpoints (X)
# Formatted as a list of tuples: (vCPU_checkpoint, (Read_IOPS, Write_IOPS))
MACHINE_LIMITS = {
    "SSD": {
        "ENTERPRISE": {
            "SHARED_CORE": [(0, (12000, 10000))], 
            "DEDICATED_CORE": [
                (1, (12000, 10000)),
                (2, (15000, 15000)),
                (16, (25000, 25000)),
                (32, (60000, 60000)),
                (64, (100000, 100000))
            ]
        },
        "ENTERPRISE_PLUS": {
            "N2": [
                (2, (15000, 15000)),
                (16, (25000, 25000)),
                (32, (60000, 60000)),
                (64, (100000, 80000))
            ]
        }
    },
    "HDD": {
        "ENTERPRISE": {
            "SHARED_CORE": [(0, (1000, 10000))],
            "DEDICATED_CORE": [
                (1, (1000, 10000)),
                (2, (3000, 15000)),
                (8, (5000, 15000)),
                (16, (7500, 15000))
            ]
        }
    }
}

def determine_machine_family(machine_tier):
    tier = machine_tier.lower()
    if "shared" in tier or "f1" in tier or "g1" in tier: return "SHARED_CORE"
    if "n4" in tier: return "N4"
    if "n2" in tier: return "N2"
    if "c4a" in tier: return "c4A"
    return "DEDICATED_CORE"

def extract_vcpu(row, machine_tier):
    if row.get("vcpu") and row["vcpu"].strip().isdigit():
        return int(row["vcpu"])
    
    parts = machine_tier.split('-')
    for part in parts:
        if part.isdigit():
            return int(part)
            
    return 1 # Fallback to 1 vCPU

def get_machine_limit(disk_type, edition, machine_family, vcpu):
    try:
        checkpoints = MACHINE_LIMITS[disk_type][edition][machine_family]
        
        inherited_vcpu = None
        max_r, max_w = None, None
        
        for cp_vcpu, limits in checkpoints:
            if vcpu >= cp_vcpu:
                inherited_vcpu = cp_vcpu
                max_r, max_w = limits
            else:
                break
                
        return inherited_vcpu, max_r, max_w
    except KeyError:
        return None, None, None

def calculate_iops(row):
    # Parse inputs
    machine_tier = row.get("Machine-tier", "")
    machine_family = row.get("machine_family", determine_machine_family(machine_tier))
    vcpu = extract_vcpu(row, machine_tier)
    
    disk = row.get("disk type", "").upper().replace("PD_", "")
    disk_type = "SSD" if "SSD" in disk else ("HDD" if "HDD" in disk else disk)
    
    storage_gb = int(row.get("Disk size", 0))
    edition = row.get("edition", "ENTERPRISE").upper()
    
    prov_read = int(row.get("provisioned_read", 0)) if row.get("provisioned_read") else None
    prov_write = int(row.get("provisioned_write", 0)) if row.get("provisioned_write") else None

    print(f"### Evaluating Instance: {machine_tier} ({vcpu} vCPU) | {disk_type} | {storage_gb}GB | {edition}")

    is_user_provisioned = machine_family in ["N4", "c4A"]

    # Step 1: Machine Limit (X)
    if is_user_provisioned:
        print("1. **Step 1: Machine Limit (X)**: Skipped - User Provisioned (N4/c4A).")
        max_r, max_w = None, None
    else:
        inherited_vcpu, max_r, max_w = get_machine_limit(disk_type, edition, machine_family, vcpu)
        if max_r is None:
            print(f"**Error:** Could not find Machine Limit for {disk_type} | {edition} | {machine_family}\n")
            return
            
        tier_label = f"{inherited_vcpu} vCPU tier" if inherited_vcpu > 0 else "Shared Core tier"
        print(f"1. **Step 1: Machine Limit (X)**: Inherited {tier_label}. Resulting limits - Read X: {max_r:,} | Write X: {max_w:,}.")

    # Step 2: Capacity Calculation (Y)
    if is_user_provisioned:
        cap_r = prov_read if prov_read is not None else 0
        cap_w = prov_write if prov_write is not None else 0
        print(f"2. **Step 2: Capacity Calculation (Y)**: Explicitly provisioned. Uncapped Read Y: {cap_r:,} | Uncapped Write Y: {cap_w:,}.")
    elif disk_type == "SSD":
        cap_r = cap_w = 6000 + (30 * storage_gb)
        print(f"2. **Step 2: Capacity Calculation (Y)**: `6000 + (30 * {storage_gb})`. Uncapped Read Y: {cap_r:,} | Uncapped Write Y: {cap_w:,}.")
    elif disk_type == "HDD":
        if storage_gb <= 100:
            cap_r = 75
            cap_w = 150
            calc_msg = "Storage <= 100GB"
        else:
            cap_r = int(storage_gb * 0.75)
            cap_w = int(storage_gb * 1.5)
            calc_msg = f"Read `GB * 0.75`, Write `GB * 1.5`"
        print(f"2. **Step 2: Capacity Calculation (Y)**: {calc_msg}. Uncapped Read Y: {cap_r:,} | Uncapped Write Y: {cap_w:,}.")

    # Step 3: Final Resolution
    if is_user_provisioned:
        final_r, final_w = cap_r, cap_w
        print(f"3. **Step 3: Final Resolution**: N4/c4A ignores Machine Limits. Final IOPS relies entirely on User Provisioned Capacity (Y).")
    else:
        final_r = min(cap_r, max_r)
        final_w = min(cap_w, max_w)
        r_logic = "capped by Machine Limit (X)" if cap_r > max_r else "dictated by Disk Capacity (Y)"
        w_logic = "capped by Machine Limit (X)" if cap_w > max_w else "dictated by Disk Capacity (Y)"
        print(f"3. **Step 3: Final Resolution**: min(Y, X). Read IOPS is {r_logic} ({final_r:,}). Write IOPS is {w_logic} ({final_w:,}).")

    # Step 4: Summary Table
    print("\n4. **Summary Table**\n")
    print("| Metric | Disk Capacity (Y) | Machine Limit (X) | Final Effective IOPS |")
    print("|---|---|---|---|")
    
    x_read_str = f"{max_r:,}" if max_r is not None else "N/A"
    x_write_str = f"{max_w:,}" if max_w is not None else "N/A"
    
    print(f"| Read IOPS | {cap_r:,} | {x_read_str} | **{final_r:,}** |")
    print(f"| Write IOPS | {cap_w:,} | {x_write_str} | **{final_w:,}** |\n")
    print("-" * 65 + "\n")


if __name__ == "__main__":
    # Default filename, can be overridden by command line argument
    filename = "instances.csv"
    
    if len(sys.argv) > 1:
        filename = sys.argv[1]

    try:
        with open(filename, mode='r', encoding='utf-8') as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                calculate_iops(row)
    except FileNotFoundError:
        print(f"Error: Could not find the file '{filename}'.")
        print("Please ensure the file exists or pass the correct path: python script.py path/to/your/file.csv")