import json
import os
from datetime import datetime

with open("database_health_report.json", "r") as f:
    report_data = json.load(f)

generated_at = datetime.now().strftime("%d %b %Y, %I:%M %p")

# ─────────────────────────────────────────────
# BUILD NAVIGATION DATA FOR DROPDOWNS
# ─────────────────────────────────────────────

nav_data = {}
for instance, data in report_data.items():
    health_checks = data.get("health_checks", {})
    nav_data[instance] = [db for db in health_checks.keys() if db != "error" and db != "connection_error"]

# ─────────────────────────────────────────────
# HELPER — COMPUTE ISSUE COUNT PER CATEGORY
# ─────────────────────────────────────────────

def count_issues(categories):
    counts = {}
    for category, queries in categories.items():
        if not isinstance(queries, dict):
            continue
        total = sum(len(rows) for rows in queries.values() if isinstance(rows, list))
        counts[category] = total
    return counts

# ─────────────────────────────────────────────
# HELPER — ROW COLOR BY VALUE
# ─────────────────────────────────────────────

def row_class(row):
    vals = " ".join(str(v) for v in row.values())
    if any(x in vals for x in ["EXPIRED", "NO PASSWORD"]):
        return "row-critical"
    if any(x in vals for x in ["NEVER EXPIRES", "WARNING"]):
        return "row-warning"
    return ""

# ─────────────────────────────────────────────
# HELPER — BUILD A TABLE FROM LIST OF DICTS
# ─────────────────────────────────────────────

def build_table(rows):
    if not rows:
        return '<p class="no-issues">✅ No issues or records flagged.</p>'
    headers = list(rows[0].keys())
    html = '<table><thead><tr>' + ''.join([f'<th>{h}</th>' for h in headers]) + '</tr></thead><tbody>'
    for row in rows:
        rc = row_class(row)
        html += f'<tr class="{rc}">'
        for h in headers:
            val = str(row[h])
            if any(x in val for x in ["EXPIRED", "NO PASSWORD"]):
                val = f'<span class="alert-badge">{val}</span>'
            elif any(x in val for x in ["NEVER EXPIRES", "WARNING"]):
                val = f'<span class="warn-badge">{val}</span>'
            html += f'<td>{val}</td>'
        html += '</tr>'
    html += '</tbody></table>'
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
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f1f5f9; color: #1e293b; }}

        /* ── FROZEN TOP BAR ── */
        #topbar {{
            position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
            background: #0f172a; color: white;
            padding: 12px 24px; display: flex; align-items: center;
            gap: 16px; flex-wrap: wrap;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }}
        #topbar h1 {{ font-size: 1em; font-weight: 700; color: white; white-space: nowrap; }}
        #topbar select {{
            padding: 6px 10px; border-radius: 6px; border: none;
            background: #1e293b; color: white; font-size: 0.85em; cursor: pointer;
        }}
        #search-box {{
            padding: 6px 10px; border-radius: 6px; border: none;
            background: #1e293b; color: white; font-size: 0.85em; width: 200px;
        }}
        #search-box::placeholder {{ color: #94a3b8; }}
        .topbar-timestamp {{ margin-left: auto; font-size: 0.75em; color: #94a3b8; white-space: nowrap; }}

        /* ── MAIN CONTENT ── */
        #content {{ margin-top: 70px; padding: 24px; }}

        /* ── SUMMARY CARDS ── */
        .summary-grid {{
            display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 12px; margin-bottom: 24px;
        }}
        .summary-card {{
            background: white; border-radius: 8px; padding: 14px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-top: 4px solid #2563eb;
        }}
        .summary-card.has-issues {{ border-top-color: #dc2626; }}
        .summary-card-title {{ font-size: 0.75em; color: #64748b; margin-bottom: 4px; text-transform: uppercase; }}
        .summary-card-server {{ font-size: 0.85em; font-weight: 700; color: #0f172a; margin-bottom: 6px; word-break: break-all; }}
        .summary-card-db {{ font-size: 0.8em; color: #334155; margin-bottom: 6px; }}
        .summary-badges {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }}
        .cat-badge {{ font-size: 0.7em; padding: 2px 6px; border-radius: 10px; background: #dcfce7; color: #166534; }}
        .cat-badge.has-issues {{ background: #fee2e2; color: #991b1b; }}

        /* ── PROVISIONED SPECS ── */
        .specs-grid {{
            display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 8px; margin-bottom: 16px;
        }}
        .spec-item {{
            background: #f8fafc; border-radius: 6px; padding: 10px;
            border-left: 3px solid #2563eb;
        }}
        .spec-label {{ font-size: 0.7em; color: #64748b; text-transform: uppercase; margin-bottom: 2px; }}
        .spec-value {{ font-size: 0.9em; font-weight: 700; color: #0f172a; }}

        /* ── MONITORING METRICS ── */
        .metrics-table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 0.82em; }}
        .metrics-table th {{ background: #1e40af; color: white; padding: 8px 10px; text-align: left; }}
        .metrics-table td {{ border: 1px solid #e2e8f0; padding: 8px 10px; }}
        .metrics-table tr:nth-child(even) td {{ background: #f8fafc; }}

        /* ── INSTANCE / DB CARDS ── */
        .instance-block {{ margin-bottom: 32px; }}
        .instance-title {{
            font-size: 1.1em; font-weight: 700; color: #0f172a;
            padding: 10px 16px; background: #e2e8f0;
            border-radius: 8px 8px 0 0; border-left: 5px solid #2563eb;
        }}
        .section-card {{
            background: white; border-radius: 0 0 8px 8px;
            padding: 16px; margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }}
        .db-card {{
            background: white; border-radius: 6px;
            padding: 16px; margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            border-left: 5px solid #3b82f6;
        }}
        .db-title {{ font-size: 1em; font-weight: 700; color: #1e40af; margin-bottom: 12px; }}
        .section-title {{
            font-size: 1em; font-weight: 700; color: #0f172a;
            margin-bottom: 12px; padding-bottom: 6px;
            border-bottom: 2px solid #e2e8f0;
        }}

        /* ── CATEGORY ── */
        .category-title {{
            font-size: 0.9em; font-weight: 700; color: #2563eb;
            margin-top: 14px; padding: 6px 0;
            border-bottom: 1px solid #e2e8f0; cursor: pointer;
            display: flex; justify-content: space-between;
        }}
        .category-title:hover {{ color: #1d4ed8; }}
        .metric-title {{ font-size: 0.85em; font-weight: 600; color: #334155; margin: 10px 0 4px; }}

        /* ── TABLES ── */
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 0.82em; }}
        th {{ background: #334155; color: white; padding: 9px 10px; text-align: left; }}
        td {{ border: 1px solid #e2e8f0; padding: 8px 10px; }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        tr.row-critical td {{ background: #fee2e2 !important; }}
        tr.row-warning td {{ background: #fef9c3 !important; }}
        tr:hover td {{ background: #eff6ff !important; }}

        .alert-badge {{ font-weight: 700; color: #991b1b; background: #fee2e2; padding: 2px 6px; border-radius: 4px; }}
        .warn-badge {{ font-weight: 700; color: #854d0e; background: #fef9c3; padding: 2px 6px; border-radius: 4px; }}
        .no-issues {{ color: #16a34a; font-style: italic; font-size: 0.85em; padding: 4px 0; }}
        .error-box {{ color: #991b1b; background: #fee2e2; padding: 10px; border-radius: 6px; font-size: 0.85em; margin: 8px 0; }}
        .highlight {{ background: #fde68a; border-radius: 2px; }}
        .hidden {{ display: none !important; }}
    </style>
</head>
<body>

<!-- FROZEN TOP BAR -->
<div id="topbar">
    <h1>🏥 DB Health Report</h1>
    <select id="filter-server" onchange="filterServer()">
        <option value="ALL">All Instances</option>
    </select>
    <select id="filter-db" onchange="filterDb()">
        <option value="ALL">All Databases</option>
    </select>
    <input id="search-box" type="text" placeholder="🔍 Search..." oninput="searchReport()">
    <span class="topbar-timestamp">Generated: {generated_at}</span>
</div>

<div id="content">
"""

# ─────────────────────────────────────────────
# SUMMARY DASHBOARD
# ─────────────────────────────────────────────

html_content += '<div class="summary-grid" id="summary-grid">'

for instance, data in report_data.items():
    health_checks = data.get("health_checks", {})
    specs = data.get("provisioned_specs", {})
    engine = specs.get("Engine", "Unknown")

    if "connection_error" in health_checks or "Error" in specs:
        html_content += f'''
        <div class="summary-card has-issues">
            <div class="summary-card-title">Instance</div>
            <div class="summary-card-server">{instance}</div>
            <div class="summary-badges"><span class="cat-badge has-issues">❌ Connection Failed</span></div>
        </div>'''
        continue

    for db_name, categories in health_checks.items():
        if not isinstance(categories, dict):
            continue
        issue_counts = count_issues(categories)
        total_issues = sum(issue_counts.values())
        card_class = "summary-card has-issues" if total_issues > 0 else "summary-card"

        html_content += f'''
        <div class="{card_class}">
            <div class="summary-card-title">{engine}</div>
            <div class="summary-card-server">{instance}</div>
            <div class="summary-card-db">🗄️ {db_name}</div>
            <div class="summary-badges">'''

        for cat, count in issue_counts.items():
            badge_class = "cat-badge has-issues" if count > 0 else "cat-badge"
            label = f"⚠️ {cat}: {count}" if count > 0 else f"✅ {cat}"
            html_content += f'<span class="{badge_class}">{label}</span>'

        html_content += '</div></div>'

html_content += '</div>'

# ─────────────────────────────────────────────
# MAIN REPORT — PER INSTANCE
# ─────────────────────────────────────────────

for instance, data in report_data.items():
    safe_instance = instance.replace(":", "-").replace(" ", "_")
    specs         = data.get("provisioned_specs", {})
    utilization   = data.get("resource_utilization", {})
    health_checks = data.get("health_checks", {})

    html_content += f'<div class="instance-block" data-server="{instance}" id="srv-{safe_instance}">'
    html_content += f'<div class="instance-title">🖥️ {instance}</div>'
    html_content += '<div class="section-card">'

    # ── SECTION 1: Provisioned Specs ──
    if specs and "Error" not in specs:
        html_content += '<div class="section-title">📋 Provisioned Specs</div>'
        html_content += '<div class="specs-grid">'
        for key, val in specs.items():
            html_content += f'''
            <div class="spec-item">
                <div class="spec-label">{key}</div>
                <div class="spec-value">{val}</div>
            </div>'''
        html_content += '</div>'
    elif "Error" in specs:
        html_content += f'<div class="error-box">❌ {specs["Error"]}</div>'

    # ── SECTION 2: Resource Utilization ──
    if utilization:
        html_content += '<div class="section-title" style="margin-top:16px;">📊 Resource Utilization (Last 24h)</div>'
        html_content += '''<table class="metrics-table">
        <thead><tr>
            <th>Metric</th><th>Mean</th><th>P95</th><th>P99</th><th>Max</th>
        </tr></thead><tbody>'''

        metric_labels = {
            "cpu_utilization":    "CPU Utilization (%)",
            "memory_utilization": "Memory Utilization (%)",
            "disk_utilization":   "Disk Utilization (%)",
            "disk_read_ops":      "Disk Read Ops",
            "disk_write_ops":     "Disk Write Ops",
            "disk_bytes_used":    "Disk Bytes Used",
            "connections":        "Connections",
            "queries":            "Queries",
        }

        for key, label in metric_labels.items():
            m = utilization.get(key, {})
            html_content += f'''<tr>
                <td><strong>{label}</strong></td>
                <td>{m.get("mean") if m.get("mean") is not None else "N/A"}</td>
                <td>{m.get("p95")  if m.get("p95")  is not None else "N/A"}</td>
                <td>{m.get("p99")  if m.get("p99")  is not None else "N/A"}</td>
                <td>{m.get("max")  if m.get("max")  is not None else "N/A"}</td>
            </tr>'''

        html_content += '</tbody></table>'

    html_content += '</div>'  # close section-card

    # ── SECTION 3: Health Checks per Database ──
    if "connection_error" in health_checks:
        html_content += f'<div class="error-box">❌ DB Connection Error: {health_checks["connection_error"]}</div>'
    else:
        for db_name, categories in health_checks.items():
            safe_db = db_name.replace(" ", "_")
            html_content += f'<div class="db-card" data-server="{instance}" data-db="{db_name}" id="db-{safe_instance}-{safe_db}">'
            html_content += f'<div class="db-title">🗄️ Database: {db_name}</div>'

            if "error" in categories:
                html_content += f'<div class="error-box">❌ {categories["error"]}</div></div>'
                continue

            for category, queries in categories.items():
                if not isinstance(queries, dict):
                    continue
                html_content += f'''
                <div class="category-title" onclick="toggleCategory(this)">
                    <span>📂 {category.upper()}</span><span>▼</span>
                </div>
                <div class="category-content">'''

                for key, rows in queries.items():
                    html_content += f'<div class="metric-title">Metric: {key}</div>'
                    if isinstance(rows, list):
                        html_content += build_table(rows)
                    elif isinstance(rows, dict) and "error" in rows:
                        html_content += f'<div class="error-box">❌ {rows["error"]}</div>'
                    else:
                        html_content += '<p class="no-issues">✅ No issues or records flagged.</p>'

                html_content += '</div>'  # close category-content

            html_content += '</div>'  # close db-card

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
Object.keys(navData).forEach(s => {{
    const o = document.createElement('option');
    o.value = s; o.textContent = s;
    serverSel.appendChild(o);
}});

function filterServer() {{
    const srv = serverSel.value;
    const dbSel = document.getElementById('filter-db');
    dbSel.innerHTML = '<option value="ALL">All Databases</option>';
    const dbs = srv === 'ALL' ? Object.values(navData).flat() : (navData[srv] || []);
    [...new Set(dbs)].forEach(db => {{
        const o = document.createElement('option');
        o.value = db; o.textContent = db;
        dbSel.appendChild(o);
    }});
    filterDb();
}}

function filterDb() {{
    const srv = serverSel.value;
    const db  = document.getElementById('filter-db').value;
    document.querySelectorAll('.instance-block').forEach(block => {{
        const match = srv === 'ALL' || block.dataset.server === srv;
        block.classList.toggle('hidden', !match);
    }});
    document.querySelectorAll('.db-card').forEach(card => {{
        const srvMatch = srv === 'ALL' || card.dataset.server === srv;
        const dbMatch  = db  === 'ALL' || card.dataset.db    === db;
        card.classList.toggle('hidden', !(srvMatch && dbMatch));
    }});
}}

function toggleCategory(el) {{
    const content = el.nextElementSibling;
    const arrow   = el.querySelector('span:last-child');
    const isHidden = content.classList.toggle('hidden');
    arrow.textContent = isHidden ? '▶' : '▼';
}}

function searchReport() {{
    const term = document.getElementById('search-box').value.toLowerCase().trim();
    document.querySelectorAll('.highlight').forEach(el => {{ el.outerHTML = el.innerHTML; }});
    if (!term) {{
        document.querySelectorAll('.db-card, .instance-block').forEach(el => el.classList.remove('hidden'));
        return;
    }}
    document.querySelectorAll('.db-card').forEach(card => {{
        card.classList.toggle('hidden', !card.textContent.toLowerCase().includes(term));
    }});
    document.querySelectorAll('td').forEach(td => {{
        if (td.textContent.toLowerCase().includes(term)) {{
            td.innerHTML = td.innerHTML.replace(new RegExp(`(${{term}})`, 'gi'), '<span class="highlight">$1</span>');
        }}
    }});
    document.querySelectorAll('.instance-block').forEach(block => {{
        const anyVisible = [...block.querySelectorAll('.db-card')].some(c => !c.classList.contains('hidden'));
        block.classList.toggle('hidden', !anyVisible);
    }});
}}
</script>
</body>
</html>"""

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Database_Health_Report.html")
with open(output_path, "w") as f:
    f.write(html_content)

print(f"✅ Report saved to: {output_path}")
