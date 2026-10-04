from datetime import datetime
import json
import os
import sys
import html

# ─────────────────────────────────────────────
# LOAD INPUT JSON FILES (VIA COMMAND LINE)
# ─────────────────────────────────────────────

if len(sys.argv) < 2:
    print("Usage Error!")
    print("Please provide JSON file paths or a directory containing JSON files:")
    sys.exit(1)

report_data = {}

def load_json_file(filepath):
    """Parses individual per-instance JSON files and merges them by instance key."""
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                # Handles single-instance JSON output from script.py
                if "instance_name" in data:
                    inst_name = data["instance_name"]
                    project_id = data.get("project_id", "")
                    unique_key = f"{project_id}:{inst_name}" if project_id else inst_name
                    report_data[unique_key] = data
                else:
                    # Fallback for multi-instance root structure
                    for k, v in data.items():
                        if isinstance(v, dict) and "health_checks" in v:
                            report_data[k] = v
    except Exception as e:
        print(f" Warning: Failed to load '{filepath}': {e}")

# Process command-line inputs
for arg in sys.argv[1:]:
    if os.path.isdir(arg):
        for filename in sorted(os.listdir(arg)):
            if filename.endswith(".json"):
                load_json_file(os.path.join(arg, filename))
    elif os.path.isfile(arg):
        load_json_file(arg)

if not report_data:
    print("Error: No valid database report JSON data found in the provided inputs.")
    sys.exit(1)

print(f"Loaded {len(report_data)} instance(s) into report generator.")

generated_at = datetime.now().strftime("%d %b %Y, %I:%M %p")

# ─────────────────────────────────────────────
# BUILD NAVIGATION DATA
# ─────────────────────────────────────────────

nav_data = {}
for unique_key, data in report_data.items():
    specs  = data.get("provisioned_specs", {})
    engine = specs.get("Engine", "")
    db_type = (
        "PostgreSQL" if "POSTGRES" in engine.upper()
        else "MySQL"  if "MYSQL"   in engine.upper()
        else "Unknown"
    )
    
    instance_name = data.get("instance_name", unique_key)
    project_id    = data.get("project_id", "")
    
    display_label = f"{instance_name} ({project_id})" if project_id else instance_name
    
    nav_data[unique_key] = {
        "type": db_type,
        "label": display_label,
        "project_id": project_id if project_id else "No Project"
    }

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def row_class(row):
    if not isinstance(row, dict):
        return ""
    vals = " ".join(str(v) for v in row.values())
    if any(x in vals for x in ["EXPIRED", "NO PASSWORD", "FAILED"]):
        return "row-critical"
    if any(x in vals for x in ["NEVER EXPIRES", "WARNING"]):
        return "row-warning"
    return ""

def format_clean_title(key):
    words = key.replace("_", " ").title()
    return (words
        .replace("Cpu", "CPU")
        .replace("Wal", "WAL")
        .replace("Sql", "SQL")
        .replace("Db",  "DB"))

def build_paginated_table(rows, table_id, metric_name=""):
    if not rows or not isinstance(rows, list):
        return '<p class="no-issues">No issues or records flagged.</p>'

    if len(rows) == 1 and isinstance(rows[0], dict):
        first_row = rows[0]
        if first_row.get("message") == "0 records returned":
            return '<p class="no-issues">No issues or records flagged.</p>'

    if len(rows) > 0 and isinstance(rows[0], dict):
        first_row = rows[0]
        if "error" in first_row or first_row.get("status") == "ERROR" or "Result unavailable" in str(first_row.get("message")):
            header_title = format_clean_title(metric_name) if metric_name else "Metric Details"
            html_table  = f'<div class="table-wrapper" id="wrapper-{table_id}">'
            html_table += f'<table id="tbl-{table_id}"><thead><tr>'
            html_table += f'<th>{header_title}</th>'
            html_table += "</tr></thead><tbody>"
            html_table += "<!-- Empty body due to query execution or permission error -->"
            html_table += "</tbody></table></div>"
            return html_table

    headers = list(rows[0].keys())
    html_table  = f'<div class="table-wrapper" id="wrapper-{table_id}">'
    html_table += f'<table id="tbl-{table_id}"><thead><tr>'
    html_table += "".join([f"<th>{h}</th>" for h in headers])
    html_table += "</tr></thead><tbody>"

    for i, row in enumerate(rows):
        rc    = row_class(row)
        style = "" if i < 10 else ' style="display:none"'
        html_table += f'<tr class="page-row {rc}"{style}>'
        for h in headers:
            val = str(row.get(h)) if row.get(h) is not None else "N/A"
            if any(x in val for x in ["EXPIRED", "NO PASSWORD", "FAILED"]):
                val = f'<span class="alert-badge">{val}</span>'
            elif any(x in val for x in ["NEVER EXPIRES", "WARNING"]):
                val = f'<span class="warn-badge">{val}</span>'
            html_table += f"<td>{val}</td>"
        html_table += "</tr>"

    html_table += "</tbody></table>"

    total_pages = (len(rows) + 9) // 10
    if total_pages > 1:
        html_table += f"""
        <div class="pagination">
            <button onclick="changePage('{table_id}', -1)">&lt; Prev</button>
            <span id="page-info-{table_id}">Page 1 of {total_pages}</span>
            <button onclick="changePage('{table_id}', 1)">Next &gt;</button>
        </div>"""

    html_table += "</div>"
    return html_table

# ─────────────────────────────────────────────
# HTML HEAD + STYLES
# ─────────────────────────────────────────────

first_instance_data = list(report_data.values())[0] if report_data else {}
report_window = first_instance_data.get("report_window", {})
period_from   = report_window.get("from", generated_at)
period_to     = report_window.get("to",   generated_at)

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Database Health Check Report</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-main: #f4f5f7;           
            --bg-card: #ffffff;           
            --border-color: #dfe1e6;      
            --text-primary: #172b4d;      
            --text-secondary: #5e6c84;    
            --track-bg: #ebecf0;          
            --c-mean: #3b82f6;     
            --c-p95: #10b981;      
            --c-p99: #f97316;      
            --c-max: #8b5cf6;      
            --color-green: #1e8e3e;
            --color-yellow: #f9ab00;
            --color-red: #d93025;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: 'Inter', -apple-system, sans-serif; 
            font-variant-numeric: tabular-nums; 
            background: var(--bg-main); 
            color: var(--text-primary); 
        }}
        h1, .section-title, .category-title, .instance-title {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}
        #topbar {{
            position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
            background: #0f172a; color: white;
            padding: 12px 24px; display: flex; align-items: center;
            gap: 16px; flex-wrap: wrap; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }}
        #topbar h1 {{ font-size: 1.1em; color: white; white-space: nowrap; }}
        #topbar select {{
            padding: 6px 10px; border-radius: 6px; border: none;
            background: #1e293b; color: white; font-size: 0.85em;
            cursor: pointer; min-width: 160px; font-family: inherit;
        }}
        #topbar select:focus {{ outline: 2px solid #3b82f6; }}
        .topbar-timestamp {{
            margin-left: auto; font-size: 0.8em; color: #94a3b8;
            white-space: nowrap; text-align: right; line-height: 1.6;
        }}
        #content {{ margin-top: 72px; padding: 24px; max-width: 1400px; margin-left: auto; margin-right: auto; }}
        
        .specs-grid {{
            display: flex; flex-wrap: nowrap; overflow-x: auto;
            gap: 10px; margin-bottom: 16px; padding-bottom: 6px;
        }}
        .spec-item {{
            flex: 1 1 auto; min-width: 100px; white-space: nowrap;
            background: #f8fafc; border-radius: 6px; padding: 10px 14px;
            border-left: 4px solid #2563eb;
        }}
        .spec-label {{ font-size: 0.68em; color: #64748b; text-transform: uppercase; margin-bottom: 4px; font-weight: 600; }}
        .spec-value {{ font-size: 0.95em; font-weight: 700; color: #0f172a; }}

        .instance-block {{ margin-bottom: 40px; }}
        .instance-title {{
            padding: 16px 24px; background: #e2e8f0; border-radius: 8px 8px 0 0; 
            border-left: 5px solid #2563eb; font-family: 'Montserrat', sans-serif; font-size: 1.25em;
        }}
        .breadcrumb-project {{ font-weight: 500; color: #64748b; }}
        .breadcrumb-separator {{ margin: 0 8px; color: #94a3b8; font-weight: 400; }}
        .breadcrumb-instance {{ font-weight: 700; color: #0f172a; }}
        .section-card {{ background: var(--bg-card); border-radius: 0 0 8px 8px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(9, 30, 66, 0.05); }}
        .section-title {{ font-size: 1.15em; color: #0f172a; margin-bottom: 24px; padding-bottom: 12px; border-bottom: 2px solid var(--border-color); }}

        .category-title {{ font-size: 1em; color: #2563eb; margin-top: 16px; padding: 8px 0; border-bottom: 1px solid #e2e8f0; cursor: pointer; display: flex; justify-content: space-between; }}
        .category-title:hover {{ color: #1d4ed8; }}
        .metric-title {{ font-size: 0.9em; font-weight: 600; color: #334155; margin: 14px 0 6px; }}

        table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 0.85em; }}
        th {{ background: #334155; color: white; padding: 9px 12px; text-align: left; font-family: 'Montserrat', sans-serif; font-weight: 600; }}
        td {{ border: 1px solid #e2e8f0; padding: 8px 12px; word-break: break-word; }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        tr.row-critical td {{ background: #fee2e2 !important; }}
        tr.row-warning  td {{ background: #fef9c3 !important; }}
        tr:hover td {{ background: #eff6ff !important; }}

        .pagination {{ display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 0.75em; color: #64748b; }}
        .pagination button {{ padding: 4px 12px; border-radius: 4px; border: 1px solid #cbd5e1; background: white; cursor: pointer; font-weight: 600; }}
        .pagination button:hover {{ background: #e2e8f0; }}
        .alert-badge {{ font-weight: 700; color: #991b1b; background: #fca5a5; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
        .warn-badge  {{ font-weight: 700; color: #854d0e; background: #fde047; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
        .no-issues   {{ color: #16a34a; font-style: italic; font-weight: 500; padding: 6px 0 12px 0; }}
        .error-box   {{ color: #7f1d1d; background: #fee2e2; border-left: 4px solid #b91c1c; padding: 12px 16px; border-radius: 4px; margin: 8px 0; }}
        .hidden      {{ display: none !important; }}

        /* =========================================
           DASHBOARD UI STYLES
           ========================================= */
        .dashboard-wrapper {{ 
            background-color: var(--bg-card); /* Update 2: White Background */
            width: 100%; /* Update 1: Spread full width */
            margin-bottom: 24px; 
            margin-top: 16px;
            display: flex; flex-direction: column; gap: 24px; 
        }}
        .dashboard-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 16px; }}
        
        /* Update 3: Accordion Title Toggle */
        .dash-title-group {{ display: flex; align-items: center; gap: 8px; cursor: pointer; }}
        .dash-title-group:hover h2, .dash-title-group:hover .dash-arrow {{ color: #1d4ed8; }}
        .dash-arrow {{ font-size: 1.1em; color: #2563eb; transition: transform 0.2s; }}
        .dashboard-header h2 {{ font-size: 1.15em; font-weight: 700; margin: 0; color: #0f172a; text-transform: uppercase; letter-spacing: 0.5px; font-family: 'Montserrat', sans-serif; }}
        
        .header-controls {{ display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }}
        .global-toggles {{ display: none; align-items: center; gap: 4px; background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 20px; padding: 4px 8px; }}
        .dashboard-wrapper.view-all .global-toggles {{ display: flex; }}
        .toggle-btn {{ background-color: transparent; border: none; color: var(--text-primary); border-radius: 16px; padding: 6px 12px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s ease; display: flex; align-items: center; gap: 6px; }}
        .toggle-btn::before {{ content: ''; display: block; width: 8px; height: 8px; border-radius: 50%; }}
        .toggle-btn[data-metric="Mean"]::before {{ background-color: var(--c-mean); }}
        .toggle-btn[data-metric="P95"]::before  {{ background-color: var(--c-p95); }}
        .toggle-btn[data-metric="P99"]::before  {{ background-color: var(--c-p99); }}
        .toggle-btn[data-metric="Max"]::before  {{ background-color: var(--c-max); }}
        .toggle-btn:not(.active) {{ opacity: 0.4; filter: grayscale(100%); }}
        .toggle-btn.active {{ background-color: var(--bg-main); }}

        .controls-container {{ display: flex; align-items: center; gap: 12px; }}
        .controls-container label {{ font-size: 14px; font-weight: 500; color: var(--text-secondary); }}
        .controls-container select {{ background-color: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-color); padding: 6px 32px 6px 12px; border-radius: 4px; font-size: 14px; font-weight: 500; cursor: pointer; outline: none; appearance: none; }}

        .dashboard-wrapper.hide-Mean .arc-mean, .dashboard-wrapper.hide-Mean .bar-mean {{ opacity: 0 !important; pointer-events: none !important; }}
        .dashboard-wrapper.hide-P95 .arc-p95,   .dashboard-wrapper.hide-P95 .bar-p95   {{ opacity: 0 !important; pointer-events: none !important; }}
        .dashboard-wrapper.hide-P99 .arc-p99,   .dashboard-wrapper.hide-P99 .bar-p99   {{ opacity: 0 !important; pointer-events: none !important; }}
        .dashboard-wrapper.hide-Max .arc-max,   .dashboard-wrapper.hide-Max .bar-max   {{ opacity: 0 !important; pointer-events: none !important; }}

        .mini-data-table {{ width: 100%; display: none; justify-content: space-between; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-color); }}
        .dashboard-wrapper.view-all .mini-data-table {{ display: flex; }}
        .mdt-col {{ display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }}
        .mdt-label {{ font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }}
        .mdt-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
        .mdt-val {{ font-size: 14px; font-weight: 600; color: var(--text-primary); padding-left: 14px; }}

        .gauges-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 24px; margin-bottom: 24px; }}
        .gauge-card {{ position: relative; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 6px; padding: 24px; display: flex; flex-direction: column; align-items: center; box-shadow: 0 1px 2px rgba(0,0,0,0.02); }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; width: 100%; margin-bottom: 24px; }}
        .card-title {{ font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0; }}
        .gauge-body {{ position: relative; width: 240px; height: 120px; }}
        .gauge-outer-track {{ position: absolute; bottom: 0; left: 0; width: 240px; height: 120px; background: conic-gradient(from 270deg at 50% 100%, var(--color-green) 0deg, var(--color-green) calc(var(--yellow-deg) - 1.5deg), transparent calc(var(--yellow-deg) - 1.5deg), transparent calc(var(--yellow-deg) + 1.5deg), var(--color-yellow) calc(var(--yellow-deg) + 1.5deg), var(--color-yellow) calc(var(--red-deg) - 1.5deg), transparent calc(var(--red-deg) - 1.5deg), transparent calc(var(--red-deg) + 1.5deg), var(--color-red) calc(var(--red-deg) + 1.5deg), var(--color-red) 180deg); mask-image: radial-gradient(circle at 50% 100%, transparent 115px, black 116px); -webkit-mask-image: radial-gradient(circle at 50% 100%, transparent 115px, black 116px); border-radius: 120px 120px 0 0; transition: opacity 0.3s; }}
        .dashboard-wrapper.view-all .gauge-outer-track {{ display: none; }}
        .gauge-inner-bg {{ position: absolute; bottom: 0; left: 10px; width: 220px; height: 110px; border: 24px solid var(--track-bg); border-bottom: 0; border-radius: 110px 110px 0 0; box-sizing: border-box; }}
        .gauge-inner-fill-wrapper {{ position: absolute; bottom: 0; left: 10px; width: 220px; height: 110px; overflow: hidden; }}
        .arc-fill {{ position: absolute; bottom: 0; left: 0; width: 220px; height: 110px; border: 24px solid; border-bottom: 0; border-radius: 110px 110px 0 0; box-sizing: border-box; transform-origin: 50% 100%; transition: transform 0.6s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.3s ease, border-color 0.3s ease; }}
        .arc-single {{ border-color: var(--fill-color, var(--c-mean)); transform: rotate(var(--rot-single, -180deg)); z-index: 5; }}
        .dashboard-wrapper.view-all .arc-single {{ display: none; }}
        .arc-multi {{ display: none; }}
        .dashboard-wrapper.view-all .arc-multi {{ display: block; }}
        .arc-max  {{ border-color: var(--c-max);  transform: rotate(var(--rot-max, -180deg));  z-index: 1; opacity: 0.15; }}
        .arc-p99  {{ border-color: var(--c-p99);  transform: rotate(var(--rot-p99, -180deg));  z-index: 2; opacity: 0.40; }}
        .arc-p95  {{ border-color: var(--c-p95);  transform: rotate(var(--rot-p95, -180deg));  z-index: 3; opacity: 0.70; }}
        .arc-mean {{ border-color: var(--c-mean); transform: rotate(var(--rot-mean, -180deg)); z-index: 4; opacity: 1.00; }}
        .gauge-value {{ position: absolute; bottom: 8px; left: 0; width: 100%; text-align: center; }}
        .gauge-value-main {{ font-size: 28px; font-weight: 600; color: var(--fill-color, var(--text-primary)); transition: color 0.4s ease; }}
        .dashboard-wrapper.view-all .gauge-value {{ display: none; }}

        .bars-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 24px; }}
        .bar-card {{ position: relative; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 6px; padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.02); }}
        .bar-header {{ display: flex; justify-content: space-between; align-items: flex-end; }}
        .bar-single-value {{ font-size: 18px; font-weight: 600; color: var(--text-primary); transition: color 0.3s; }}
        .dashboard-wrapper.view-all .bar-single-value {{ display: none; }}
        .bar-track {{ width: 100%; height: 12px; background: var(--track-bg); border-radius: 6px; overflow: hidden; position: relative; }}
        .bar-fill {{ position: absolute; top: 0; left: 0; height: 100%; border-radius: 6px; transition: width 0.6s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.3s ease, background-color 0.3s ease; }}
        .bar-single {{ background: var(--fill-color, var(--c-mean)); width: var(--w-single, 0%); z-index: 5; }}
        .dashboard-wrapper.view-all .bar-single {{ display: none; }}
        .bar-multi {{ display: none; }}
        .dashboard-wrapper.view-all .bar-multi {{ display: block; }}
        .bar-max  {{ width: var(--w-max, 0%);  background: var(--c-max);  z-index: 1; opacity: 0.15; }}
        .bar-p99  {{ width: var(--w-p99, 0%);  background: var(--c-p99);  z-index: 2; opacity: 0.40; }}
        .bar-p95  {{ width: var(--w-p95, 0%);  background: var(--c-p95);  z-index: 3; opacity: 0.70; }}
        .bar-mean {{ width: var(--w-mean, 0%); background: var(--c-mean); z-index: 4; opacity: 1.00; }}
        .dashboard-section-title {{ font-size: 13px; font-weight: 700; color: var(--text-primary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 16px; padding-bottom: 8px; }}
    </style>
</head>
<body>

<div id="topbar">
    <h1>Database Health Report</h1>
    <select id="filter-dbtype" onchange="updateServerDropdown()">
        <option value="ALL">All Database Engines</option>
        <option value="PostgreSQL">PostgreSQL</option>
        <option value="MySQL">MySQL</option>
    </select>
    <select id="filter-project" onchange="updateServerDropdown()">
        <option value="ALL">All Projects</option>
    </select>
    <select id="filter-server" onchange="applyFilters()">
        <option value="ALL">All Instances</option>
    </select>
    <div class="topbar-timestamp">
        Report Period<br>
        {period_from} &rarr; {period_to}
    </div>
</div>

<div id="content">
"""

# ─────────────────────────────────────────────
# MAIN REPORT — PER INSTANCE
# ─────────────────────────────────────────────

table_counter = [0]
dashboards_json_data = {}

for unique_key, data in report_data.items():
    safe_instance = unique_key.replace(":", "-").replace(" ", "_")
    
    instance_name = data.get("instance_name", unique_key)
    project_id    = data.get("project_id", "")
    specs         = data.get("provisioned_specs", {})
    utilization   = data.get("resource_utilization", {})
    health_checks = data.get("health_checks", {})
    
    db_type_label = nav_data.get(unique_key, {}).get("type", "Unknown")
    safe_proj     = html.escape(project_id) if project_id else "No Project"

    html_content += f'<div class="instance-block" data-server="{unique_key}" data-dbtype="{db_type_label}" data-project="{safe_proj}" id="srv-{safe_instance}">'
    
    # Instance Header
    html_content += '<div class="instance-title">'
    if project_id:
        html_content += f'<span class="breadcrumb-project">{project_id}</span>'
        html_content += '<span class="breadcrumb-separator">/</span>'
    html_content += f'<span class="breadcrumb-instance">{instance_name}</span>'
    html_content += '</div>'
    
    html_content += '<div class="section-card">'

    # ── Provisioned Specs ──
    if specs and "Error" not in specs:
        html_content += '<div class="section-title" style="margin-bottom:16px;">Provisioned Specifications</div>'
        html_content += '<div class="specs-grid">'
        for key, val in specs.items():
            html_content += f'''
            <div class="spec-item">
                <div class="spec-label">{key}</div>
                <div class="spec-value">{val if val is not None else "N/A"}</div>
            </div>'''
        html_content += '</div>'
    elif "Error" in specs:
        html_content += f'<div class="error-box"><strong>Configuration Error:</strong><br>{specs["Error"]}</div>'

    # ── Resource Utilization Dashboard ──
    if utilization:
        pct_metrics = []
        cnt_metrics = []
        
        for metric_key, m in utilization.items():
            if not isinstance(m, dict): continue
            label = m.get("header-name") or format_clean_title(metric_key)
            item = {
                "name": label,
                "values": {
                    "Mean": m.get("mean"),
                    "P95": m.get("p95"),
                    "P99": m.get("p99"),
                    "Max": m.get("max")
                }
            }
            if "(%)" in label or "Percent" in label:
                item["thresholds"] = {"yellow": 70, "red": 90} 
                pct_metrics.append(item)
            else:
                max_val = m.get("max") if m.get("max") is not None else 100
                item["max_scale"] = max_val * 1.5 if max_val > 0 else 100 
                cnt_metrics.append(item)
                
        dashboards_json_data[safe_instance] = {
            "percentage_metrics": pct_metrics,
            "count_metrics": cnt_metrics
        }
        
        # Inject the Dashboard Wrapper per instance with toggle functionality
        html_content += f'''
        <div class="dashboard-wrapper" id="dash-{safe_instance}">
            <div class="dashboard-header">
                <div class="dash-title-group" onclick="toggleDashboard(this)">
                    <span class="dash-arrow">&#9660;</span>
                    <h2>Resource Utilization (Last 24h)</h2>
                </div>
                <div class="header-controls">
                    <div class="global-toggles" id="toggles-{safe_instance}">
                        <button class="toggle-btn active" data-metric="Mean">Mean</button>
                        <button class="toggle-btn active" data-metric="P95">P95</button>
                        <button class="toggle-btn active" data-metric="P99">P99</button>
                        <button class="toggle-btn active" data-metric="Max">Max</button>
                    </div>
                    <div class="controls-container">
                        <label>Aggregation View:</label>
                        <select id="agg-{safe_instance}"></select>
                    </div>
                </div>
            </div>
            
            <div class="dashboard-content" id="dash-content-{safe_instance}">
                <div class="dashboard-section-title">Percentage Metrics</div>
                <div id="gauges-{safe_instance}" class="gauges-grid"></div>
                
                <div class="dashboard-section-title">Count Metrics</div>
                <div id="bars-{safe_instance}" class="bars-grid"></div>
            </div>
        </div>
        '''

    html_content += '</div>'  # close section-card

    # ── Health Checks ──
    if "connection_error" in health_checks:
        html_content += f'<div class="error-box"><strong>Connection Error:</strong><br>{health_checks["connection_error"]}</div>'
    elif "error" in health_checks:
        html_content += f'<div class="error-box"><strong>Error:</strong><br>{health_checks["error"]}</div>'
    elif isinstance(health_checks, dict):

        for category, queries in health_checks.items():
            if not isinstance(queries, dict):
                continue
            
            clean_category = format_clean_title(category)
            valid_queries = {}
            for metric_name, rows in queries.items():
                skip_metric = False
                if rows and isinstance(rows, list) and len(rows) > 0 and isinstance(rows[0], dict):
                    first_row = rows[0]
                    if "error" in first_row or first_row.get("status") == "ERROR" or "Result unavailable" in str(first_row.get("message", "")):
                        skip_metric = True
                
                if not skip_metric:
                    valid_queries[metric_name] = rows
            
            if not valid_queries:
                continue

            html_content += f'''
            <div class="category-title" onclick="toggleCategory(this)">
                <span>{clean_category}</span><span>&#9654;</span>
            </div>
            <div class="category-content hidden">'''

            for metric_name, rows in valid_queries.items():
                table_counter[0] += 1
                tid          = f"{safe_instance}_{category}_{metric_name}_{table_counter[0]}"
                clean_metric = format_clean_title(metric_name)
                html_content += f'<div class="metric-title">{clean_metric}</div>'

                html_content += build_paginated_table(rows, tid, metric_name=metric_name)

            html_content += '</div>'

    html_content += '</div>'

# ─────────────────────────────────────────────
# JAVASCRIPT
# ─────────────────────────────────────────────

nav_json = json.dumps(nav_data)
dash_data_json = json.dumps(dashboards_json_data)

html_content += f"""
</div>

<script>
const navData = {nav_json};

const typeSel = document.getElementById('filter-dbtype');
const projSel = document.getElementById('filter-project');
const serverSel = document.getElementById('filter-server');

const projects = new Set();
Object.values(navData).forEach(info => {{
    if (info.project_id && info.project_id !== "No Project") {{
        projects.add(info.project_id);
    }}
}});
Array.from(projects).sort().forEach(proj => {{
    const o = document.createElement('option');
    o.value = proj; 
    o.textContent = proj;
    projSel.appendChild(o);
}});

function updateServerDropdown() {{
    const type = typeSel.value;
    const proj = projSel.value;
    
    serverSel.innerHTML = '<option value="ALL">All Instances</option>';
    
    Object.entries(navData).forEach(([key, info]) => {{
        const typeMatch = type === 'ALL' || info.type === type;
        const projMatch = proj === 'ALL' || info.project_id === proj;
        
        if (typeMatch && projMatch) {{
            const o = document.createElement('option');
            o.value = key; 
            o.textContent = info.label;
            serverSel.appendChild(o);
        }}
    }});
    
    applyFilters();
}}

function applyFilters() {{
    const type = typeSel.value;
    const proj = projSel.value;
    const srv  = serverSel.value;
    
    document.querySelectorAll('.instance-block').forEach(block => {{
        const typeMatch = type === 'ALL' || block.dataset.dbtype === type;
        const projMatch = proj === 'ALL' || block.dataset.project === proj;
        const srvMatch  = srv  === 'ALL' || block.dataset.server === srv;
        
        block.classList.toggle('hidden', !(typeMatch && projMatch && srvMatch));
    }});
}}

updateServerDropdown();

function toggleCategory(el) {{
    const content  = el.nextElementSibling;
    const arrow    = el.querySelector('span:last-child');
    const isHidden = content.classList.toggle('hidden');
    arrow.innerHTML = isHidden ? '&#9654;' : '&#9660;';
}}

function toggleDashboard(el) {{
    const wrapper  = el.closest('.dashboard-wrapper');
    const content  = wrapper.querySelector('.dashboard-content');
    const arrow    = el.querySelector('.dash-arrow');
    const isHidden = content.classList.toggle('hidden');
    arrow.innerHTML = isHidden ? '&#9654;' : '&#9660;';
}}

const pageState = {{}};
function changePage(tableId, direction) {{
    const rows       = document.querySelectorAll(`#tbl-${{tableId}} tbody .page-row`);
    const totalPages = Math.ceil(rows.length / 10);
    if (!pageState[tableId]) pageState[tableId] = 1;
    pageState[tableId] = Math.max(1, Math.min(totalPages, pageState[tableId] + direction));
    const currentPage = pageState[tableId];
    const start       = (currentPage - 1) * 10;
    const end         = start + 10;
    rows.forEach((row, i) => {{
        row.style.display = (i >= start && i < end) ? '' : 'none';
    }});
    const info = document.getElementById(`page-info-${{tableId}}`);
    if (info) info.textContent = `Page ${{currentPage}} of ${{totalPages}}`;
}}

// =========================================
// DASHBOARD RENDER LOGIC
// =========================================
const metricColorMap = {{
    "Mean": "var(--c-mean)",
    "P95": "var(--c-p95)",
    "P99": "var(--c-p99)",
    "Max": "var(--c-max)"
}};

function generateMiniDataTableHTML(values, isPct) {{
    const fmt = (v) => isPct ? (v != null ? v.toFixed(2) + '%' : 'N/A') : (v != null ? v.toLocaleString('en-US', {{maximumFractionDigits:2}}) : 'N/A');
    return `
        <div class="mini-data-table">
            <div class="mdt-col"><div class="mdt-label"><div class="mdt-dot" style="background: var(--c-mean)"></div>Mean</div><div class="mdt-val">${{fmt(values.Mean)}}</div></div>
            <div class="mdt-col"><div class="mdt-label"><div class="mdt-dot" style="background: var(--c-p95)"></div>P95</div><div class="mdt-val">${{fmt(values.P95)}}</div></div>
            <div class="mdt-col"><div class="mdt-label"><div class="mdt-dot" style="background: var(--c-p99)"></div>P99</div><div class="mdt-val">${{fmt(values.P99)}}</div></div>
            <div class="mdt-col"><div class="mdt-label"><div class="mdt-dot" style="background: var(--c-max)"></div>Max</div><div class="mdt-val">${{fmt(values.Max)}}</div></div>
        </div>
    `;
}}

const dashboardsData = {dash_data_json};

Object.keys(dashboardsData).forEach(instId => {{
    const appData = dashboardsData[instId];
    if (appData.percentage_metrics.length === 0 && appData.count_metrics.length === 0) return;

    let currentAggregation = "All";
    const dashboardWrapper = document.getElementById('dash-' + instId);
    const selectEl = document.getElementById('agg-' + instId);
    const gaugesContainer = document.getElementById('gauges-' + instId);
    const barsContainer = document.getElementById('bars-' + instId);

    ["All", "Mean", "P95", "P99", "Max"].forEach(opt => {{
        const option = document.createElement('option');
        option.value = opt; option.textContent = opt;
        if (opt === currentAggregation) option.selected = true;
        selectEl.appendChild(option);
    }});

    selectEl.addEventListener('change', (e) => {{
        currentAggregation = e.target.value;
        updateDashboard();
    }});

    document.querySelectorAll(`#toggles-${{instId}} .toggle-btn`).forEach(btn => {{
        btn.addEventListener('click', (e) => {{
            e.target.classList.toggle('active');
            dashboardWrapper.classList.toggle(`hide-${{e.target.getAttribute('data-metric')}}`);
        }});
    }});

    appData.percentage_metrics.forEach((metric, index) => {{
        const card = document.createElement('div');
        card.className = 'gauge-card';
        card.id = `gauge-${{instId}}-${{index}}`;
        const yellowDeg = (metric.thresholds.yellow / 100) * 180;
        const redDeg = (metric.thresholds.red / 100) * 180;
        card.style.setProperty('--yellow-deg', `${{yellowDeg}}deg`);
        card.style.setProperty('--red-deg', `${{redDeg}}deg`);

        card.innerHTML = `
            <div class="card-header"><h3 class="card-title">${{metric.name}}</h3></div>
            <div class="gauge-body">
                <div class="gauge-outer-track"></div>
                <div class="gauge-inner-bg"></div>
                <div class="gauge-inner-fill-wrapper">
                    <div class="arc-fill arc-single"></div>
                    <div class="arc-fill arc-multi arc-max"></div>
                    <div class="arc-fill arc-multi arc-p99"></div>
                    <div class="arc-fill arc-multi arc-p95"></div>
                    <div class="arc-fill arc-multi arc-mean"></div>
                </div>
                <div class="gauge-value"><div class="gauge-value-main"></div></div>
            </div>
            ${{generateMiniDataTableHTML(metric.values, true)}}
        `;
        gaugesContainer.appendChild(card);
    }});

    appData.count_metrics.forEach((metric, index) => {{
        const card = document.createElement('div');
        card.className = 'bar-card';
        card.id = `bar-${{instId}}-${{index}}`;
        card.innerHTML = `
            <div class="card-header">
                <h3 class="card-title">${{metric.name}}</h3>
                <div class="bar-single-value"></div>
            </div>
            <div class="bar-track">
                <div class="bar-fill bar-single"></div>
                <div class="bar-fill bar-multi bar-max"></div>
                <div class="bar-fill bar-multi bar-p99"></div>
                <div class="bar-fill bar-multi bar-p95"></div>
                <div class="bar-fill bar-multi bar-mean"></div>
            </div>
            ${{generateMiniDataTableHTML(metric.values, false)}}
        `;
        barsContainer.appendChild(card);
    }});

    function updateDashboard() {{
        const isAll = currentAggregation === "All";
        if (isAll) dashboardWrapper.classList.add('view-all');
        else dashboardWrapper.classList.remove('view-all');

        appData.percentage_metrics.forEach((config, index) => {{
            const card = document.getElementById(`gauge-${{instId}}-${{index}}`);
            const getSingleRot = (v) => v != null ? (Math.max(0, Math.min(v, 100)) / 100 * 180 - 180) + 'deg' : '-180deg';
            const fmt = (v) => v != null ? (Number.isInteger(v) ? v : v.toFixed(2).replace(/\.?0+$/, '')) + '%' : 'N/A';

            if (isAll) {{
                const localMax = config.values.Max > 0 ? config.values.Max : 1; 
                card.style.setProperty('--rot-mean', (((config.values.Mean / localMax) * 180) - 180) + 'deg');
                card.style.setProperty('--rot-p95', (((config.values.P95 / localMax) * 180) - 180) + 'deg');
                card.style.setProperty('--rot-p99', (((config.values.P99 / localMax) * 180) - 180) + 'deg');
                card.style.setProperty('--rot-max', '0deg');
            }} else {{
                const valTarget = config.values[currentAggregation];
                card.style.setProperty('--rot-single', getSingleRot(valTarget));
                card.style.setProperty('--fill-color', metricColorMap[currentAggregation]);
                card.querySelector('.gauge-value-main').textContent = fmt(valTarget);
            }}
        }});

        appData.count_metrics.forEach((config, index) => {{
            const card = document.getElementById(`bar-${{instId}}-${{index}}`);
            const getSingleWidth = (v) => v != null ? Math.min((v / config.max_scale) * 100, 100) + '%' : '0%';
            const fmtBar = (v) => v != null ? v.toLocaleString('en-US', {{ maximumFractionDigits: 2 }}) : 'N/A';

            if (isAll) {{
                const localMax = config.values.Max > 0 ? config.values.Max : 1; 
                card.style.setProperty('--w-mean', (config.values.Mean / localMax * 100) + '%');
                card.style.setProperty('--w-p95', (config.values.P95 / localMax * 100) + '%');
                card.style.setProperty('--w-p99', (config.values.P99 / localMax * 100) + '%');
                card.style.setProperty('--w-max', '100%');
            }} else {{
                const valTarget = config.values[currentAggregation];
                card.style.setProperty('--w-single', getSingleWidth(valTarget));
                card.style.setProperty('--fill-color', metricColorMap[currentAggregation]);
                card.querySelector('.bar-single-value').textContent = fmtBar(valTarget);
            }}
        }});
    }}

    setTimeout(updateDashboard, 50);
}});
</script>
</body>
</html>"""
