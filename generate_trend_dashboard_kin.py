from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import argparse
import glob
import json
import os
import re
import sys

# ==========================================
# 1. FETCH & PARSE DAILY HTML REPORTS
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


def list_report_sources(bucket_name=None, local_dir=None):
    """
    Returns a list of (name, file_date_or_None, fallback_date, read_fn) tuples.
    GCS layout:  gs://bucket_name/<project_id>/<DD-MM-YYYY>_Database_Health_Report.html
    Local mode:  any *.html under --local-dir (handy for testing without GCS)
    """
    sources = []

    if local_dir:
        print(f"📂 Reading reports from local folder: {local_dir}")
        for path in glob.glob(os.path.join(local_dir, "**", "*.html"), recursive=True):
            fallback = datetime.fromtimestamp(os.path.getmtime(path))
            sources.append((path, fallback, lambda p=path: open(p, encoding="utf-8").read()))
        return sources

    from google.cloud import storage  # imported lazily so local mode works without the SDK
    print(f"📡 Connecting to GCS Bucket: 'gs://{bucket_name}'...")
    try:
        bucket = storage.Client().bucket(bucket_name)
        blobs = list(bucket.list_blobs())
    except Exception as e:
        print(f"❌ Error accessing GCS bucket '{bucket_name}': {e}")
        sys.exit(1)

    for blob in blobs:
        fallback = blob.updated.replace(tzinfo=None) if blob.updated else datetime.now()
        sources.append((blob.name, fallback, lambda b=blob: b.download_as_text()))
    return sources


def parse_report(html_text, report_date):
    """Parses one daily report into a list of per-instance daily records."""
    soup = BeautifulSoup(html_text, "html.parser")
    records = []

    for block in soup.find_all("div", class_="instance-block"):
        project_id = block.get("data-project", "Unknown-Project")
        server_attr = block.get("data-server", "")

        inst_elem = block.find("span", class_="breadcrumb-instance")
        if inst_elem:
            instance_name = inst_elem.text.strip()
        elif ":" in server_attr:
            instance_name = server_attr.split(":")[1]
        else:
            instance_name = server_attr or "Unknown-Instance"

        metrics = {}
        metrics_table = block.find("table", class_="metrics-table")
        if metrics_table:
            for row in metrics_table.find_all("tr")[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) >= 5:
                    try:
                        metrics[cols[0].text.strip()] = [float(c.text.strip()) for c in cols[1:5]]
                    except ValueError:
                        continue

        max_conn = None
        for tbl in block.find_all("table"):
            headers = [th.text.strip() for th in tbl.find_all("th")]
            if "Max Allowed Connections" in headers:
                conn_rows = tbl.find_all("tr")[1:]
                if conn_rows:
                    conn_cols = conn_rows[0].find_all("td")
                    if conn_cols:
                        try:
                            max_conn = float(conn_cols[0].text.strip())
                        except ValueError:
                            pass

        records.append({
            "d": report_date.strftime("%Y-%m-%d"),   # report date (ISO, sorts as text)
            "p": project_id,
            "i": instance_name,
            "mc": max_conn,
            "m": metrics,                             # {metric: [mean, p95, p99, max]}
        })
    return records


def collect_daily_records(bucket_name=None, local_dir=None, history_days=90):
    """
    Loads every daily report from the last `history_days` days and returns the
    raw per-day records. Aggregation now happens in the browser, so the
    dashboard can be re-aggregated for any From/To range the user picks.
    """
    cutoff = (datetime.now() - timedelta(days=history_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_files = []

    for name, fallback_date, read_fn in list_report_sources(bucket_name, local_dir):
        base = os.path.basename(name)
        if not base.endswith(".html") or "trend_analysis" in base:
            continue
        file_date = parse_date_from_filename(base) or fallback_date
        if file_date >= cutoff:
            daily_files.append((file_date, name, read_fn))

    daily_files.sort(key=lambda x: x[0])
    print(f"📄 Found {len(daily_files)} daily HTML report(s) within the last {history_days} days.")

    records = []
    for file_date, name, read_fn in daily_files:
        print(f"  └─ Parsing HTML Report: {name}...")
        try:
            records.extend(parse_report(read_fn(), file_date))
        except Exception as e:
            print(f"⚠️ Failed to parse HTML report '{name}': {e}")
    return records

# ==========================================
# 2. HTML DASHBOARD GENERATOR (LIGHT MODE ONLY)
# ==========================================
# Plain string (not an f-string) so CSS/JS braces don't need doubling.
# The data is injected by replacing __DASHBOARD_DATA__.

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Database Trend Analysis</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
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

            --track-bg: #e2e8f0;
            --gridline-color: rgba(0, 0, 0, 0.06);
            --shadow-color: rgba(0, 0, 0, 0.05);

            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        body {
            background-color: var(--bg-main);
            color: var(--text-main);
            margin: 0;
            padding: 40px 24px;
            box-sizing: border-box;
        }

        .container {
            max-width: 1040px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 36px;
        }

        .global-header {
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px;
            display: flex;
            flex-direction: column;
            gap: 18px;
        }

        .global-title {
            font-size: 22px;
            font-weight: 700;
            margin: 0;
            color: var(--text-main);
        }

        /* ---------- Date range filter ---------- */
        .filter-bar {
            display: flex;
            flex-wrap: wrap;
            align-items: flex-end;
            gap: 14px;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 14px 18px;
        }
        .filter-field { display: flex; flex-direction: column; gap: 4px; }
        .filter-field label {
            font-size: 10px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.06em; color: var(--text-muted);
        }
        .filter-field input[type="date"] {
            font-family: inherit; font-size: 13px; color: var(--text-main);
            padding: 7px 10px; border: 1px solid var(--border-color);
            border-radius: 8px; background: var(--bg-subcard);
        }
        .filter-field input[type="date"]:focus { outline: 2px solid rgba(2,132,199,0.35); border-color: var(--color-mean); }
        .preset-group { display: flex; gap: 6px; flex-wrap: wrap; }
        .preset-btn {
            font-family: inherit; font-size: 12px; font-weight: 600;
            padding: 7px 12px; border-radius: 8px; cursor: pointer;
            border: 1px solid var(--border-color); background: var(--bg-subcard);
            color: var(--text-muted);
        }
        .preset-btn:hover { border-color: var(--color-mean); color: var(--color-mean); }
        .preset-btn.active { background: rgba(2,132,199,0.1); border-color: rgba(2,132,199,0.4); color: var(--color-mean); }
        .range-summary { font-size: 12px; color: var(--text-muted); margin-left: auto; align-self: center; }
        .range-summary strong { color: var(--text-main); }

        .empty-state {
            text-align: center; padding: 48px 24px; color: var(--text-muted);
            background: var(--bg-card); border: 1px dashed var(--border-color); border-radius: 16px;
            font-size: 14px;
        }

        #instances-list { display: flex; flex-direction: column; gap: 36px; }

        .instance-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 32px;
            display: flex;
            flex-direction: column;
            gap: 28px;
            box-shadow: 0 10px 25px -5px var(--shadow-color);
        }

        .instance-header {
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        .breadcrumb { font-size: 18px; font-weight: 700; }
        .breadcrumb-project { color: var(--text-muted); }
        .breadcrumb-sep { color: var(--border-color); margin: 0 8px; }
        .breadcrumb-instance { color: var(--color-mean); }

        .days-badge {
            font-size: 12px;
            background: rgba(2, 132, 199, 0.08);
            color: var(--color-mean);
            font-weight: 600;
            padding: 5px 14px;
            border-radius: 20px;
            border: 1px solid rgba(2, 132, 199, 0.2);
        }

        .section-title {
            font-size: 13px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            margin-bottom: 16px;
        }

        .bullet-group { display: flex; flex-direction: column; gap: 20px; }

        .bullet-row {
            background: var(--bg-subcard);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .bullet-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .bullet-title { font-size: 14px; font-weight: 600; color: var(--text-main); }

        .bullet-badges { display: flex; gap: 8px; flex-wrap: wrap; }

        .badge-pill {
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid rgba(0, 0, 0, 0.08);
        }

        .badge-pill.mean { color: var(--color-mean); border-color: rgba(2, 132, 199, 0.3); background: rgba(2, 132, 199, 0.1); }
        .badge-pill.p95  { color: var(--color-p95);  border-color: rgba(5, 150, 105, 0.3); background: rgba(5, 150, 105, 0.1); }
        .badge-pill.p99  { color: var(--color-p99);  border-color: rgba(217, 119, 6, 0.3); background: rgba(217, 119, 6, 0.1); }
        .badge-pill.max  { color: var(--color-max);  border-color: rgba(126, 34, 206, 0.3); background: rgba(126, 34, 206, 0.1); }

        .track-wrapper { position: relative; width: 100%; }

        .gridlines {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            display: flex;
            justify-content: space-between;
            pointer-events: none;
            z-index: 0;
        }
        .gridline { width: 1px; height: 100%; background: var(--gridline-color); }

        .bullet-track {
            position: relative;
            height: 18px;
            background: var(--track-bg);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            box-shadow: inset 0 2px 4px var(--shadow-color);
            z-index: 1;
        }

        .bullet-bar {
            position: absolute;
            top: 0; left: 0; height: 100%;
            border-radius: 6px;
            transition: width 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .bar-max  { background: var(--color-max);  z-index: 1; opacity: 0.5; }
        .bar-p99  { background: var(--color-p99);  z-index: 2; opacity: 0.7; }
        .bar-p95  { background: var(--color-p95);  z-index: 3; opacity: 0.85; }
        .bar-mean { background: var(--color-mean); z-index: 4; opacity: 1; }

        .ticks-row {
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            color: var(--text-muted);
            margin-top: 4px;
        }

        .kpi-tile-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 18px;
        }

        .kpi-tile {
            background: var(--bg-subcard);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            cursor: pointer;
            transition: transform 0.2s ease, border-color 0.2s ease, background-color 0.2s ease;
        }
        .kpi-tile:hover, .kpi-tile.selected {
            transform: translateY(-2px);
            border-color: var(--color-mean);
            background: rgba(2, 132, 199, 0.04);
        }

        .kpi-tile-header {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .main-val-container { display: flex; flex-direction: column; gap: 6px; }

        .kpi-tile-main-val {
            font-size: 28px;
            font-weight: 700;
            color: var(--color-mean);
            line-height: 1;
            white-space: nowrap;
        }

        .max-allowed-badge {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            align-self: flex-start;
        }

        /* 4 columns now: Mean / P95 / P99 / Max */
        .kpi-tile-footer {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(68px, 1fr));
            gap: 8px 10px;
            border-top: 1px solid var(--border-color);
            padding-top: 10px;
            margin-top: 4px;
        }

        .kpi-footer-item { display: flex; flex-direction: column; min-width: 0; }

        .kpi-footer-label {
            font-size: 10px;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
        }

        .kpi-footer-val {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-main);
            margin-top: 2px;
        }

        .shared-bar-container { display: none; margin-top: 18px; width: 100%; }
        .shared-bar-container.active { display: block; }

        .truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        @media (max-width: 600px) {
            body { padding: 24px 16px; }
            .instance-card { padding: 20px; }
            .range-summary { margin-left: 0; }
        }
    </style>
</head>
<body>

    <div class="container">
        <div class="global-header">
            <h1 class="global-title">Aggregated Database Health Analysis</h1>

            <div class="filter-bar">
                <div class="filter-field">
                    <label for="date-from">From</label>
                    <input type="date" id="date-from">
                </div>
                <div class="filter-field">
                    <label for="date-to">To</label>
                    <input type="date" id="date-to">
                </div>
                <div class="preset-group">
                    <button class="preset-btn" data-days="7">Last 7 days</button>
                    <button class="preset-btn" data-days="14">Last 14 days</button>
                    <button class="preset-btn" data-days="30">Last 30 days</button>
                    <button class="preset-btn" data-days="all">All</button>
                </div>
                <div class="range-summary" id="range-summary"></div>
            </div>
        </div>

        <div id="instances-list"></div>
    </div>

    <script>
        // Raw per-day records: { d: "YYYY-MM-DD", p: project, i: instance, mc: maxConn, m: { metric: [mean, p95, p99, max] } }
        const dailyRecords = __DASHBOARD_DATA__;

        // Aggregated view for the currently selected range (rebuilt on every change)
        let dashboardData = {};

        const availableDates = [...new Set(dailyRecords.map(r => r.d))].sort();
        const minDate = availableDates[0] || null;
        const maxDate = availableDates[availableDates.length - 1] || null;

        // ---------- helpers ----------
        const round2 = v => Math.round(v * 100) / 100;
        const avg = arr => arr.reduce((a, b) => a + b, 0) / arr.length;
        const safeId = s => s.replace(/[^a-zA-Z0-9_-]/g, '_');

        function shiftDate(iso, days) {
            const dt = new Date(iso + "T00:00:00");
            dt.setDate(dt.getDate() + days);
            const y = dt.getFullYear(), m = String(dt.getMonth() + 1).padStart(2, '0'), d = String(dt.getDate()).padStart(2, '0');
            return `${y}-${m}-${d}`;
        }

        function prettyDate(iso) {
            return new Date(iso + "T00:00:00").toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
        }

        function formatNumber(num) {
            if (num === null || num === undefined) return "N/A";
            return num.toLocaleString('en-US', { maximumFractionDigits: 2 });
        }

        function formatTickValue(val) {
            if (val >= 10) return Math.round(val).toLocaleString();
            return Number(val.toFixed(2));
        }

        function getSnapCeiling(maxVal) {
            if (maxVal <= 20) return 25;
            if (maxVal <= 42) return 50;
            if (maxVal <= 68) return 75;
            return 100;
        }

        // MULTI-TIER DYNAMIC RATIO SCALING FOR COUNT METRICS
        function getDynamicCountScale(mMax) {
            if (mMax <= 0) return 1;
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
            return Math.ceil(target / 500) * 500;
        }

        // ---------- aggregation for a date range (same rules as before) ----------
        // Mean/P95/P99 = average of the daily values; Max = highest daily max.
        function aggregate(fromIso, toIso) {
            const acc = {};
            dailyRecords
                .filter(r => r.d >= fromIso && r.d <= toIso)
                .sort((a, b) => a.d.localeCompare(b.d))
                .forEach(r => {
                    const key = r.p + "\u0000" + r.i;
                    if (!acc[key]) acc[key] = { project_id: r.p, instance_name: r.i, dates: new Set(), maxConn: null, metrics: {} };
                    const a = acc[key];
                    a.dates.add(r.d);
                    if (r.mc !== null && r.mc !== undefined) a.maxConn = r.mc;  // latest in range wins
                    Object.entries(r.m).forEach(([name, vals]) => {
                        if (!a.metrics[name]) a.metrics[name] = { means: [], p95s: [], p99s: [], maxs: [] };
                        a.metrics[name].means.push(vals[0]);
                        a.metrics[name].p95s.push(vals[1]);
                        a.metrics[name].p99s.push(vals[2]);
                        a.metrics[name].maxs.push(vals[3]);
                    });
                });

            const out = {};
            Object.values(acc).forEach(a => {
                const util = {};
                Object.entries(a.metrics).forEach(([name, m]) => {
                    const mean = round2(avg(m.means));
                    let p95 = round2(avg(m.p95s));
                    let p99 = round2(avg(m.p99s));
                    let max = round2(Math.max(...m.maxs));
                    // STATISTICAL INVARIANT: Mean <= P95 <= P99 <= Max
                    p95 = Math.max(p95, mean);
                    p99 = Math.max(p99, p95);
                    max = Math.max(max, p99);
                    util[name] = { "header-name": name, Mean: mean, P95: p95, P99: p99, Max: max };
                });
                if (!out[a.project_id]) out[a.project_id] = {};
                out[a.project_id][a.instance_name] = {
                    project_id: a.project_id,
                    instance_name: a.instance_name,
                    days_counted: a.dates.size,
                    max_allowed_connections: a.maxConn,
                    resource_utilization: util
                };
            });
            return out;
        }

        // ---------- expandable bar for count tiles ----------
        function findInstance(instId) {
            let target = null;
            Object.values(dashboardData).forEach(proj => {
                Object.values(proj).forEach(inst => {
                    if (safeId(inst.project_id + "__" + inst.instance_name) === instId) target = inst;
                });
            });
            return target;
        }

        function handleTileClick(instId, metricKey) {
            const container = document.getElementById(`shared-bar-${instId}`);
            const allTiles = document.querySelectorAll(`.tile-${instId}`);
            if (!container) return;

            const selectedTile = document.getElementById(`tile-${instId}-${safeId(metricKey)}`);
            const isAlreadyActive = selectedTile && selectedTile.classList.contains('selected') && container.classList.contains('active');

            allTiles.forEach(t => t.classList.remove('selected'));

            if (isAlreadyActive) {
                container.classList.remove('active');
                container.innerHTML = '';
                return;
            }

            if (selectedTile) selectedTile.classList.add('selected');

            const targetInst = findInstance(instId);
            if (!targetInst) return;
            const m = targetInst.resource_utilization[metricKey];
            if (!m) return;

            const label = m["header-name"] || metricKey;
            const dynamicScaleBound = getDynamicCountScale(m.Max);
            const calcWidth = (val) => Math.min(100, Math.max(0, (val / dynamicScaleBound) * 100));
            const step = dynamicScaleBound / 4;

            container.innerHTML = `
                <div class="bullet-row">
                    <div class="bullet-header">
                        <div class="bullet-title">${label}</div>
                        <div class="bullet-badges">
                            <span class="badge-pill mean">Mean: ${formatNumber(m.Mean)}</span>
                            <span class="badge-pill p95">P95: ${formatNumber(m.P95)}</span>
                            <span class="badge-pill p99">P99: ${formatNumber(m.P99)}</span>
                            <span class="badge-pill max">Max: ${formatNumber(m.Max)}</span>
                        </div>
                    </div>
                    <div class="track-wrapper">
                        <div class="gridlines">
                            <div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div>
                        </div>
                        <div class="bullet-track">
                            <div class="bullet-bar bar-max" style="width: ${calcWidth(m.Max)}%;"></div>
                            <div class="bullet-bar bar-p99" style="width: ${calcWidth(m.P99)}%;"></div>
                            <div class="bullet-bar bar-p95" style="width: ${calcWidth(m.P95)}%;"></div>
                            <div class="bullet-bar bar-mean" style="width: ${calcWidth(m.Mean)}%;"></div>
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

        // ---------- render ----------
        function renderDashboard() {
            const container = document.getElementById('instances-list');
            container.innerHTML = '';

            const projects = Object.keys(dashboardData).sort();
            if (projects.length === 0) {
                container.innerHTML = `<div class="empty-state">No reports found for the selected date range.</div>`;
                return;
            }

            projects.forEach(projKey => {
                const instanceDict = dashboardData[projKey];

                Object.keys(instanceDict).sort().forEach(instKey => {
                    const inst = instanceDict[instKey];
                    const card = document.createElement('div');
                    card.className = 'instance-card';

                    const res = inst.resource_utilization || {};
                    const safeInstId = safeId(inst.project_id + "__" + inst.instance_name);

                    let pctRowsHtml = '';
                    let kpiTilesHtml = '';

                    Object.keys(res).forEach(metricKey => {
                        const m = res[metricKey];
                        const label = m["header-name"] || metricKey;
                        const isPct = label.includes("(%)");

                        if (isPct) {
                            const snapMax = getSnapCeiling(m.Max);
                            const calcWidth = (val) => Math.min(100, Math.max(0, (val / snapMax) * 100));

                            pctRowsHtml += `
                                <div class="bullet-row">
                                    <div class="bullet-header">
                                        <div class="bullet-title">${label}</div>
                                        <div class="bullet-badges">
                                            <span class="badge-pill mean">Mean: ${formatNumber(m.Mean)}%</span>
                                            <span class="badge-pill p95">P95: ${formatNumber(m.P95)}%</span>
                                            <span class="badge-pill p99">P99: ${formatNumber(m.P99)}%</span>
                                            <span class="badge-pill max">Max: ${formatNumber(m.Max)}%</span>
                                        </div>
                                    </div>
                                    <div class="track-wrapper">
                                        <div class="gridlines">
                                            <div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div><div class="gridline"></div>
                                        </div>
                                        <div class="bullet-track">
                                            <div class="bullet-bar bar-max" style="width: ${calcWidth(m.Max)}%;"></div>
                                            <div class="bullet-bar bar-p99" style="width: ${calcWidth(m.P99)}%;"></div>
                                            <div class="bullet-bar bar-p95" style="width: ${calcWidth(m.P95)}%;"></div>
                                            <div class="bullet-bar bar-mean" style="width: ${calcWidth(m.Mean)}%;"></div>
                                        </div>
                                    </div>
                                    <div class="ticks-row">
                                        <span>0%</span>
                                        <span>${Math.round(snapMax * 0.25)}%</span>
                                        <span>${Math.round(snapMax * 0.50)}%</span>
                                        <span>${Math.round(snapMax * 0.75)}%</span>
                                        <span>${snapMax}%</span>
                                    </div>
                                </div>
                            `;
                        } else {
                            let maxAllowedStr = "N/A";
                            if (label.includes("Connections") && inst.max_allowed_connections) {
                                maxAllowedStr = formatNumber(inst.max_allowed_connections);
                            }

                            kpiTilesHtml += `
                                <div id="tile-${safeInstId}-${safeId(metricKey)}"
                                     class="kpi-tile tile-${safeInstId}"
                                     onclick="handleTileClick('${safeInstId}', '${metricKey}')">
                                    <div class="kpi-tile-header truncate" title="${label}">
                                        <span>${label}</span>
                                    </div>
                                    <div class="main-val-container">
                                        <div class="kpi-tile-main-val">${formatNumber(m.Mean)}</div>
                                        <div class="max-allowed-badge">Max Allowed: ${maxAllowedStr}</div>
                                    </div>
                                    <div class="kpi-tile-footer">
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--color-mean);">Mean</span>
                                            <span class="kpi-footer-val">${formatNumber(m.Mean)}</span>
                                        </div>
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--color-p95);">P95</span>
                                            <span class="kpi-footer-val">${formatNumber(m.P95)}</span>
                                        </div>
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--color-p99);">P99</span>
                                            <span class="kpi-footer-val">${formatNumber(m.P99)}</span>
                                        </div>
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--color-max);">Max</span>
                                            <span class="kpi-footer-val">${formatNumber(m.Max)}</span>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }
                    });

                    const pctSection = pctRowsHtml ? `
                        <div>
                            <div class="section-title">Percentage Utilization Metrics</div>
                            <div class="bullet-group">${pctRowsHtml}</div>
                        </div>` : '';

                    const countSection = kpiTilesHtml ? `
                        <div>
                            <div class="section-title">Count & Throughput Metrics</div>
                            <div class="kpi-tile-grid">${kpiTilesHtml}</div>
                            <div id="shared-bar-${safeInstId}" class="shared-bar-container"></div>
                        </div>` : '';

                    card.innerHTML = `
                        <div class="instance-header">
                            <div class="breadcrumb">
                                <span class="breadcrumb-project">${inst.project_id}</span>
                                <span class="breadcrumb-sep">/</span>
                                <span class="breadcrumb-instance">${inst.instance_name}</span>
                            </div>
                            <span class="days-badge">${inst.days_counted} Day(s) Aggregated</span>
                        </div>
                        ${pctSection}
                        ${countSection}
                    `;
                    container.appendChild(card);
                });
            });
        }

        // ---------- date range controls ----------
        const fromInput = document.getElementById('date-from');
        const toInput = document.getElementById('date-to');
        const presetBtns = document.querySelectorAll('.preset-btn');

        function applyRange(fromIso, toIso, activePreset) {
            if (fromIso > toIso) [fromIso, toIso] = [toIso, fromIso];  // tolerate reversed picks
            fromInput.value = fromIso;
            toInput.value = toIso;
            presetBtns.forEach(b => b.classList.toggle('active', b.dataset.days === activePreset));

            dashboardData = aggregate(fromIso, toIso);
            const reportDays = availableDates.filter(d => d >= fromIso && d <= toIso).length;
            document.getElementById('range-summary').innerHTML =
                `Showing <strong>${prettyDate(fromIso)}</strong> – <strong>${prettyDate(toIso)}</strong> · ${reportDays} report day(s)`;
            renderDashboard();
        }

        // "Last N days" counts back from the most recent report available
        function applyPreset(days) {
            if (days === 'all') return applyRange(minDate, maxDate, 'all');
            const from = shiftDate(maxDate, -(Number(days) - 1));
            applyRange(from < minDate ? minDate : from, maxDate, String(days));
        }

        if (!minDate) {
            document.getElementById('instances-list').innerHTML =
                `<div class="empty-state">No report data was embedded in this dashboard.</div>`;
            document.querySelector('.filter-bar').style.display = 'none';
        } else {
            [fromInput, toInput].forEach(inp => { inp.min = minDate; inp.max = maxDate; });
            fromInput.addEventListener('change', () => fromInput.value && toInput.value && applyRange(fromInput.value, toInput.value, null));
            toInput.addEventListener('change', () => fromInput.value && toInput.value && applyRange(fromInput.value, toInput.value, null));
            presetBtns.forEach(b => b.addEventListener('click', () => applyPreset(b.dataset.days)));
            applyPreset(7);
        }
    </script>
</body>
</html>"""


def generate_trend_html(daily_records, output_filepath="trend_analysis.html"):
    # "</" escaped so report text can never close the <script> tag early
    data_json = json.dumps(daily_records, indent=2).replace("</", "<\\/")
    with open(output_filepath, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__DASHBOARD_DATA__", data_json))
    print(f"✅ Dashboard generated successfully: {output_filepath}")

# ==========================================
# 3. SCRIPT ENTRYPOINT
# ==========================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Database trend dashboard with a selectable date range")
    parser.add_argument("--bucket", default="db-health-report", help="Name of GCS bucket")
    parser.add_argument("--local-dir", help="Read reports from a local folder instead of GCS (for testing)")
    parser.add_argument("--history-days", type=int, default=90,
                        help="How many days of reports to embed; the dashboard's date picker can choose any range inside this")
    parser.add_argument("--out", default="trend_analysis.html", help="Output HTML file path")
    args = parser.parse_args()

    records = collect_daily_records(bucket_name=args.bucket, local_dir=args.local_dir,
                                    history_days=args.history_days)

    if not records:
        print(f"❌ No valid HTML report data found for the past {args.history_days} days.")
    else:
        generate_trend_html(records, args.out)
