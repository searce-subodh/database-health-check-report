from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import json
import os
import re
import sys
from google.cloud import storage

# ==========================================
# 1. FETCH & PARSE HTML REPORTS FROM GCS
# ==========================================

def parse_date_from_filename(filename):
    """Extracts date from DD-MM-YYYY_Database_Health_Report.html format."""
    match = re.search(r'(\d{2}-\d{2}-\d{4})', filename)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d-%m-%Y")
        except ValueError:
            return None
    return None

def collect_instance_data_from_html_gcs(bucket_name, days_window=7):
    """
    Connects directly to GCS bucket and iterates over daily HTML reports:
    gs://bucket_name/<project_id>/<date>_Database_Health_Report.html
    """
    print(f"📡 Connecting to GCS Bucket: 'gs://{bucket_name}'...")
    storage_client = storage.Client()
    
    try:
        bucket = storage_client.bucket(bucket_name)
        blobs = list(bucket.list_blobs())
    except Exception as e:
        print(f"❌ Error accessing GCS bucket '{bucket_name}': {e}")
        sys.exit(1)

    if not blobs:
        print(f"⚠️️ Warning: Bucket 'gs://{bucket_name}' is empty.")
        return {}

    cutoff_date = datetime.now() - timedelta(days=days_window)
    daily_files = []

    for blob in blobs:
        if not blob.name.endswith(".html") or "7day_trend_analysis" in blob.name:
            continue

        filename = os.path.basename(blob.name)
        file_date = parse_date_from_filename(filename)
        if not file_date:
            file_date = blob.updated.replace(tzinfo=None) if blob.updated else datetime.now()

        if file_date >= cutoff_date:
            daily_files.append((file_date, blob))

    daily_files.sort(key=lambda x: x[0])
    print(f"📄 Found {len(daily_files)} daily HTML report(s) within the last {days_window} days.")

    raw_accumulator = {}

    for file_date, blob in daily_files:
        print(f"  └─ Parsing HTML Report: {blob.name}...")
        try:
            html_text = blob.download_as_text()
            soup = BeautifulSoup(html_text, 'html.parser')

            instance_blocks = soup.find_all('div', class_='instance-block')
            for block in instance_blocks:
                project_id = block.get('data-project', 'Unknown-Project')
                server_attr = block.get('data-server', '')
                
                inst_elem = block.find('span', class_='breadcrumb-instance')
                if inst_elem:
                    instance_name = inst_elem.text.strip()
                elif ":" in server_attr:
                    instance_name = server_attr.split(":")[1]
                else:
                    instance_name = server_attr or "Unknown-Instance"

                key = (project_id, instance_name)
                if key not in raw_accumulator:
                    raw_accumulator[key] = {
                        "project_id": project_id,
                        "instance_name": instance_name,
                        "days_count": 0,
                        "metrics": {},
                        "max_allowed_connections": None
                    }

                raw_accumulator[key]["days_count"] += 1

                # 1. Parse Resource Utilization Metrics Table
                metrics_table = block.find('table', class_='metrics-table')
                if metrics_table:
                    rows = metrics_table.find_all('tr')[1:]
                    for row in rows:
                        cols = row.find_all(['td', 'th'])
                        if len(cols) >= 5:
                            metric_name = cols[0].text.strip()
                            try:
                                mean_v = float(cols[1].text.strip())
                                p95_v  = float(cols[2].text.strip())
                                p99_v  = float(cols[3].text.strip())
                                max_v  = float(cols[4].text.strip())

                                if metric_name not in raw_accumulator[key]["metrics"]:
                                    raw_accumulator[key]["metrics"][metric_name] = {
                                        "means": [], "p95s": [], "p99s": [], "maxs": []
                                    }

                                raw_accumulator[key]["metrics"][metric_name]["means"].append(mean_v)
                                raw_accumulator[key]["metrics"][metric_name]["p95s"].append(p95_v)
                                raw_accumulator[key]["metrics"][metric_name]["p99s"].append(p99_v)
                                raw_accumulator[key]["metrics"][metric_name]["maxs"].append(max_v)
                            except ValueError:
                                continue

                # 2. Parse Max Connection Utilization Table
                tables = block.find_all('table')
                for tbl in tables:
                    headers = [th.text.strip() for th in tbl.find_all('th')]
                    if "Max Allowed Connections" in headers:
                        conn_rows = tbl.find_all('tr')[1:]
                        if conn_rows:
                            conn_cols = conn_rows[0].find_all('td')
                            if conn_cols:
                                try:
                                    max_conn = float(conn_cols[0].text.strip())
                                    raw_accumulator[key]["max_allowed_connections"] = max_conn
                                except ValueError:
                                    pass

        except Exception as e:
            print(f"⚠️ Failed to parse HTML blob '{blob.name}': {e}")

    projects_data = {}
    for (project_id, instance_name), inst_data in raw_accumulator.items():
        final_utilization = {}
        for metric_name, acc in inst_data["metrics"].items():
            calc_mean = round(sum(acc["means"]) / len(acc["means"]), 2) if acc["means"] else 0.0
            calc_p95  = round(sum(acc["p95s"]) / len(acc["p95s"]), 2) if acc["p95s"] else 0.0
            calc_p99  = round(sum(acc["p99s"]) / len(acc["p99s"]), 2) if acc["p99s"] else 0.0
            calc_max  = round(max(acc["maxs"]), 2) if acc["maxs"] else 0.0

            # STATISTICAL INVARIANT: Mean <= P95 <= P99 <= Max
            calc_p95 = max(calc_p95, calc_mean)
            calc_p99 = max(calc_p99, calc_p95)
            calc_max = max(calc_max, calc_p99)

            final_utilization[metric_name] = {
                "header-name": metric_name,
                "Mean": calc_mean,
                "P95": calc_p95,
                "P99": calc_p99,
                "Max": calc_max
            }

        if project_id not in projects_data:
            projects_data[project_id] = {}

        projects_data[project_id][instance_name] = {
            "project_id": project_id,
            "instance_name": instance_name,
            "days_counted": inst_data["days_count"],
            "max_allowed_connections": inst_data["max_allowed_connections"],
            "resource_utilization": final_utilization
        }

    return projects_data

# ==========================================
# 2. HYBRID HTML DASHBOARD GENERATOR (LIGHT MODE ONLY)
# ==========================================

def generate_trend_html(projects_data, output_filepath="7day_trend_analysis.html"):
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>7-Day Database Trend Analysis</title>
    <!-- Google Fonts: Inter -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-main: #f8fafc;
            --bg-card: #ffffff;
            --bg-subcard: #f1f5f9;
            --border-color: #e2e8f0;
            --text-main: #0f172a;
            --text-muted: #64748b;

            --color-mean: #0284c7;
            --color-p95:  #059669;
            --color-p99:  #d97706;
            --color-max:  #7e22ce;

            --badge-red-bg: rgba(239, 68, 68, 0.1);
            --badge-red-text: #dc2626;
            --badge-red-border: rgba(239, 68, 68, 0.3);

            --track-bg: #e2e8f0;
            --gridline-color: rgba(0, 0, 0, 0.06);
            --shadow-color: rgba(0, 0, 0, 0.05);

            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }}

        body {{
            background-color: var(--bg-main);
            color: var(--text-main);
            margin: 0;
            padding: 40px 24px;
            box-sizing: border-box;
        }}

        .container {{
            max-width: 1040px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 36px;
        }}

        .global-header {{
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .global-title {{
            font-size: 22px;
            font-weight: 700;
            margin: 0;
            color: var(--text-main);
        }}

        .instance-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 32px;
            display: flex;
            flex-direction: column;
            gap: 28px;
            box-shadow: 0 10px 25px -5px var(--shadow-color);
        }}

        .instance-header {{
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .breadcrumb {{
            font-size: 18px;
            font-weight: 700;
        }}
        .breadcrumb-project {{ color: var(--text-muted); }}
        .breadcrumb-sep {{ color: var(--border-color); margin: 0 8px; }}
        .breadcrumb-instance {{ color: var(--color-mean); }}

        .days-badge {{
            font-size: 12px;
            background: rgba(2, 132, 199, 0.08);
            color: var(--color-mean);
            font-weight: 600;
            padding: 5px 14px;
            border-radius: 20px;
            border: 1px solid rgba(2, 132, 199, 0.2);
        }}

        .section-title {{
            font-size: 13px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            margin-bottom: 16px;
        }}

        .bullet-group {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}

        .bullet-row {{
            background: var(--bg-subcard);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .bullet-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .bullet-title {{
            font-size: 14px;
            font-weight: 600;
            color: var(--text-main);
        }}

        .bullet-badges {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .badge-pill {{
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid rgba(0, 0, 0, 0.08);
        }}

        .badge-pill.mean {{ color: var(--color-mean); border-color: rgba(2, 132, 199, 0.3); background: rgba(2, 132, 199, 0.1); }}
        .badge-pill.p95  {{ color: var(--color-p95);  border-color: rgba(5, 150, 105, 0.3); background: rgba(5, 150, 105, 0.1); }}
        .badge-pill.p99  {{ color: var(--color-p99);  border-color: rgba(217, 119, 6, 0.3); background: rgba(217, 119, 6, 0.1); }}
        .badge-pill.max  {{ color: var(--color-max);  border-color: rgba(126, 34, 206, 0.3); background: rgba(126, 34, 206, 0.1); }}

        .badge-pill.critical {{
            color: var(--badge-red-text) !important;
            background: var(--badge-red-bg) !important;
            border-color: var(--badge-red-border) !important;
        }}

        .track-wrapper {{
            position: relative;
            width: 100%;
        }}

        .gridlines {{
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            display: flex;
            justify-content: space-between;
            pointer-events: none;
            z-index: 0;
        }}
        .gridline {{
            width: 1px;
            height: 100%;
            background: var(--gridline-color);
        }}

        .bullet-track {{
            position: relative;
            height: 18px;
            background: var(--track-bg);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            box-shadow: inset 0 2px 4px var(--shadow-color);
            z-index: 1;
        }}

        .bullet-bar {{
            position: absolute;
            top: 0; left: 0; height: 100%;
            border-radius: 6px;
            transition: width 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }}

        .bar-max  {{ background: var(--color-max);  z-index: 1; opacity: 0.5; }}
        .bar-p99  {{ background: var(--color-p99);  z-index: 2; opacity: 0.7; }}
        .bar-p95  {{ background: var(--color-p95);  z-index: 3; opacity: 0.85; }}
        .bar-mean {{ background: var(--color-mean); z-index: 4; opacity: 1; }}

        .ticks-row {{
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            color: var(--text-muted);
            margin-top: 4px;
        }}

        .kpi-tile-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 18px;
        }}

        .kpi-tile {{
            background: var(--bg-subcard);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            cursor: pointer;
            transition: transform 0.2s ease, border-color 0.2s ease, background-color 0.2s ease;
        }}
        .kpi-tile:hover, .kpi-tile.selected {{
            transform: translateY(-2px);
            border-color: var(--color-mean);
            background: rgba(2, 132, 199, 0.04);
        }}

        .kpi-tile-header {{
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .main-val-container {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .kpi-tile-main-val {{
            font-size: 28px;
            font-weight: 700;
            color: var(--color-mean);
            line-height: 1;
            white-space: nowrap;
        }}

        .max-allowed-badge {{
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            align-self: flex-start;
        }}

        .kpi-tile-footer {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
            border-top: 1px solid var(--border-color);
            padding-top: 10px;
            margin-top: 4px;
        }}

        .kpi-footer-item {{
            display: flex;
            flex-direction: column;
        }}

        .kpi-footer-label {{
            font-size: 10px;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
        }}

        .kpi-footer-val {{
            font-size: 12px;
            font-weight: 600;
            color: var(--text-main);
            margin-top: 2px;
        }}

        .shared-bar-container {{
            display: none;
            margin-top: 18px;
            width: 100%;
        }}

        .shared-bar-container.active {{
            display: block;
        }}

        .truncate {{
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
    </style>
</head>
<body>

    <div class="container">
        <div class="global-header">
            <h1 class="global-title">7-Day Aggregated Database Health Analysis</h1>
        </div>

        <div id="instances-list"></div>
    </div>

    <script>
        const dashboardData = {json.dumps(projects_data, indent=2)};

        function formatNumber(num) {{
            if (num === null || num === undefined) return "N/A";
            return num.toLocaleString('en-US', {{ maximumFractionDigits: 2 }});
        }}

        function formatTickValue(val) {{
            if (val >= 10) return Math.round(val).toLocaleString();
            return Number(val.toFixed(2));
        }}

        function getSnapCeiling(maxVal) {{
            if (maxVal <= 20) return 25;
            if (maxVal <= 42) return 50;
            if (maxVal <= 68) return 75;
            return 100;
        }}

        // MULTI-TIER DYNAMIC RATIO SCALING FOR COUNT METRICS
        function getDynamicCountScale(mMax) {{
            if (mMax <= 0) return 1;
            
            // Give 35% to 50% breathing room at the right end relative to actual peak usage
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
        }}

        function handleTileClick(instId, metricKey) {{
            const container = document.getElementById(`shared-bar-${{instId}}`);
            const allTiles = document.querySelectorAll(`.tile-${{instId}}`);

            if (!container) return;

            const sanitizedKey = metricKey.replace(/[^a-zA-Z0-9_-]/g, '_');
            const selectedTile = document.getElementById(`tile-${{instId}}-${{sanitizedKey}}`);
            const isAlreadyActive = selectedTile && selectedTile.classList.contains('selected') && container.classList.contains('active');

            allTiles.forEach(t => t.classList.remove('selected'));

            if (isAlreadyActive) {{
                container.classList.remove('active');
                container.innerHTML = '';
                return;
            }}

            if (selectedTile) selectedTile.classList.add('selected');

            let targetInst = null;
            Object.values(dashboardData).forEach(proj => {{
                Object.values(proj).forEach(inst => {{
                    const curId = inst.instance_name.replace(/[^a-zA-Z0-9_-]/g, '_');
                    if (curId === instId) targetInst = inst;
                }});
            }});

            if (!targetInst) return;

            const m = targetInst.resource_utilization[metricKey];
            if (!m) return;

            const label = m["header-name"] || metricKey;
            
            // Compute scale boundary dynamically relative to peak Max usage
            const dynamicScaleBound = getDynamicCountScale(m.Max);
            const calcWidth = (val) => Math.min(100, Math.max(0, (val / dynamicScaleBound) * 100));

            // Generate 5 evenly-spaced tick marks with precision formatting
            const step = dynamicScaleBound / 4;
            const ticksMarkup = `
                <div class="ticks-row">
                    <span>0</span>
                    <span>${{formatTickValue(step)}}</span>
                    <span>${{formatTickValue(step * 2)}}</span>
                    <span>${{formatTickValue(step * 3)}}</span>
                    <span>${{formatTickValue(dynamicScaleBound)}}</span>
                </div>
            `;

            container.innerHTML = `
                <div class="bullet-row">
                    <div class="bullet-header">
                        <div class="bullet-title">${{label}}</div>
                        <div class="bullet-badges">
                            <span class="badge-pill mean">Mean: ${{formatNumber(m.Mean)}}</span>
                            <span class="badge-pill p95">P95: ${{formatNumber(m.P95)}}</span>
                            <span class="badge-pill p99">P99: ${{formatNumber(m.P99)}}</span>
                            <span class="badge-pill max">Max: ${{formatNumber(m.Max)}}</span>
                        </div>
                    </div>

                    <div class="track-wrapper">
                        <div class="gridlines">
                            <div class="gridline"></div>
                            <div class="gridline"></div>
                            <div class="gridline"></div>
                            <div class="gridline"></div>
                            <div class="gridline"></div>
                        </div>

                        <div class="bullet-track">
                            <div class="bullet-bar bar-max" style="width: ${{calcWidth(m.Max)}}%;"></div>
                            <div class="bullet-bar bar-p99" style="width: ${{calcWidth(m.P99)}}%;"></div>
                            <div class="bullet-bar bar-p95" style="width: ${{calcWidth(m.P95)}}%;"></div>
                            <div class="bullet-bar bar-mean" style="width: ${{calcWidth(m.Mean)}}%;"></div>
                        </div>
                    </div>

                    ${{ticksMarkup}}
                </div>
            `;

            container.classList.add('active');
        }}

        function renderDashboard() {{
            const container = document.getElementById('instances-list');
            container.innerHTML = '';

            Object.keys(dashboardData).sort().forEach(projKey => {{
                const instanceDict = dashboardData[projKey];
                
                Object.keys(instanceDict).sort().forEach(instKey => {{
                    const inst = instanceDict[instKey];
                    const card = document.createElement('div');
                    card.className = 'instance-card';

                    const res = inst.resource_utilization || {{}};
                    const safeInstId = inst.instance_name.replace(/[^a-zA-Z0-9_-]/g, '_');

                    let pctRowsHtml = '';
                    let kpiTilesHtml = '';

                    Object.keys(res).forEach(metricKey => {{
                        const m = res[metricKey];
                        const label = m["header-name"] || metricKey;
                        const isPct = label.includes("(%)");

                        if (isPct) {{
                            const snapMax = getSnapCeiling(m.Max);
                            const calcWidth = (val) => Math.min(100, Math.max(0, (val / snapMax) * 100));
                            const isCriticalMax = m.Max >= 80;

                            pctRowsHtml += `
                                <div class="bullet-row">
                                    <div class="bullet-header">
                                        <div class="bullet-title">${{label}}</div>
                                        <div class="bullet-badges">
                                            <span class="badge-pill mean">Mean: ${{formatNumber(m.Mean)}}%</span>
                                            <span class="badge-pill p95">P95: ${{formatNumber(m.P95)}}%</span>
                                            <span class="badge-pill p99">P99: ${{formatNumber(m.P99)}}%</span>
                                            <span class="badge-pill max ${{isCriticalMax ? 'critical' : ''}}">Max: ${{formatNumber(m.Max)}}%</span>
                                        </div>
                                    </div>

                                    <div class="track-wrapper">
                                        <div class="gridlines">
                                            <div class="gridline"></div>
                                            <div class="gridline"></div>
                                            <div class="gridline"></div>
                                            <div class="gridline"></div>
                                            <div class="gridline"></div>
                                        </div>

                                        <div class="bullet-track">
                                            <div class="bullet-bar bar-max" style="width: ${{calcWidth(m.Max)}}%;"></div>
                                            <div class="bullet-bar bar-p99" style="width: ${{calcWidth(m.P99)}}%;"></div>
                                            <div class="bullet-bar bar-p95" style="width: ${{calcWidth(m.P95)}}%;"></div>
                                            <div class="bullet-bar bar-mean" style="width: ${{calcWidth(m.Mean)}}%;"></div>
                                        </div>
                                    </div>

                                    <div class="ticks-row">
                                        <span>0%</span>
                                        <span>${{Math.round(snapMax * 0.25)}}%</span>
                                        <span>${{Math.round(snapMax * 0.50)}}%</span>
                                        <span>${{Math.round(snapMax * 0.75)}}%</span>
                                        <span>${{snapMax}}%</span>
                                    </div>
                                </div>
                            `;
                        }} else {{
                            let maxAllowedStr = "N/A";
                            if (label.includes("Connections") && inst.max_allowed_connections) {{
                                maxAllowedStr = formatNumber(inst.max_allowed_connections);
                            }}

                            const sanitizedMetricKey = metricKey.replace(/[^a-zA-Z0-9_-]/g, '_');

                            kpiTilesHtml += `
                                <div id="tile-${{safeInstId}}-${{sanitizedMetricKey}}" 
                                     class="kpi-tile tile-${{safeInstId}}" 
                                     onclick="handleTileClick('${{safeInstId}}', '${{metricKey}}')">
                                    <div class="kpi-tile-header truncate" title="${{label}}">
                                        <span>${{label}}</span>
                                    </div>
                                    <div class="main-val-container">
                                        <div class="kpi-tile-main-val">${{formatNumber(m.Mean)}}</div>
                                        <div class="max-allowed-badge">Max Allowed: ${{maxAllowedStr}}</div>
                                    </div>
                                    <div class="kpi-tile-footer">
                                        <div class="kpi-footer-item truncate">
                                            <span class="kpi-footer-label" style="color: var(--color-p95);">P95</span>
                                            <span class="kpi-footer-val truncate">${{formatNumber(m.P95)}}</span>
                                        </div>
                                        <div class="kpi-footer-item truncate">
                                            <span class="kpi-footer-label" style="color: var(--color-p99);">P99</span>
                                            <span class="kpi-footer-val truncate">${{formatNumber(m.P99)}}</span>
                                        </div>
                                        <div class="kpi-footer-item truncate">
                                            <span class="kpi-footer-label" style="color: var(--color-max);">Max</span>
                                            <span class="kpi-footer-val truncate">${{formatNumber(m.Max)}}</span>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }}
                    }});

                    const pctSection = pctRowsHtml ? `
                        <div>
                            <div class="section-title">Percentage Utilization Metrics</div>
                            <div class="bullet-group">${{pctRowsHtml}}</div>
                        </div>` : '';

                    const countSection = kpiTilesHtml ? `
                        <div>
                            <div class="section-title">Count & Throughput Metrics</div>
                            <div class="kpi-tile-grid">${{kpiTilesHtml}}</div>
                            <!-- SINGLE SHARED FULL-WIDTH EXPANDABLE CONTAINER BELOW ALL TILES -->
                            <div id="shared-bar-${{safeInstId}}" class="shared-bar-container"></div>
                        </div>` : '';

                    card.innerHTML = `
                        <div class="instance-header">
                            <div class="breadcrumb">
                                <span class="breadcrumb-project">${{inst.project_id}}</span>
                                <span class="breadcrumb-sep">/</span>
                                <span class="breadcrumb-instance">${{inst.instance_name}}</span>
                            </div>
                            <span class="days-badge">${{inst.days_counted}} Day(s) Aggregated</span>
                        </div>

                        ${{pctSection}}
                        ${{countSection}}
                    `;

                    container.appendChild(card);
                }});
            }});
        }}

        renderDashboard();
    </script>
</body>
</html>"""

    with open(output_filepath, 'w', encoding='utf-8') as f:
        f.write(html_template)

    print(f"✅ Dashboard generated successfully: {output_filepath}")

# ==========================================
# 3. SCRIPT ENTRYPOINT
# ==========================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Direct GCS 7-Day Trend Analysis from HTML Reports")
    parser.add_argument("--bucket", default="db-health-report", help="Name of GCS Bucket")
    parser.add_argument("--out", default="7day_trend_analysis.html", help="Output HTML file path")
    args = parser.parse_args()

    data = collect_instance_data_from_html_gcs(bucket_name=args.bucket, days_window=7)

    if not data:
        print("❌ No valid HTML report data found in GCS for the past 7 days.")
    else:
        generate_trend_html(data, args.out)
