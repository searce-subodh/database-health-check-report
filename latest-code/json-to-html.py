from datetime import datetime
import json
import os

# Load report data
with open("database_health_report.json", "r") as f:
    report_data = json.load(f)

generated_at = datetime.now().strftime("%d %b %Y, %I:%M %p")

# ─────────────────────────────────────────────
# BUILD NAVIGATION DATA
# ─────────────────────────────────────────────

nav_data = {}
for instance, data in report_data.items():
    specs = data.get("provisioned_specs", {})
    engine = specs.get("Engine", "")
    db_type = (
        "PostgreSQL"
        if "POSTGRES" in engine.upper()
        else "MySQL" if "MYSQL" in engine.upper() else "Unknown"
    )
    nav_data[instance] = db_type


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

def format_metric_label(key):
    """Formats raw JSON keys like 'cpu_utilization' into readable titles."""
    words = key.replace("_", " ").title()
    return words.replace("Cpu", "CPU").replace("Ops", "Ops")

def build_paginated_table(rows, table_id):
    """Build a table with pagination — 10 rows per page."""
    if not rows or not isinstance(rows, list):
        return '<p class="no-issues">[OK] No issues or records flagged.</p>'

    headers = list(rows[0].keys())
    html = f'<div class="table-wrapper" id="wrapper-{table_id}">'
    html += f'<table id="tbl-{table_id}"><thead><tr>'
    html += "".join([f"<th>{h}</th>" for h in headers])
    html += "</tr></thead><tbody>"

    for i, row in enumerate(rows):
        rc = row_class(row)
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

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Database Health Check Report</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        /* Clean Sans-Serif for body and headers */
        body {{ 
            font-family: system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; 
            background: #f1f5f9; 
            color: #1e293b; 
        }}
        h1, .section-title, .category-title {{
            font-weight: 700;
        }}

        /* -- FROZEN TOP BAR -- */
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
            background: #1e293b; color: white; font-size: 0.85em; cursor: pointer;
            min-width: 140px; font-family: inherit;
        }}
        #topbar select:focus {{ outline: 2px solid #3b82f6; }}
        .topbar-timestamp {{ margin-left: auto; font-size: 0.85em; color: #94a3b8; white-space: nowrap; text-align: right; line-height: 1.5; }}

        /* -- MAIN CONTENT -- */
        #content {{ margin-top: 70px; padding: 24px; }}

        /* -- PROVISIONED SPECS (FLEXBOX SINGLE LINE) -- */
        .specs-grid {{
            display: flex;
            flex-wrap: wrap; /* Allows wrapping on smaller screens */
            gap: 10px; 
            margin-bottom: 16px;
        }}
        .spec-item {{
            flex: 1 1 auto; /* Grow to fill space uniformly */
            min-width: 130px; /* Minimum width before forcing a wrap */
            background: #f8fafc; border-radius: 6px; padding: 12px 16px;
            border-left: 4px solid #2563eb;
        }}
        .spec-label {{ font-size: 0.75em; color: #64748b; text-transform: uppercase; margin-bottom: 4px; font-weight: 600; }}
        .spec-value {{ 
            font-size: 0.95em; font-weight: 700; color: #0f172a; 
            /* Monospace for specification values */
            font-family: ui-monospace, SFMono-Regular, Consolas, Monaco, monospace;
        }}

        /* -- MONITORING METRICS -- */
        .metrics-table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 0.9em; }}
        .metrics-table th {{ background: #1e40af; color: white; padding: 10px 12px; text-align: left; }}
        .metrics-table td {{ border: 1px solid #e2e8f0; padding: 10px 12px; }}
        .metrics-table tr:nth-child(even) td {{ background: #f8fafc; }}

        /* -- INSTANCE CARDS -- */
        .instance-block {{ margin-bottom: 32px; }}
        .instance-title {{
            font-size: 1.2em; color: #0f172a;
            padding: 12px 20px; background: #e2e8f0;
            border-radius: 8px 8px 0 0; border-left: 5px solid #2563eb;
        }}
        .section-card {{
            background: white; border-radius: 0 0 8px 8px;
            padding: 20px; margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}
        .section-title {{
            font-size: 1.1em; color: #0f172a;
            margin-bottom: 16px; padding-bottom: 8px;
            border-bottom: 2px solid #e2e8f0;
        }}

        /* -- CATEGORY -- */
        .category-title {{
            font-size: 1em; color: #2563eb;
            margin-top: 16px; padding: 8px 0;
            border-bottom: 1px solid #e2e8f0; cursor: pointer;
            display: flex; justify-content: space-between;
        }}
        .category-title:hover {{ color: #1d4ed8; }}
        .metric-title {{ font-size: 0.9em; font-weight: 600; color: #334155; margin: 12px 0 8px; text-transform: uppercase; }}

        /* -- TABLES -- */
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; font-size: 0.85em; }}
        th {{ background: #334155; color: white; padding: 10px 12px; text-align: left; }}
        /* Monospace font applied directly to table data cells for DBA readability */
        td {{ 
            border: 1px solid #e2e8f0; 
            padding: 8px 12px; 
            font-family: ui-monospace, SFMono-Regular, Consolas, Monaco, monospace; 
            word-break: break-word;
        }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        tr.row-critical td {{ background: #fee2e2 !important; }}
        tr.row-warning td {{ background: #fef9c3 !important; }}
        tr:hover td {{ background: #eff6ff !important; }}

        /* -- PAGINATION -- */
        .pagination {{
            display: flex; align-items: center; gap: 12px;
            padding: 8px 0; font-size: 0.9em; color: #334155; font-family: system-ui, sans-serif;
        }}
        .pagination button {{
            padding: 6px 14px; border-radius: 4px; border: 1px solid #cbd5e1;
            background: white; cursor: pointer; font-size: 0.9em; font-weight: 600;
        }}
        .pagination button:hover {{ background: #e2e8f0; }}

        .alert-badge {{ font-weight: 700; color: #991b1b; background: #fca5a5; padding: 2px 6px; border-radius: 4px; font-family: system-ui, sans-serif; font-size: 0.85em; }}
        .warn-badge  {{ font-weight: 700; color: #854d0e; background: #fde047; padding: 2px 6px; border-radius: 4px; font-family: system-ui, sans-serif; font-size: 0.85em; }}
        .no-issues   {{ color: #16a34a; font-style: italic; font-size: 0.95em; padding: 8px 0; font-family: system-ui, sans-serif; }}
        
        /* Formatted Error Box */
        .error-box {{ 
            color: #7f1d1d; background: #fee2e2; border-left: 4px solid #b91c1c; 
            padding: 12px 16px; border-radius: 4px; font-size: 0.9em; margin: 8px 0; 
            font-family: ui-monospace, SFMono-Regular, Consolas, Monaco, monospace;
        }}
        .hidden      {{ display: none !important; }}
    </style>
</head>
<body>

<!-- FROZEN TOP BAR -->
<div id="topbar">
    <h1>Database Health Report</h1>
    <select id="filter-dbtype" onchange="filterByType()">
        <option value="ALL">All DB Types</option>
        <option value="PostgreSQL">PostgreSQL</option>
        <option value="MySQL">MySQL</option>
    </select>
    <select id="filter-server" onchange="filterByServer()">
        <option value="ALL">All Servers</option>
    </select>
    <div class="topbar-timestamp">
        Report Period:<br>
        {list(report_data.values())[0].get('report_window', {}).get('from', generated_at)}
        &rarr; {list(report_data.values())[0].get('report_window', {}).get('to', generated_at)}
    </div>
</div>

<div id="content">
"""

# ─────────────────────────────────────────────
# MAIN REPORT — PER INSTANCE
# ─────────────────────────────────────────────

table_counter = [0]

for instance, data in report_data.items():
    safe_instance = instance.replace(":", "-").replace(" ", "_")
    specs = data.get("provisioned_specs", {})
    utilization = data.get("resource_utilization", {})
    health_checks = data.get("health_checks", {})
    db_type_label = nav_data.get(instance, "Unknown")

    html_content += f'<div class="instance-block" data-server="{instance}" data-dbtype="{db_type_label}" id="srv-{safe_instance}">'
    html_content += f'<div class="instance-title">Server: {instance} <small style="font-weight:400;font-size:0.85em;color:#64748b;">({db_type_label})</small></div>'
    html_content += '<div class="section-card">'

    # -- Provisioned Specs --
    if specs and "Error" not in specs:
        html_content += (
            '<div class="section-title">[Specs] Provisioned Specifications</div>'
        )
        html_content += '<div class="specs-grid">'
        for key, val in specs.items():
            html_content += f"""
                <div class="spec-item">
                    <div class="spec-label">{key}</div>
                    <div class="spec-value">{val if val is not None else "N/A"}</div>
                </div>"""
        html_content += "</div>"
    elif "Error" in specs:
        html_content += f'<div class="error-box"><strong>Configuration Error:</strong><br>{specs["Error"]}</div>'

    # -- Resource Utilization (Now 100% Dynamic) --
    if utilization:
        html_content += (
            '<div class="section-title" style="margin-top:24px;">[Metrics]'
            " Resource Utilization (Last 24h)</div>"
        )
        html_content += """<table class="metrics-table">
            <thead><tr><th>Metric</th><th>Mean</th><th>P95</th><th>P99</th><th>Max</th></tr></thead><tbody>"""

        for metric_key, m in utilization.items():
            if not isinstance(m, dict):
                continue
            
            label = format_metric_label(metric_key)
            html_content += f"""<tr>
                <td style="font-family: system-ui, sans-serif; font-weight: 600;">{label}</td>
                <td>{m.get("mean") if m.get("mean") is not None else "N/A"}</td>
                <td>{m.get("p95")  if m.get("p95")  is not None else "N/A"}</td>
                <td>{m.get("p99")  if m.get("p99")  is not None else "N/A"}</td>
                <td>{m.get("max")  if m.get("max")  is not None else "N/A"}</td>
            </tr>"""

        html_content += "</tbody></table>"

    html_content += "</div>"  # close section-card

    # -- Health Checks --
    if "connection_error" in health_checks:
        html_content += (
            f'<div class="error-box"><strong>Connection Error:</strong><br>{health_checks["connection_error"]}</div>'
        )
    elif isinstance(health_checks, dict):
        for category, metrics in health_checks.items():
            if not isinstance(metrics, dict):
                continue

            html_content += f"""
                <div class="category-title" onclick="toggleCategory(this)">
                    <span>[Category] {category.upper()}</span><span>&#9660;</span>
                </div>
                <div class="category-content">"""

            for metric_name, rows in metrics.items():
                table_counter[0] += 1
                tid = f"{safe_instance}_{category}_{metric_name}_{table_counter[0]}"
                
                html_content += f'<div class="metric-title">Metric: {format_metric_label(metric_name)}</div>'
                
                # Check for explicit error payloads in the metric execution
                if isinstance(rows, dict) and "error" in rows:
                    err_msg = str(rows.get("error", "Unknown error occurred."))
                    html_content += f'<div class="error-box"><strong>Query Failed:</strong><br>{err_msg}</div>'
                elif isinstance(rows, dict) and "status" in rows and rows["status"] == "FAILED":
                    html_content += f'<div class="error-box"><strong>Execution Failed:</strong> Data retrieval was unsuccessful.</div>'
                else:
                    html_content += build_paginated_table(rows, tid)

            html_content += "</div>"

    html_content += "</div>"  # close instance-block

# ─────────────────────────────────────────────
# JAVASCRIPT
# ─────────────────────────────────────────────

nav_json = json.dumps(nav_data)

html_content += f"""
</div>

<script>
const navData = {nav_json};

// -- POPULATE SERVER DROPDOWN --
const serverSel = document.getElementById('filter-server');
Object.keys(navData).forEach(s => {{
    const o = document.createElement('option');
    o.value = s; o.textContent = s;
    serverSel.appendChild(o);
}});

// -- FILTER BY DB TYPE --
function filterByType() {{
    const type = document.getElementById('filter-dbtype').value;

    // Rebuild server dropdown filtered by type
    serverSel.innerHTML = '<option value="ALL">All Servers</option>';
    Object.entries(navData).forEach(([srv, dbtype]) => {{
        if (type === 'ALL' || dbtype === type) {{
            const o = document.createElement('option');
            o.value = srv; o.textContent = srv;
            serverSel.appendChild(o);
        }}
    }});

    filterByServer();
}}

// -- FILTER BY SERVER --
function filterByServer() {{
    const type = document.getElementById('filter-dbtype').value;
    const srv  = serverSel.value;

    document.querySelectorAll('.instance-block').forEach(block => {{
        const typeMatch = type === 'ALL' || block.dataset.dbtype === type;
        const srvMatch  = srv  === 'ALL' || block.dataset.server === srv;
        block.classList.toggle('hidden', !(typeMatch && srvMatch));
    }});
}}

// -- COLLAPSIBLE CATEGORIES --
function toggleCategory(el) {{
    const content = el.nextElementSibling;
    const arrow   = el.querySelector('span:last-child');
    const isHidden = content.classList.toggle('hidden');
    arrow.innerHTML = isHidden ? '&#9654;' : '&#9660;';
}}

// -- PAGINATION --
const pageState = {{}};

function changePage(tableId, direction) {{
    const rows       = document.querySelectorAll(`#tbl-${{tableId}} tbody .page-row`);
    const totalPages = Math.ceil(rows.length / 10);

    if (!pageState[tableId]) pageState[tableId] = 1;
    pageState[tableId] = Math.max(1, Math.min(totalPages, pageState[tableId] + direction));

    const currentPage = pageState[tableId];
    const start        = (currentPage - 1) * 10;
    const end          = start + 10;

    rows.forEach((row, i) => {{
        row.style.display = (i >= start && i < end) ? '' : 'none';
    }});

    const info = document.getElementById(`page-info-${{tableId}}`);
    if (info) info.textContent = `Page ${{currentPage}} of ${{totalPages}}`;
}}
</script>
</body>
</html>"""

output_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "Database_Health_Report.html"
)
with open(output_path, "w") as f:
    f.write(html_content)

print(f"[OK] Report saved to: {output_path}")