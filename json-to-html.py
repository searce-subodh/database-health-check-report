from datetime import datetime, timedelta
import json
import os
import sys
import html
import math
import urllib.request

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
                if "instance_name" in data:
                    inst_name = data["instance_name"]
                    project_id = data.get("project_id", "")
                    unique_key = f"{project_id}:{inst_name}" if project_id else inst_name
                    report_data[unique_key] = data
                else:
                    for k, v in data.items():
                        if isinstance(v, dict) and "health_checks" in v:
                            report_data[k] = v
    except Exception as e:
        print(f" Warning: Failed to load '{filepath}': {e}")

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
    
    display_label = instance_name
    
    nav_data[unique_key] = {
        "type": db_type,
        "label": display_label,
        "project_id": project_id if project_id else "No Project"
    }

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────


def calculate_y_axis_config(local_min, local_max, is_percent):
    """Calculates step size and boundary for Chart.js dynamically based ONLY on data."""
    
    # Handle edge case where there is no data
    if local_min == float('inf') or local_max == 0.0:
        return {"min": 0, "max": 100 if is_percent else 10, "step": 20 if is_percent else 2}
        
    data_range = local_max - local_min
    if data_range <= 0:
        return {"min": max(0, local_min - 5), "max": local_max + 5, "step": 5}

    # Determine a clean step size
    if data_range <= 10: y_step = 2
    elif data_range <= 25: y_step = 5
    elif data_range <= 50: y_step = 10
    elif data_range <= 100: y_step = 20
    else:
        magnitude = 10 ** math.floor(math.log10(max(1, data_range)))
        normalized = data_range / magnitude
        if normalized <= 2: y_step = 0.5 * magnitude
        elif normalized <= 5: y_step = 1 * magnitude
        else: y_step = 2 * magnitude

    # Calculate min and max bounds based strictly on the data points + padding
    y_min = max(0, math.floor(local_min / y_step) * y_step - y_step)
    y_max = math.ceil(local_max / y_step) * y_step + y_step
    
    # Cap percentage metrics at 100 so it doesn't draw a 105% line
    if is_percent: 
        y_max = min(100, y_max)
        
    return {"min": y_min, "max": y_max, "step": y_step}

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

def format_number(val):
    if val is None: return "0.00"
    return f"{val:,.2f}"

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
        style = "" if i < 5 else ' style="display:none"'
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

    total_pages = (len(rows) + 4) // 5
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
# FETCH CHART.JS FOR OFFLINE EMBEDDING
# ─────────────────────────────────────────────
print("Fetching Chart.js library for offline support...")
try:
    # Use a standard user-agent to prevent CDN blocking
    req = urllib.request.Request(
        'https://cdn.jsdelivr.net/npm/chart.js', 
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    with urllib.request.urlopen(req) as response:
        chartjs_source = response.read().decode('utf-8')
    chartjs_tag = f"<script>\n{chartjs_source}\n</script>"
    print("Successfully embedded Chart.js for offline use.")
except Exception as e:
    print(f"Warning: Failed to fetch Chart.js ({e}). Falling back to CDN link.")
    chartjs_tag = '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'

# ─────────────────────────────────────────────
# HTML HEAD + STYLES
# ─────────────────────────────────────────────

first_instance_data = list(report_data.values())[0] if report_data else {}
report_window = first_instance_data.get("report_window", {})
period_from   = report_window.get("from", generated_at)
period_to     = report_window.get("to",   generated_at)

css_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Database Health Check Report</title>
    __CHARTJS_SCRIPT__
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
    <title>Database Health Check Report</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
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
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: 'Inter', -apple-system, sans-serif; 
            font-variant-numeric: tabular-nums; 
            background: var(--bg-main); 
            color: var(--text-primary); 
        }
        h1, .section-title, .category-title, .instance-title {
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            letter-spacing: -0.02em;
        }
        #topbar {
            position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
            background: #0f172a; color: white;
            padding: 12px 24px; display: flex; align-items: center;
            gap: 16px; flex-wrap: wrap; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }
        #topbar h1 { font-size: 1.1em; color: white; white-space: nowrap; }
        #topbar select {
            padding: 6px 10px; border-radius: 6px; border: none;
            background: #1e293b; color: white; font-size: 0.85em;
            cursor: pointer; min-width: 160px; font-family: inherit;
        }
        #topbar select:focus { outline: 2px solid #3b82f6; }
        .topbar-timestamp {
            margin-left: auto; font-size: 0.8em; color: #94a3b8;
            white-space: nowrap; text-align: right; line-height: 1.6;
        }
        #content { margin-top: 72px; padding: 24px; max-width: 1400px; margin-left: auto; margin-right: auto; }
        
        .specs-grid {
            display: flex; flex-wrap: nowrap; overflow-x: auto;
            gap: 10px; margin-bottom: 16px; padding-bottom: 6px;
        }
        .spec-item {
            flex: 1 1 auto; min-width: 100px; white-space: nowrap;
            background: #f8fafc; border-radius: 6px; padding: 10px 14px;
            border-left: 4px solid #2563eb;
        }
        .spec-label { font-size: 0.68em; color: #64748b; text-transform: uppercase; margin-bottom: 4px; font-weight: 600; }
        .spec-value { font-size: 0.95em; font-weight: 700; color: #0f172a; }

        .instance-block { margin-bottom: 40px; }
        .instance-title {
            padding: 16px 24px; background: #e2e8f0; border-radius: 8px 8px 0 0; 
            border-left: 5px solid #2563eb; font-family: 'Montserrat', sans-serif; font-size: 1.25em;
        }
        .breadcrumb-project { font-weight: 500; color: #64748b; }
        .breadcrumb-separator { margin: 0 8px; color: #94a3b8; font-weight: 400; }
        .breadcrumb-instance { font-weight: 700; color: #0f172a; }
        .section-card { background: var(--bg-card); border-radius: 0 0 8px 8px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(9, 30, 66, 0.05); }
        .section-title { font-size: 1.15em; color: #0f172a; margin-bottom: 24px; padding-bottom: 12px; border-bottom: 2px solid var(--border-color); }

        details.metric-details {
            margin-bottom: 24px;
        }
        details.metric-details > summary {
            font-size: 1.05em; font-weight: 600; color: var(--text-primary); 
            font-family: 'Montserrat', sans-serif;
            padding-bottom: 8px; margin-bottom: 16px; 
            border-bottom: 1px solid var(--border-color);
            cursor: pointer; list-style: none; display: flex; align-items: center;
        }
        details.metric-details > summary::-webkit-details-marker { display: none; }
        details.metric-details > summary::before {
            content: '▶'; display: inline-block; margin-right: 8px; font-size: 0.9em;
            color: #2563eb; transition: transform 0.2s;
        }
        details.metric-details[open] > summary::before { transform: rotate(90deg); }

        .metric-title { font-size: 0.9em; font-weight: 600; color: #334155; margin: 14px 0 6px; }

        table { width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 0.85em; }
        th { background: #334155; color: white; padding: 9px 12px; text-align: left; font-family: 'Montserrat', sans-serif; font-weight: 600; }
        td { border: 1px solid #e2e8f0; padding: 8px 12px; word-break: break-word; }
        tr:nth-child(even) td { background: #f8fafc; }
        tr.row-critical td { background: #fee2e2 !important; }
        tr.row-warning  td { background: #fef9c3 !important; }
        tr:hover td { background: #eff6ff !important; }

        .pagination { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 0.75em; color: #64748b; }
        .pagination button { padding: 4px 12px; border-radius: 4px; border: 1px solid #cbd5e1; background: white; cursor: pointer; font-weight: 600; }
        .pagination button:hover { background: #e2e8f0; }
        .alert-badge { font-weight: 700; color: #991b1b; background: #fca5a5; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }
        .warn-badge  { font-weight: 700; color: #854d0e; background: #fde047; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }
        .no-issues   { color: #16a34a; font-style: italic; font-weight: 500; padding: 6px 0 12px 0; }
        .error-box   { color: #7f1d1d; background: #fee2e2; border-left: 4px solid #b91c1c; padding: 12px 16px; border-radius: 4px; margin: 8px 0; }
        .hidden      { display: none !important; }

        /* =========================================
           DASHBOARD UI STYLES 
           ========================================= */
        .dashboard-wrapper { background-color: var(--bg-card); width: 100%; margin-bottom: 24px; margin-top: 16px; display: flex; flex-direction: column; gap: 24px; }
        
        .bullet-group { display: flex; flex-direction: column; gap: 20px; }
        .bullet-row { background: #f8fafc; border: 1px solid var(--border-color); border-radius: 12px; padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
        .bullet-header { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
        .bullet-title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
        .bullet-badges { display: flex; gap: 8px; flex-wrap: wrap; }
        .badge-pill { font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: rgba(255, 255, 255, 0.8); border: 1px solid rgba(0, 0, 0, 0.08); }
        .badge-pill.mean { color: var(--c-mean); border-color: var(--c-mean); background: #eff6ff; }
        .badge-pill.p95  { color: var(--c-p95);  border-color: var(--c-p95); background: #ecfdf5; }
        .badge-pill.p99  { color: var(--c-p99);  border-color: var(--c-p99); background: #fff7ed; }
        .badge-pill.max  { color: var(--c-max);  border-color: var(--c-max); background: #faf5ff; }

        .kpi-tile-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; }
        .kpi-tile { background: #f8fafc; border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 12px; cursor: pointer; transition: transform 0.2s ease, border-color 0.2s ease, background-color 0.2s ease; }
        .kpi-tile:hover, .kpi-tile.selected { transform: translateY(-2px); border-color: var(--c-mean); background: #eff6ff; }
        .kpi-tile-header { font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; justify-content: space-between; align-items: center; }
        .kpi-tile-header-group { display: flex; flex-direction: column; gap: 4px; }
        .kpi-mean-label { font-size: 11px; font-weight: 600; color: var(--c-mean); }
        .main-val-container { display: flex; flex-direction: column; gap: 6px; }
        .kpi-tile-main-val { font-size: 28px; font-weight: 700; color: var(--c-mean); line-height: 1; white-space: nowrap; }
        .max-allowed-badge { font-size: 11px; font-weight: 600; color: var(--text-secondary); align-self: flex-start; }
        
        .kpi-tile-footer { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 10px; border-top: 1px solid var(--border-color); padding-top: 10px; margin-top: 4px; }
        .kpi-footer-item { display: flex; flex-direction: column; min-width: 0; }
        .kpi-footer-label { font-size: 10px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; }
        .kpi-footer-val { font-size: 12px; font-weight: 600; color: var(--text-primary); margin-top: 2px; }
        .shared-bar-container { margin-top: 18px; width: 100%; }
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
        __PERIOD_FROM__ &rarr; __PERIOD_TO__
    </div>
</div>

<div id="content">
"""
html_content = (css_template
    .replace("__PERIOD_FROM__", period_from)
    .replace("__PERIOD_TO__", period_to)
    .replace("__CHARTJS_SCRIPT__", chartjs_tag)
)
# ─────────────────────────────────────────────
# MAIN REPORT — PER INSTANCE
# ─────────────────────────────────────────────

table_counter = [0]
global_chart_configs = {}

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
        html_content += '<div class="section-title">Provisioned Specifications</div>'
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

    # ── Resource Usage Dashboard ──
    if utilization:
        # 3. Time Range Padding & Missing Data
        raw_time_keys = sorted([k for k in utilization.keys() if k.startswith("20")])
        if raw_time_keys:
            start_dt = datetime.strptime(raw_time_keys[0], '%Y_%m_%d_%H_%M_%S')
            end_dt = datetime.strptime(raw_time_keys[-1], '%Y_%m_%d_%H_%M_%S')
            
            continuous_keys = []
            curr = start_dt
            while curr <= end_dt:
                continuous_keys.append(curr.strftime('%Y_%m_%d_%H_%M_%S'))
                curr += timedelta(hours=1)
        else:
            continuous_keys = []
            
        js_labels = [datetime.strptime(ts, '%Y_%m_%d_%H_%M_%S').isoformat() + "Z" for ts in continuous_keys]
        
        global_chart_configs[safe_instance] = {"labels": js_labels, "charts": []}
        metric_configs = {k: v for k, v in utilization.items() if not k.startswith("20") and isinstance(v, dict)}
        
        capacity_metrics = [k for k in metric_configs.keys() if "utilization" in k]
        traffic_metrics  = [k for k in metric_configs.keys() if "utilization" not in k]

        html_content += f'''
        <div class="dashboard-wrapper" id="dash-{safe_instance}">
            <div class="section-title">Resource Usage Summary</div>
            <div class="dashboard-content" id="dash-content-{safe_instance}">
        '''

        # 1. Capacity Allocation (Stacked vertically)
        if capacity_metrics:
            html_content += '<details class="metric-details"><summary>Capacity Utilization</summary><div class="bullet-group">'
            for m_key in capacity_metrics:
                limit_info = metric_configs[m_key]
                is_pct = "utilization" in m_key
                unit = "%" if is_pct else (" GB" if "bytes" in m_key else "")
                display_name = limit_info.get("header-name", format_clean_title(m_key))
                chart_id = f"chart_{safe_instance}_{m_key}"
                
                mean_data, p95_data, p99_data, max_data = [], [], [], []
                local_max = 0.0
                local_min = float('inf')
                
                for t_key in continuous_keys:
                    hour_data = utilization.get(t_key, {})
                    
                    v_mean = hour_data.get(f"{m_key}_mean")
                    v_p95  = hour_data.get(f"{m_key}_p95")
                    v_p99  = hour_data.get(f"{m_key}_p99")
                    v_max  = hour_data.get(f"{m_key}_max")
                    
                    mean_data.append(v_mean)
                    p95_data.append(v_p95)
                    p99_data.append(v_p99)
                    max_data.append(v_max)
                    
                    # Track limits for dynamic Y-Axis
                    if v_max is not None and v_max > local_max: local_max = v_max
                    if v_mean is not None and v_mean < local_min: local_min = v_mean

                valid_mean = [x for x in mean_data if x is not None]
                valid_p95 = [x for x in p95_data if x is not None]
                valid_p99 = [x for x in p99_data if x is not None]
                valid_max = [x for x in max_data if x is not None]

                avg_mean = sum(valid_mean) / len(valid_mean) if valid_mean else 0
                avg_p95 = sum(valid_p95) / len(valid_p95) if valid_p95 else 0
                avg_p99 = sum(valid_p99) / len(valid_p99) if valid_p99 else 0
                max_max = max(valid_max) if valid_max else 0

                html_content += f'''
                <div class="bullet-row">
                    <div class="bullet-header">
                        <div class="bullet-title">{display_name}</div>
                        <div class="bullet-badges">
                            <span class="badge-pill mean">Mean: {format_number(avg_mean)}{unit}</span>
                            <span class="badge-pill p95">P95: {format_number(avg_p95)}{unit}</span>
                            <span class="badge-pill p99">P99: {format_number(avg_p99)}{unit}</span>
                            <span class="badge-pill max">Max: {format_number(max_max)}{unit}</span>
                        </div>
                    </div>
                    <div class="canvas-wrapper" style="position: relative; height: 220px; width: 100%; margin-top: 16px;">
                        <canvas id="{chart_id}"></canvas>
                    </div>
                </div>
                '''

                # Dynamic Y-Axis Limits Application
                y_config = calculate_y_axis_config(local_min, local_max, is_pct)
                y_config["title"] = f"Unit: {unit.strip()}" if unit else "Count"

                global_chart_configs[safe_instance]["charts"].append({
                    "id": chart_id, "unit": unit, "yConfig": y_config,
                    "data": {"mean": mean_data, "p95": p95_data, "p99": p99_data, "max": max_data}
                })
            html_content += '</div></details>'

        # 2. IO & Traffic Statistics (Clickable KPI Tiles)
        if traffic_metrics:
            html_content += '<details class="metric-details"><summary>IO & Traffic Statistics</summary><div class="kpi-tile-grid">'
            # Generate the Tiles
            for m_key in traffic_metrics:
                limit_info = metric_configs[m_key]
                display_name = limit_info.get("header-name", format_clean_title(m_key))
                chart_id = f"chart_{safe_instance}_{m_key}"
                is_pct = "utilization" in m_key
                unit = "%" if is_pct else (" GB" if "bytes" in m_key else "")
                
                mean_data, p95_data, p99_data, max_data = [], [], [], []
                local_max = 0.0
                local_min = float('inf')
                
                for t_key in continuous_keys:
                    hour_data = utilization.get(t_key, {})
                    
                    v_mean = hour_data.get(f"{m_key}_mean")
                    v_p95  = hour_data.get(f"{m_key}_p95")
                    v_p99  = hour_data.get(f"{m_key}_p99")
                    v_max  = hour_data.get(f"{m_key}_max")
                    
                    mean_data.append(v_mean)
                    p95_data.append(v_p95)
                    p99_data.append(v_p99)
                    max_data.append(v_max)
                    
                    if v_max is not None and v_max > local_max: local_max = v_max
                    if v_mean is not None and v_mean < local_min: local_min = v_mean

                valid_mean = [x for x in mean_data if x is not None]
                valid_p95 = [x for x in p95_data if x is not None]
                valid_p99 = [x for x in p99_data if x is not None]
                valid_max = [x for x in max_data if x is not None]

                avg_mean = sum(valid_mean) / len(valid_mean) if valid_mean else 0
                avg_p95 = sum(valid_p95) / len(valid_p95) if valid_p95 else 0
                avg_p99 = sum(valid_p99) / len(valid_p99) if valid_p99 else 0
                max_max = max(valid_max) if valid_max else 0
                max_allowed = limit_info.get("max_allocated_limit", "N/A")

                html_content += f'''
                <div class="kpi-tile tile-{safe_instance}" id="tile-{chart_id}" onclick="handleTileClick('{safe_instance}', '{chart_id}')">
                    <div class="kpi-tile-header-group">
                        <div class="kpi-tile-header">{display_name}</div>
                        <div class="kpi-mean-label">Mean</div>
                    </div>
                    <div class="main-val-container">
                        <div class="kpi-tile-main-val">{format_number(avg_mean)}</div>
                        <div class="max-allowed-badge">Max Allowed: {format_number(max_allowed) if isinstance(max_allowed, (int, float)) else max_allowed}</div>
                    </div>
                    <div class="kpi-tile-footer">
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-p95);">P95</span>
                            <span class="kpi-footer-val">{format_number(avg_p95)}</span>
                        </div>
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-p99);">P99</span>
                            <span class="kpi-footer-val">{format_number(avg_p99)}</span>
                        </div>
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-max);">Max</span>
                            <span class="kpi-footer-val">{format_number(max_max)}</span>
                        </div>
                    </div>
                </div>
                '''

                # Populate Chart Config inside the loop to avoid dataset overwriting
                y_config = calculate_y_axis_config(local_min, local_max, is_pct)
                y_config["title"] = f"Unit: {unit.strip()}" if unit else "Count"

                global_chart_configs[safe_instance]["charts"].append({
                    "id": chart_id, "unit": unit, "yConfig": y_config,
                    "data": {"mean": mean_data, "p95": p95_data, "p99": p99_data, "max": max_data}
                })

            html_content += '</div>'
            
            # Shared container holding all hidden chart canvases
            html_content += f'<div id="shared-chart-container-{safe_instance}" class="shared-bar-container active">'
            for m_key in traffic_metrics:
                limit_info = metric_configs[m_key]
                display_name = limit_info.get("header-name", format_clean_title(m_key))
                chart_id = f"chart_{safe_instance}_{m_key}"

                html_content += f'''
                <div id="wrapper-{chart_id}" class="chart-wrapper-{safe_instance}" style="display: none;">
                    <div class="bullet-row" style="margin-top: 16px;">
                        <div class="bullet-header">
                            <div class="bullet-title">{display_name} Chart</div>
                        </div>
                        <div class="canvas-wrapper" style="position: relative; height: 260px; width: 100%;">
                            <canvas id="{chart_id}"></canvas>
                        </div>
                    </div>
                </div>
                '''
            html_content += '</div></details>'

        html_content += '</div></div>'

    # ── Operational Health ──
    html_content += '<div class="section-title">Operational Health</div>'
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
            <details class="metric-details">
                <summary>{clean_category}</summary>
                <div class="category-content">'''

            for metric_name, rows in valid_queries.items():
                table_counter[0] += 1
                tid          = f"{safe_instance}_{category}_{metric_name}_{table_counter[0]}"
                clean_metric = format_clean_title(metric_name)
                html_content += f'<div class="metric-title">{clean_metric}</div>'
                html_content += build_paginated_table(rows, tid, metric_name=metric_name)

            html_content += '</div></details>'

    html_content += '</div>'
    html_content += '</div>'

# ─────────────────────────────────────────────
# JAVASCRIPT
# ─────────────────────────────────────────────

nav_json = json.dumps(nav_data)
dash_data_json = json.dumps(global_chart_configs if 'global_chart_configs' in locals() else {})

js_template = """
</div>

<script>
const navData = __NAV_JSON__;
const chartConfigs = __DASH_JSON__;

const typeSel = document.getElementById('filter-dbtype');
const projSel = document.getElementById('filter-project');
const serverSel = document.getElementById('filter-server');

const projects = new Set();
Object.values(navData).forEach(info => {
    if (info.project_id && info.project_id !== "No Project") {
        projects.add(info.project_id);
    }
});
Array.from(projects).sort().forEach(proj => {
    const o = document.createElement('option');
    o.value = proj; 
    o.textContent = proj;
    projSel.appendChild(o);
});

function updateServerDropdown() {
    const type = typeSel.value;
    const proj = projSel.value;
    serverSel.innerHTML = '<option value="ALL">All Instances</option>';
    
    Object.entries(navData).forEach(([key, info]) => {
        if ((type === 'ALL' || info.type === type) && (proj === 'ALL' || info.project_id === proj)) {
            const o = document.createElement('option');
            o.value = key; o.textContent = info.label;
            serverSel.appendChild(o);
        }
    });
    applyFilters();
}

function applyFilters() {
    const type = typeSel.value;
    const proj = projSel.value;
    const srv  = serverSel.value;
    
    document.querySelectorAll('.instance-block').forEach(block => {
        const typeMatch = type === 'ALL' || block.dataset.dbtype === type;
        const projMatch = proj === 'ALL' || block.dataset.project === proj;
        const srvMatch  = srv  === 'ALL' || block.dataset.server === srv;
        block.classList.toggle('hidden', !(typeMatch && projMatch && srvMatch));
    });
}
updateServerDropdown();

const pageState = {};
function changePage(tableId, direction) {
    const rows = document.querySelectorAll(`#tbl-${tableId} tbody .page-row`);
    const totalPages = Math.ceil(rows.length / 5);
    if (!pageState[tableId]) pageState[tableId] = 1;
    pageState[tableId] = Math.max(1, Math.min(totalPages, pageState[tableId] + direction));
    const start = (pageState[tableId] - 1) * 5;
    rows.forEach((row, i) => { row.style.display = (i >= start && i < start + 5) ? '' : 'none'; });
    document.getElementById(`page-info-${tableId}`).textContent = `Page ${pageState[tableId]} of ${totalPages}`;
}

// KPI Tile Click Logic
function handleTileClick(instanceId, chartId) {
    document.querySelectorAll(`.tile-${instanceId}`).forEach(t => t.classList.remove('selected'));
    const clickedTile = document.getElementById(`tile-${chartId}`);
    
    const wrapper = document.getElementById(`wrapper-${chartId}`);
    const isAlreadyOpen = wrapper.style.display === 'block';
    
    document.querySelectorAll(`.chart-wrapper-${instanceId}`).forEach(w => {
        w.style.display = 'none';
    });

    if (isAlreadyOpen) {
        return; 
    }

    if (clickedTile) clickedTile.classList.add('selected');
    if (wrapper) {
        wrapper.style.display = 'block';
        const chart = Chart.getChart(chartId);
        if (chart) {
            chart.resize();
        }
    }
}


// =========================================
// CHART.JS INITIALIZATION
// =========================================
function createMetricChart(canvasId, labels, metricData, yConfig, unit) {
    const ctx = document.getElementById(canvasId).getContext('2d');
    new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels, 
            datasets: [
                { label: 'Mean', data: metricData.mean, borderColor: '#0d6efd', backgroundColor: 'rgba(13,110,253,0.15)', borderWidth: 1.5, tension: 0.3, fill: 'origin', pointRadius: 0, pointHoverRadius: 6, spanGaps: false },
                { label: 'P95', data: metricData.p95, borderColor: '#fd7e14', backgroundColor: 'rgba(253,126,20,0.15)', borderWidth: 1.5, tension: 0.3, fill: '-1', pointRadius: 0, pointHoverRadius: 6, spanGaps: false },
                { label: 'P99', data: metricData.p99, borderColor: '#6c757d', backgroundColor: 'rgba(108,117,125,0.15)', borderWidth: 1.5, tension: 0.3, fill: '-1', pointRadius: 0, pointHoverRadius: 6, spanGaps: false },
                { label: 'Max', data: metricData.max, borderColor: '#dc3545', backgroundColor: 'rgba(220,53,69,0.15)', borderWidth: 1.5, tension: 0.3, fill: '-1', pointRadius: 0, pointHoverRadius: 6, spanGaps: false }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            
            // Tooltip Sorting and Hover Interaction
            onHover: function(e, elements, chart) {
                if (elements && elements.length > 0) {
                    chart.hoveredDatasetIndex = elements[0].datasetIndex;
                }
            },
            interaction: { mode: 'index', intersect: false },
            hover: { mode: 'nearest', intersect: false },
            
            plugins: {
                tooltip: {
                    itemSort: function(a, b) {
                        const hoveredIndex = a.chart.hoveredDatasetIndex;
                        if (a.datasetIndex === hoveredIndex) return -1;
                        if (b.datasetIndex === hoveredIndex) return 1;
                        return b.raw - a.raw; 
                    },
                    callbacks: {
                        title: (ctx) => {
                            let d = new Date(ctx[0].label);
                            let day = d.getDate();
                            let month = d.toLocaleString('en-US', { month: 'short' });
                            let year = d.getFullYear();
                            let time = d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
                            return `${day}, ${month}, ${year}, ${time}`;
                        },
                        label: (ctx) => {
                            let val = ctx.parsed.y;
                            return ctx.dataset.label + ': ' + (val !== null ? val + unit : 'No Data');
                        }
                    }
                },
                legend: { labels: { color: '#333' } }
            },
            scales: {
                y: { min: yConfig.min, max: yConfig.max, title: { display: true, text: yConfig.title }, ticks: { stepSize: yConfig.step } },
                x: {
                    offset: true, // X-Axis Edge Spacing
                    grid: { display: false }, 
                    ticks: {
                        callback: function(val, i) {
                            let d = new Date(this.getLabelForValue(val));
                            if (i % 2 !== 0) return null; // Thin out ticks
                            
                            // Return Month Day for midnight, otherwise Hour:Minute AM/PM
                            return d.getHours() === 0 
                                ? d.toLocaleString('en-US', { month: 'short', day: 'numeric' }) 
                                : d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
                        }
                    }
                }
            }
        }
    });
}

// Render all charts dynamically
Object.values(chartConfigs).forEach(instance => {
    instance.charts.forEach(c => {
        createMetricChart(c.id, instance.labels, c.data, c.yConfig, c.unit);
    });
});
</script>
</body>
</html>
"""

html_content += js_template.replace("__NAV_JSON__", nav_json).replace("__DASH_JSON__", dash_data_json)
current_time = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
filename = f"Database_Health_Report_{current_time}.html"
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

with open(output_path, "w") as f:
    f.write(html_content)

print(f"[OK] Report saved to: {output_path}")
