from datetime import datetime
import json
import os
import re
import sys

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
                # Handles new single-instance JSON output from script.py
                if "instance_name" in data:
                    inst_name = data["instance_name"]
                    project_id = data.get("project_id", "")
                    # Unique key to ensure no collision across projects
                    unique_key = f"{project_id}_{inst_name}" if project_id else inst_name
                    report_data[unique_key] = data
                else:
                    # Fallback for old multi-instance root structure
                    for k, v in data.items():
                        if isinstance(v, dict) and "health_checks" in v:
                            report_data[k] = v
    except Exception as e:
        print(f"Warning: Failed to load '{filepath}': {e}")

# Process command-line inputs
for arg in sys.argv[1:]:
    if os.path.isdir(arg):
        for filename in sorted(os.listdir(arg)):
            if filename.endswith(".json") and filename != "queries.json":
                load_json_file(os.path.join(arg, filename))
    elif os.path.isfile(arg):
        load_json_file(arg)

if not report_data:
    print("Error: No valid database report JSON data found in the provided inputs.")
    sys.exit(1)

print(f"Loaded {len(report_data)} instance(s) into report generator.")

# Load raw SQL queries for extracting header column names on failed queries
try:
    with open("queries.json", "r") as f:
        queries_definitions = json.load(f)
except Exception:
    queries_definitions = {}

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
        "label": display_label
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


def get_sql_headers(sql_query):
    """Extracts column header names/aliases from raw SQL string."""
    try:
        select_part = sql_query.split(" FROM ")[0].split(" from ")[0]
        select_part = re.sub(r'(?i)^SELECT\s+', '', select_part)
        columns = select_part.split(',')
        headers = []
        for col in columns:
            col = col.strip()
            if ' AS ' in col.upper():
                alias = re.split(r'\s+AS\s+', col, flags=re.IGNORECASE)[-1]
                alias = alias.strip(" `\"'")
                headers.append(alias)
            else:
                col_name = col.split('.')[-1].strip(" `\"'")
                headers.append(col_name)
        return headers if headers else ["Metric Data"]
    except Exception:
        return ["Metric Data"]


def build_paginated_table(rows, table_id, sql_query=""):
    if not rows or not isinstance(rows, list):
        return '<p class="no-issues">No issues or records flagged.</p>'

    # ── CASE 1: 0 Records Returned ──
    if len(rows) == 1 and isinstance(rows[0], dict):
        first_row = rows[0]
        if first_row.get("message") == "0 records returned":
            return '<p class="no-issues">No issues or records flagged.</p>'

    # ── CASE 2: Query Failed / Permission Error (Headers Only, Empty tbody) ──
    if len(rows) > 0 and isinstance(rows[0], dict):
        first_row = rows[0]
        if "error" in first_row or first_row.get("status") == "ERROR" or "Result unavailable" in str(first_row.get("message")):
            headers = get_sql_headers(sql_query) if sql_query else [""]
            html  = f'<div class="table-wrapper" id="wrapper-{table_id}">'
            html += f'<table id="tbl-{table_id}"><thead><tr>'
            html += "".join([f"<th>{h}</th>" for h in headers])
            html += "</tr></thead><tbody>"
            html += "<!-- Empty body due to execution failure or missing permissions -->"
            html += "</tbody></table></div>"
            return html

    # ── CASE 3: Normal Data Table Rendering ──
    headers = list(rows[0].keys())
    html  = f'<div class="table-wrapper" id="wrapper-{table_id}">'
    html += f'<table id="tbl-{table_id}"><thead><tr>'
    html += "".join([f"<th>{h}</th>" for h in headers])
    html += "</tr></thead><tbody>"

    for i, row in enumerate(rows):
        rc    = row_class(row)
        style = "" if i < 10 else ' style="display:none"'
        html += f'<tr class="page-row {rc}"{style}>'
        for h in headers:
            val = str(row.get(h)) if row.get(h) is not None else "N/A"
            if any(x in val for x in ["EXPIRED", "NO PASSWORD", "FAILED"]):
                val = f'<span class="alert-badge">{val}</span>'
            elif any(x in val for x in ["NEVER EXPIRES", "WARNING"]):
                val = f'<span class="warn-badge">{val}</span>'
            html += f"<td>{val}</td>"
        html += "</tr>"

    html += "</tbody></table>"

    total_pages = (len(rows) + 9) // 10
    if total_pages > 1:
        html += f"""
        <div class="pagination">
            <button onclick="changePage('{table_id}', -1)">&lt; Prev</button>
            <span id="page-info-{table_id}">Page 1 of {total_pages}</span>
            <button onclick="changePage('{table_id}', 1)">Next &gt;</button>
        </div>"""

    html += "</div>"
    return html


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
    <!-- Import Montserrat and Inter from Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: 'Inter', Arial, Helvetica, sans-serif; 
            font-variant-numeric: tabular-nums; 
            background: #f1f5f9; 
            color: #1e293b; 
        }}

        h1, .section-title, .category-title, .instance-title {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}

        /* ── FROZEN TOP BAR ── */
        #topbar {{
            position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
            background: #0f172a; color: white;
            padding: 12px 24px; display: flex; align-items: center;
            gap: 16px; flex-wrap: wrap;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
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

        /* ── MAIN CONTENT ── */
        #content {{ margin-top: 72px; padding: 24px; }}

        /* ── PROVISIONED SPECS — single dynamic row ── */
        .specs-grid {{
            display: flex;
            flex-wrap: nowrap;
            overflow-x: auto;
            gap: 10px;
            margin-bottom: 16px;
            padding-bottom: 6px;
        }}
        .spec-item {{
            flex: 1 1 auto;
            min-width: 100px;
            white-space: nowrap;
            background: #f8fafc;
            border-radius: 6px;
            padding: 10px 14px;
            border-left: 4px solid #2563eb;
        }}
        .spec-label {{
            font-size: 0.68em; color: #64748b;
            text-transform: uppercase; letter-spacing: 0.04em;
            margin-bottom: 4px; font-weight: 600;
        }}
        .spec-value {{ font-size: 0.95em; font-weight: 700; color: #0f172a; }}

        /* ── MONITORING METRICS ── */
        .metrics-table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 0.88em; }}
        .metrics-table th {{ 
            background: #1e40af; color: white; padding: 10px 12px; text-align: left; 
            font-family: 'Montserrat', sans-serif; font-weight: 600;
        }}
        .metrics-table td {{ border: 1px solid #e2e8f0; padding: 9px 12px; }}
        .metrics-table tr:nth-child(even) td {{ background: #f8fafc; }}

        /* ── INSTANCE CARDS ── */
        .instance-block {{ margin-bottom: 32px; }}

        .instance-title {{
            padding: 12px 20px; background: #e2e8f0;
            border-radius: 8px 8px 0 0; border-left: 5px solid #2563eb;
            font-family: 'Montserrat', sans-serif; font-size: 1.15em;
        }}
        .breadcrumb-project {{ font-weight: 500; color: #64748b; }}
        .breadcrumb-separator {{ margin: 0 8px; color: #94a3b8; font-weight: 400; }}
        .breadcrumb-instance {{ font-weight: 700; color: #0f172a; }}

        .section-card {{
            background: white; border-radius: 0 0 8px 8px;
            padding: 20px; margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}
        .section-title {{
            font-size: 1.05em; color: #0f172a;
            margin-bottom: 14px; padding-bottom: 8px;
            border-bottom: 2px solid #e2e8f0;
        }}

        /* ── CATEGORY ── */
        .category-title {{
            font-size: 1em; color: #2563eb;
            margin-top: 16px; padding: 8px 0;
            border-bottom: 1px solid #e2e8f0; cursor: pointer;
            display: flex; justify-content: space-between;
        }}
        .category-title:hover {{ color: #1d4ed8; }}
        .metric-title {{
            font-size: 0.9em; font-weight: 600; color: #334155;
            margin: 14px 0 6px;
        }}

        /* ── TABLES ── */
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 0.83em; }}
        th {{ 
            background: #334155; color: white; padding: 9px 12px; text-align: left; 
            font-family: 'Montserrat', sans-serif; font-weight: 600;
        }}
        td {{ border: 1px solid #e2e8f0; padding: 8px 12px; word-break: break-word; max-width: 400px; }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        tr.row-critical td {{ background: #fee2e2 !important; }}
        tr.row-warning  td {{ background: #fef9c3 !important; }}
        tr:hover         td {{ background: #eff6ff !important; }}

        /* ── PAGINATION ── */
        .pagination {{
            display: flex; align-items: center; gap: 10px;
            padding: 6px 0; font-size: 0.72em; color: #64748b;
        }}
        .pagination button {{
            padding: 3px 10px; border-radius: 4px;
            border: 1px solid #cbd5e1; background: white;
            cursor: pointer; font-size: 0.95em; font-weight: 600;
            color: #334155;
        }}
        .pagination button:hover {{ background: #e2e8f0; }}
        .pagination button:disabled {{ opacity: 0.4; cursor: default; }}

        .alert-badge {{ font-weight: 700; color: #991b1b; background: #fca5a5; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
        .warn-badge  {{ font-weight: 700; color: #854d0e; background: #fde047; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
        .no-issues   {{ color: #16a34a; font-style: italic; font-weight: 500; font-size: 0.95em; padding: 6px 0 12px 0; margin: 0; }}
        .error-box   {{ color: #7f1d1d; background: #fee2e2; border-left: 4px solid #b91c1c; padding: 12px 16px; border-radius: 4px; font-size: 0.88em; margin: 8px 0; }}
        .hidden      {{ display: none !important; }}
    </style>
</head>
<body>

<!-- FROZEN TOP BAR -->
<div id="topbar">
    <h1>Database Health Report</h1>
    <select id="filter-dbtype" onchange="filterByType()">
        <option value="ALL">All Database Engines</option>
        <option value="PostgreSQL">PostgreSQL</option>
        <option value="MySQL">MySQL</option>
    </select>
    <select id="filter-server" onchange="filterByServer()">
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

SPEC_RENAME = {
    "Read Replica count": "Replicas",
    "Last Backup time":   "Last Backup",
}

for unique_key, data in report_data.items():
    safe_instance = unique_key.replace(":", "-").replace(" ", "_")
    
    instance_name = data.get("instance_name", unique_key)
    project_id    = data.get("project_id", "")
    specs         = data.get("provisioned_specs", {})
    utilization   = data.get("resource_utilization", {})
    health_checks = data.get("health_checks", {})
    
    db_type_label = nav_data.get(unique_key, {}).get("type", "Unknown")

    html_content += f'<div class="instance-block" data-server="{unique_key}" data-dbtype="{db_type_label}" id="srv-{safe_instance}">'
    
    # Instance Header with Cloud Breadcrumb Styling
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
            display_key = SPEC_RENAME.get(key, key)
            html_content += f'''
            <div class="spec-item">
                <div class="spec-label">{display_key}</div>
                <div class="spec-value">{val if val is not None else "N/A"}</div>
            </div>'''
        html_content += '</div>'
    elif "Error" in specs:
        html_content += f'<div class="error-box"><strong>Configuration Error:</strong><br>{specs["Error"]}</div>'

    # ── Resource Utilization (Uses 'header-name' key from new JSON structure) ──
    if utilization:
        html_content += '<div class="section-title" style="margin-top:20px;">Resource Utilization (Last 24h)</div>'
        html_content += '''<table class="metrics-table">
        <thead><tr><th>Metric</th><th>Mean</th><th>P95</th><th>P99</th><th>Max</th></tr></thead><tbody>'''
        
        for metric_key, m in utilization.items():
            if not isinstance(m, dict):
                continue
            label = m.get("header-name") or format_clean_title(metric_key)
            html_content += f'''<tr>
                <td><strong>{label}</strong></td>
                <td>{m.get("mean") if m.get("mean") is not None else "N/A"}</td>
                <td>{m.get("p95")  if m.get("p95")  is not None else "N/A"}</td>
                <td>{m.get("p99")  if m.get("p99")  is not None else "N/A"}</td>
                <td>{m.get("max")  if m.get("max")  is not None else "N/A"}</td>
            </tr>'''

        html_content += '</tbody></table>'

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
            html_content += f'''
            <div class="category-title" onclick="toggleCategory(this)">
                <span>{clean_category}</span><span>&#9654;</span>
            </div>
            <div class="category-content hidden">'''

            for metric_name, rows in queries.items():
                table_counter[0] += 1
                tid          = f"{safe_instance}_{category}_{metric_name}_{table_counter[0]}"
                clean_metric = format_clean_title(metric_name)
                html_content += f'<div class="metric-title">{clean_metric}</div>'

                sql_query = queries_definitions.get(db_type_label.lower(), {}).get(category, {}).get(metric_name, "")
                html_content += build_paginated_table(rows, tid, sql_query=sql_query)

            html_content += '</div>'  # close category-content

    html_content += '</div>'  # close instance-block

# ─────────────────────────────────────────────
# JAVASCRIPT
# ─────────────────────────────────────────────

nav_json = json.dumps(nav_data)

html_content += f"""
</div>

<script>
const navData = {nav_json};

const serverSel = document.getElementById('filter-server');

// Populate initial Server Dropdown
Object.entries(navData).forEach(([key, info]) => {{
    const o = document.createElement('option');
    o.value = key; 
    o.textContent = info.label;
    serverSel.appendChild(o);
}});

function filterByType() {{
    const type = document.getElementById('filter-dbtype').value;
    serverSel.innerHTML = '<option value="ALL">All Instances</option>';
    
    Object.entries(navData).forEach(([key, info]) => {{
        if (type === 'ALL' || info.type === type) {{
            const o = document.createElement('option');
            o.value = key; 
            o.textContent = info.label;
            serverSel.appendChild(o);
        }}
    }});
    filterByServer();
}}

function filterByServer() {{
    const type = document.getElementById('filter-dbtype').value;
    const srv  = serverSel.value;
    
    document.querySelectorAll('.instance-block').forEach(block => {{
        const typeMatch = type === 'ALL' || block.dataset.dbtype === type;
        const srvMatch  = srv   === 'ALL' || block.dataset.server === srv;
        block.classList.toggle('hidden', !(typeMatch && srvMatch));
    }});
}}

function toggleCategory(el) {{
    const content  = el.nextElementSibling;
    const arrow    = el.querySelector('span:last-child');
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
</script>
</body>
</html>"""

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Database_Health_Report.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"[OK] Report saved to: {output_path}")
