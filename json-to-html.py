from datetime import datetime
import json
import math
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
    
    # Instance dropdown only displays the name without the project ID
    display_label = instance_name
    
    nav_data[unique_key] = {
        "type": db_type,
        "label": display_label,
        "project_id": project_id if project_id else "No Project"
    }

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_dynamic_scale(m_max):
    """MULTI-TIER DYNAMIC RATIO SCALING FOR PERCENTAGE/COUNT METRICS"""
    if m_max is None or m_max <= 0: return 1
    
    target = m_max * 1.4
    if target <= 1: return 1
    if target <= 2: return 2
    if target <= 5: return 5
    if target <= 10: return 10
    if target <= 25: return 25
    if target <= 50: return 50
    if target <= 100: return 100
    if target <= 250: return 250
    if target <= 500: return 500
    if target <= 1000: return 1000
    
    return math.ceil(target / 500) * 500

def format_tick(val):
    if val >= 10:
        return f"{round(val):,}"
    formatted = f"{val:.2f}"
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted

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

css_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Database Health Check Report</title>
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

        .category-title { font-size: 1em; color: #2563eb; margin-top: 16px; padding: 8px 0; border-bottom: 1px solid #e2e8f0; cursor: pointer; display: flex; justify-content: space-between; }
        .category-title:hover { color: #1d4ed8; }
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
        .dashboard-wrapper { 
            background-color: var(--bg-card); 
            width: 100%; 
            margin-bottom: 24px; 
            margin-top: 16px;
            display: flex; flex-direction: column; gap: 24px; 
        }
        
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
        details.metric-details > summary::-webkit-details-marker {
            display: none;
        }
        details.metric-details > summary::before {
            content: '▶';
            display: inline-block;
            margin-right: 8px;
            font-size: 0.9em;
            color: #2563eb;
            transition: transform 0.2s;
        }
        details.metric-details[open] > summary::before {
            transform: rotate(90deg);
        }

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

        .track-wrapper { position: relative; width: 100%; }
        .gridlines { position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between; pointer-events: none; z-index: 0; }
        .gridline { width: 1px; height: 100%; background: rgba(0,0,0,0.06); }
        .bullet-track { position: relative; height: 18px; background: var(--track-bg); border-radius: 8px; overflow: hidden; border: 1px solid var(--border-color); box-shadow: inset 0 2px 4px rgba(0,0,0,0.05); z-index: 1; }
        .bullet-bar { position: absolute; top: 0; left: 0; height: 100%; border-radius: 6px; transition: width 0.6s cubic-bezier(0.16, 1, 0.3, 1); }
        
        .bar-max  { background: var(--c-max);  z-index: 1; opacity: 0.5; }
        .bar-p99  { background: var(--c-p99);  z-index: 2; opacity: 0.7; }
        .bar-p95  { background: var(--c-p95);  z-index: 3; opacity: 0.85; }
        .bar-mean { background: var(--c-mean); z-index: 4; opacity: 1; }
        .ticks-row { display: flex; justify-content: space-between; font-size: 10px; color: var(--text-secondary); margin-top: 4px; }

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
        .shared-bar-container { display: none; margin-top: 18px; width: 100%; }
        .shared-bar-container.active { display: block; }
        .truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
html_content = css_template.replace("__PERIOD_FROM__", period_from).replace("__PERIOD_TO__", period_to)

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
        pct_rows_html = ""
        kpi_tiles_html = ""
        dashboards_json_data[safe_instance] = {}
        
        for metric_key, m in utilization.items():
            if not isinstance(m, dict): continue
            label = m.get("header-name") or format_clean_title(metric_key)
            
            mean_val = m.get("mean")
            p95_val = m.get("p95")
            p99_val = m.get("p99")
            max_val = m.get("max")
            max_alloc = m.get("max_allocated_limit")
            
            is_pct = "(%)" in label or "Percent" in label
            
            def fmt_val(v, is_pct_flag=False):
                if v is None: return "N/A"
                return f"{v:,.2f}{'%' if is_pct_flag else ''}"

            if is_pct:
                snap_max = get_dynamic_scale(max_val)
                def calc_w(v): return min(100, max(0, (v / snap_max) * 100)) if v is not None else 0
                
                step = snap_max / 4
                
                pct_rows_html += f'''
                <div class="bullet-row">
                    <div class="bullet-header">
                        <div class="bullet-title">{label}</div>
                        <div class="bullet-badges">
                            <span class="badge-pill mean">Mean: {fmt_val(mean_val, True)}</span>
                            <span class="badge-pill p95">P95: {fmt_val(p95_val, True)}</span>
                            <span class="badge-pill p99">P99: {fmt_val(p99_val, True)}</span>
                            <span class="badge-pill max">Max: {fmt_val(max_val, True)}</span>
                        </div>
                    </div>
                    <div class="track-wrapper">
                        <div class="gridlines">
                            <div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div>
                        </div>
                        <div class="bullet-track">
                            <div class="bullet-bar bar-max" style="width: {calc_w(max_val)}%;"></div>
                            <div class="bullet-bar bar-p99" style="width: {calc_w(p99_val)}%;"></div>
                            <div class="bullet-bar bar-p95" style="width: {calc_w(p95_val)}%;"></div>
                            <div class="bullet-bar bar-mean" style="width: {calc_w(mean_val)}%;"></div>
                        </div>
                    </div>
                    <div class="ticks-row">
                        <span>0%</span>
                        <span>{format_tick(step)}%</span>
                        <span>{format_tick(step * 2)}%</span>
                        <span>{format_tick(step * 3)}%</span>
                        <span>{format_tick(snap_max)}%</span>
                    </div>
                </div>
                '''
            else:
                max_allowed_str = fmt_val(max_alloc) if max_alloc is not None else "N/A"
                safe_metric_key = metric_key.replace("_", "-").replace(" ", "-")
                
                # Push data to JS dict for expanding detailed bar 
                dashboards_json_data[safe_instance][metric_key] = m
                
                kpi_tiles_html += f'''
                <div id="tile-{safe_instance}-{safe_metric_key}" class="kpi-tile tile-{safe_instance}" onclick="handleTileClick('{safe_instance}', '{metric_key}')">
                    <div class="kpi-tile-header-group">
                        <div class="kpi-tile-header truncate" title="{label}">
                            <span>{label}</span>
                        </div>
                        <div class="kpi-mean-label">Mean</div>
                    </div>
                    <div class="main-val-container">
                        <div class="kpi-tile-main-val">{fmt_val(mean_val)}</div>
                        <div class="max-allowed-badge">Max Allowed: {max_allowed_str}</div>
                    </div>
                    <div class="kpi-tile-footer">
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-p95);">P95</span>
                            <span class="kpi-footer-val">{fmt_val(p95_val)}</span>
                        </div>
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-p99);">P99</span>
                            <span class="kpi-footer-val">{fmt_val(p99_val)}</span>
                        </div>
                        <div class="kpi-footer-item">
                            <span class="kpi-footer-label" style="color: var(--c-max);">Max</span>
                            <span class="kpi-footer-val">{fmt_val(max_val)}</span>
                        </div>
                    </div>
                </div>
                '''
                
        # Main Dashboard Wrapper Assembly
        html_content += f'''
        <div class="dashboard-wrapper" id="dash-{safe_instance}">
            <div class="section-title" style="margin-bottom:16px;">Resource Utilization (Last 24h)</div>
            
            <div class="dashboard-content" id="dash-content-{safe_instance}">
        '''
        
        if pct_rows_html:
            html_content += f'''
                <details class="metric-details" open>
                    <summary>Percentage Utilization Metrics</summary>
                    <div class="bullet-group">{pct_rows_html}</div>
                </details>
            '''
            
        if kpi_tiles_html:
            html_content += f'''
                <details class="metric-details" open>
                    <summary>Count And Throughput Metrics</summary>
                    <div class="kpi-tile-grid">{kpi_tiles_html}</div>
                    <div id="shared-bar-{safe_instance}" class="shared-bar-container"></div>
                </details>
            '''
            
        html_content += '''
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

js_template = """
</div>

<script>
const navData = __NAV_JSON__;
const countMetricsData = __DASH_JSON__;

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
        const typeMatch = type === 'ALL' || info.type === type;
        const projMatch = proj === 'ALL' || info.project_id === proj;
        
        if (typeMatch && projMatch) {
            const o = document.createElement('option');
            o.value = key; 
            o.textContent = info.label;
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

function toggleCategory(el) {
    const content  = el.nextElementSibling;
    const arrow    = el.querySelector('span:last-child');
    const isHidden = content.classList.toggle('hidden');
    arrow.innerHTML = isHidden ? '&#9654;' : '&#9660;';
}

const pageState = {};
function changePage(tableId, direction) {
    const rows       = document.querySelectorAll(`#tbl-${tableId} tbody .page-row`);
    const totalPages = Math.ceil(rows.length / 10);
    if (!pageState[tableId]) pageState[tableId] = 1;
    pageState[tableId] = Math.max(1, Math.min(totalPages, pageState[tableId] + direction));
    const currentPage = pageState[tableId];
    const start       = (currentPage - 1) * 10;
    const end         = start + 10;
    rows.forEach((row, i) => {
        row.style.display = (i >= start && i < end) ? '' : 'none';
    });
    const info = document.getElementById(`page-info-${tableId}`);
    if (info) info.textContent = `Page ${currentPage} of ${totalPages}`;
}

// =========================================
// DASHBOARD KPI TILE CLICK HANDLER
// =========================================

function formatTickValue(val) {
    if (val >= 10) return Math.round(val).toLocaleString();
    return Number(val.toFixed(2));
}

function formatNumber(num) {
    if (num === null || num === undefined) return "N/A";
    return num.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

// MULTI-TIER DYNAMIC RATIO SCALING FOR COUNT METRICS
function getDynamicCountScale(mMax) {
    if (mMax <= 0) return 1;
    
    // Give 40% breathing room at the right end relative to actual peak usage
    const target = mMax * 1.4;
    
    if (target <= 1) return 1;
    if (target <= 2) return 2;
    if (target <= 5) return 5;
    if (target <= 10) return 10;
    if (target <= 25) return 25;
    if (target <= 50) return 50;
    if (target <= 100) return 100;
    if (target <= 250) return 250;
    if (target <= 500) return 500;
    if (target <= 1000) return 1000;
    
    // Round to next clean multiple of 500 for higher numbers
    return Math.ceil(target / 500) * 500;
}

function handleTileClick(instId, metricKey) {
    const container = document.getElementById(`shared-bar-${instId}`);
    const allTiles = document.querySelectorAll(`.tile-${instId}`);
    if (!container) return;

    const safeMetricKey = metricKey.replace(/_/g, '-').replace(/ /g, '-');
    const selectedTile = document.getElementById(`tile-${instId}-${safeMetricKey}`);
    const isAlreadyActive = selectedTile && selectedTile.classList.contains('selected') && container.classList.contains('active');

    allTiles.forEach(t => t.classList.remove('selected'));

    if (isAlreadyActive) {
        container.classList.remove('active');
        container.innerHTML = '';
        return;
    }

    if (selectedTile) selectedTile.classList.add('selected');

    const instData = countMetricsData[instId];
    if (!instData) return;
    const m = instData[metricKey];
    if (!m) return;

    const label = m["header-name"] || metricKey;
    
    // Compute scale boundary dynamically relative to peak Max usage
    const dynamicScaleBound = getDynamicCountScale(m.max);
    const calcWidth = (val) => Math.min(100, Math.max(0, (val / dynamicScaleBound) * 100));
    
    // Generate evenly-spaced tick marks
    const step = dynamicScaleBound / 4;

    container.innerHTML = `
        <div class="bullet-row">
            <div class="bullet-header">
                <div class="bullet-title">${label}</div>
                <div class="bullet-badges">
                    <span class="badge-pill mean">Mean: ${formatNumber(m.mean)}</span>
                    <span class="badge-pill p95">P95: ${formatNumber(m.p95)}</span>
                    <span class="badge-pill p99">P99: ${formatNumber(m.p99)}</span>
                    <span class="badge-pill max">Max: ${formatNumber(m.max)}</span>
                </div>
            </div>
            <div class="track-wrapper">
                <div class="gridlines">
                    <div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div>
                </div>
                <div class="bullet-track">
                    <div class="bullet-bar bar-max" style="width: ${calcWidth(m.max)}%;"></div>
                    <div class="bullet-bar bar-p99" style="width: ${calcWidth(m.p99)}%;"></div>
                    <div class="bullet-bar bar-p95" style="width: ${calcWidth(m.p95)}%;"></div>
                    <div class="bullet-bar bar-mean" style="width: ${calcWidth(m.mean)}%;"></div>
                </div>
            </div>
            <div class="ticks-row">
                <span>0</span>
                <span>${formatTickValue(step)}</span>
                <span>${formatTickValue(step * 2)}</span>
                <span>${formatTickValue(step * 3)}</span>
                <span>${formatTickValue(dynamicScaleBound)}</span>
            </div>
        </div>
    `;
    container.classList.add('active');
}

</script>
</body>
</html>
"""

html_content += js_template.replace("__NAV_JSON__", nav_json).replace("__DASH_JSON__", dash_data_json)
current_time = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
filename = f"Database_Health_Report_{current_time}.html"
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),filename)

with open(output_path, "w") as f:
    f.write(html_content)

print(f"[OK] Report saved to: {output_path}")