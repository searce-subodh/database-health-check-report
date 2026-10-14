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
    """
    Extracts the report date (and run time, if present) from the file name. Handles both:
      DD-MM-YYYY_Database_Health_Report.html               (older reports)
      Database_Health_Report_DD-MM-YYYY_HH-MM-SS.html      (current reports)
    """
    match = re.search(r'(\d{2}-\d{2}-\d{4})(?:_(\d{2})-(\d{2})-(\d{2}))?', filename)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%d-%m-%Y")
            if match.group(2):
                dt = dt.replace(hour=int(match.group(2)), minute=int(match.group(3)), second=int(match.group(4)))
            return dt
        except ValueError:
            return None
    return None


def _sources_from_folder(folder):
    sources = []
    for path in glob.glob(os.path.join(folder, "**", "*.html"), recursive=True):
        fallback = datetime.fromtimestamp(os.path.getmtime(path))
        sources.append((path, fallback, lambda p=path: open(p, encoding="utf-8").read()))
    return sources


def _sources_via_gcloud(bucket_name, prefix):
    """
    Fallback when the Python storage library is refused (e.g. 403 serviceusage.services.use):
    the gcloud CLI uses your own login, so copy the reports to a temp folder with it.
    """
    import shutil, subprocess, tempfile
    if not shutil.which("gcloud"):
        print("❌ gcloud CLI not found, so there is no fallback. Use --local-dir instead.")
        sys.exit(1)
    tmp = tempfile.mkdtemp(prefix="trend_reports_")
    src = f"gs://{bucket_name}/{prefix}**.html" if prefix else f"gs://{bucket_name}/**.html"
    print(f"↪️  Falling back to gcloud: copying {src} ...")
    res = subprocess.run(["gcloud", "storage", "cp", src, tmp], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"❌ gcloud could not read the bucket either:\n{res.stderr.strip()}")
        sys.exit(1)
    return _sources_from_folder(tmp)


def list_report_sources(bucket_name=None, local_dir=None, prefix=""):
    """
    Returns a list of (name, fallback_date, read_fn) tuples.
    Bucket mode: every *.html under gs://bucket_name/<prefix>  (prefix = a folder, e.g. "Updated_databasehealth_report_samples/")
    Local mode:  every *.html under --local-dir (handy for testing without GCS)
    """
    if local_dir:
        print(f"📂 Reading reports from local folder: {local_dir}")
        return _sources_from_folder(local_dir)

    if prefix and not prefix.endswith("/"):
        prefix += "/"
    where = f"gs://{bucket_name}/{prefix}"
    print(f"📡 Reading reports from {where} ...")
    try:
        from google.cloud import storage  # imported lazily so local mode works without the SDK
        bucket = storage.Client().bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=prefix or None))
    except Exception as e:
        print(f"⚠️  Python storage client could not read {where}: {str(e).splitlines()[0][:200]}")
        return _sources_via_gcloud(bucket_name, prefix)

    sources = []
    for blob in blobs:
        if not blob.name.endswith(".html"):
            continue
        fallback = blob.updated.replace(tzinfo=None) if blob.updated else datetime.now()
        sources.append((blob.name, fallback, lambda b=blob: b.download_as_text()))
    return sources


def to_number(text):
    """'Mean: 1,417.91%' -> 1417.91 ; raises ValueError when there is no number (e.g. 'N/A')."""
    m = re.search(r'-?\d[\d,]*(?:\.\d+)?|-?\.\d+', text)
    if not m:
        raise ValueError(f"no number in {text!r}")
    return float(m.group().replace(",", ""))


# Spec labels in the daily report (case-insensitive) -> name shown in the trend report
SPEC_KEYS = {"cpu": "CPU", "vcpu": "CPU", "vcpus": "CPU", "memory": "Memory", "storage": "Storage"}


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

        metrics = {}         # {metric: [mean, p95, p99, max]}
        max_allowed = {}     # {metric: limit shown on the report tile}

        # Older reports: one "metrics-table" with Mean / P95 / P99 / Max columns
        metrics_table = block.find("table", class_="metrics-table")
        if metrics_table:
            for row in metrics_table.find_all("tr")[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) >= 5:
                    try:
                        metrics[cols[0].text.strip()] = [to_number(c.text) for c in cols[1:5]]
                    except ValueError:
                        continue

        # Current reports: percentage rows with Mean/P95/P99/Max badges ...
        for row in block.find_all("div", class_="bullet-row"):
            title = row.find(class_="bullet-title")
            badges = row.find(class_="bullet-badges")
            if not title or not badges:
                continue                      # chart-only rows (no badges) carry no summary values
            vals = {}
            for pill in badges.find_all(class_="badge-pill"):
                kind = next((c for c in pill.get("class", []) if c in ("mean", "p95", "p99", "max")), None)
                if kind:
                    try:
                        vals[kind] = to_number(pill.get_text().split(":", 1)[-1])   # "P95: 10.94%" -> 10.94
                    except ValueError:
                        pass
            if len(vals) == 4:
                metrics[title.get_text(strip=True)] = [vals["mean"], vals["p95"], vals["p99"], vals["max"]]

        # ... and count / throughput tiles (Mean as the big number, P95 / P99 / Max in the footer)
        for tile in block.find_all("div", class_="kpi-tile"):
            head = tile.find(class_="kpi-tile-header")
            main = tile.find(class_="kpi-tile-main-val")
            if not head or not main:
                continue
            name = head.get_text(strip=True)
            try:
                vals = {"mean": to_number(main.get_text())}
                for item in tile.find_all(class_="kpi-footer-item"):
                    lbl = item.find(class_="kpi-footer-label")
                    val = item.find(class_="kpi-footer-val")
                    if lbl and val:
                        vals[lbl.get_text(strip=True).lower()] = to_number(val.get_text())
                if all(k in vals for k in ("mean", "p95", "p99", "max")):
                    metrics[name] = [vals["mean"], vals["p95"], vals["p99"], vals["max"]]
            except ValueError:
                continue
            allowed = tile.find(class_="max-allowed-badge")
            if allowed:
                try:
                    max_allowed[name] = to_number(allowed.get_text().split(":", 1)[-1])
                except ValueError:
                    pass                      # "Max Allowed: N/A"

        # Provisioned Specifications cards -> keep only CPU, Memory, Storage
        specs = {}
        for item in block.find_all("div", class_="spec-item"):
            lbl = item.find(class_="spec-label")
            val = item.find(class_="spec-value")
            if not lbl or not val:
                continue
            key = SPEC_KEYS.get(lbl.get_text(strip=True).lower())
            if key:
                specs[key] = val.get_text(" ", strip=True)

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
            "s": specs,                               # {"CPU": "1", "Memory": "3.75 GB", "Storage": "10 GB SSD"}
            "ma": max_allowed,                        # {metric: limit}, e.g. IOPS limit, disk size
        })
    return records


def collect_daily_records(bucket_name=None, local_dir=None, history_days=90, prefix=""):
    """
    Loads every daily report from the last `history_days` days and returns the
    raw per-day records. Aggregation now happens in the browser, so the
    dashboard can be re-aggregated for any From/To range the user picks.
    """
    cutoff = (datetime.now() - timedelta(days=history_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_files = []

    for name, fallback_date, read_fn in list_report_sources(bucket_name, local_dir, prefix):
        base = os.path.basename(name)
        if not base.endswith(".html") or "trend_analysis" in base:
            continue
        file_date = parse_date_from_filename(base) or fallback_date
        if file_date >= cutoff:
            daily_files.append((file_date, name, read_fn))

    daily_files.sort(key=lambda x: x[0])
    print(f"📄 Found {len(daily_files)} daily HTML report(s) within the last {history_days} days.")

    # If a day has more than one run, the latest run wins (files are processed oldest -> newest)
    latest = {}
    for file_date, name, read_fn in daily_files:
        print(f"  └─ Parsing HTML Report: {name}...")
        try:
            for rec in parse_report(read_fn(), file_date):
                key = (rec["d"], rec["p"], rec["i"])
                if key in latest:
                    print(f"     ↺ replacing earlier run of {rec['p']}/{rec['i']} on {rec['d']}")
                latest[key] = rec
        except Exception as e:
            print(f"⚠️ Failed to parse HTML report '{name}': {e}")
    return sorted(latest.values(), key=lambda r: (r["d"], r["p"], r["i"]))

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

            /* Series colours (same as the daily health report charts) */
            --color-mean: #0d6efd;
            --color-p95:  #198754;
            --color-p99:  #495057;
            --color-max:  #dc3545;
            /* Darker shades of the same hues for text, so labels stay readable */
            --ink-mean: #0a58ca;
            --ink-p95:  #146c43;
            --ink-p99:  #343a40;
            --ink-max:  #b02a37;

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
        .filter-bar { align-items: center; }
        .filter-divider { width: 1px; align-self: stretch; background: var(--border-color); }

        /* Instance dropdown, styled like the preset tiles */
        .filter-select {
            font-family: inherit; font-size: 12px; font-weight: 600;
            color: var(--text-main);
            padding: 7px 30px 7px 12px; border-radius: 8px; cursor: pointer;
            border: 1px solid var(--border-color); background-color: var(--bg-subcard);
            appearance: none; -webkit-appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M0 0h10L5 6z' fill='%230f172a'/%3E%3C/svg%3E");
            background-repeat: no-repeat; background-position: right 11px center; background-size: 9px 6px;
            max-width: 260px;
        }
        .filter-select:hover { border-color: var(--color-mean); }
        .filter-select:focus-visible { outline: 2px solid rgba(13, 110, 253,0.35); border-color: var(--color-mean); }
        .filter-select.active { background-color: rgba(13, 110, 253,0.1); border-color: rgba(13, 110, 253,0.4); color: var(--color-mean); }

        /* Project / instance dropdown */
        .scope-wrap { position: relative; min-width: 0; }
        #scope-btn { text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 280px; }
        .scope-menu {
            position: absolute; top: calc(100% + 6px); left: 0; z-index: 20;
            background: var(--bg-card); border: 1px solid var(--border-color);
            border-radius: 12px; padding: 8px; width: 300px; max-width: calc(100vw - 32px);
            box-shadow: 0 12px 28px -6px rgba(15, 23, 42, 0.18);
            display: flex; flex-direction: column; gap: 6px; box-sizing: border-box;
        }
        .scope-menu[hidden] { display: none; }
        .scope-search {
            font-family: inherit; font-size: 12px; color: var(--text-main);
            padding: 7px 10px; border: 1px solid var(--border-color); border-radius: 8px;
            background: var(--bg-subcard); width: 100%; box-sizing: border-box;
        }
        .scope-search:focus { outline: 2px solid rgba(13, 110, 253,0.35); border-color: var(--color-mean); }
        .scope-list { max-height: 340px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
        .scope-item {
            font-family: inherit; font-size: 12px; font-weight: 500; color: var(--text-main);
            display: flex; align-items: center; gap: 8px; width: 100%; text-align: left;
            padding: 7px 10px; border: 0; border-radius: 6px; background: transparent; cursor: pointer;
        }
        .scope-item:hover, .scope-item:focus-visible { background: var(--bg-subcard); outline: none; }
        .scope-item .scope-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .scope-item.selected { color: var(--color-mean); font-weight: 600; }
        .scope-item .scope-check { width: 14px; height: 14px; flex-shrink: 0; visibility: hidden; }
        .scope-item.selected .scope-check { visibility: visible; }
        .scope-project { font-weight: 600; }
        .scope-project .scope-chev { width: 12px; height: 12px; flex-shrink: 0; color: var(--text-muted); transition: transform 0.15s ease; }
        .scope-project[aria-expanded="true"] .scope-chev { transform: rotate(90deg); }
        .scope-count {
            font-size: 10px; font-weight: 600; color: var(--text-muted);
            background: var(--bg-subcard); border: 1px solid var(--border-color);
            border-radius: 10px; padding: 0 7px;
        }
        .scope-children { margin: 0 0 4px 16px; padding-left: 8px; border-left: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 1px; }
        .scope-children .scope-all-in { color: var(--text-muted); }
        .scope-children .scope-all-in.selected { color: var(--color-mean); }
        .scope-sep { height: 1px; background: var(--border-color); margin: 4px 2px; }
        .scope-empty { font-size: 12px; color: var(--text-muted); padding: 10px; text-align: center; }

        /* Custom range dropdown */
        .custom-wrap { position: relative; }
        .custom-btn { display: inline-flex; align-items: center; gap: 8px; }
        .custom-caret { width: 9px; height: 6px; transition: transform 0.15s ease; }
        .custom-btn[aria-expanded="true"] .custom-caret { transform: rotate(180deg); }
        .custom-popover {
            position: absolute; top: calc(100% + 6px); left: 0; z-index: 20;
            background: var(--bg-card); border: 1px solid var(--border-color);
            border-radius: 12px; padding: 14px; width: 280px; max-width: calc(100vw - 32px);
            box-shadow: 0 12px 28px -6px rgba(15, 23, 42, 0.18);
            display: flex; flex-direction: column; gap: 12px;
        }
        .custom-popover[hidden] { display: none; }
        .popover-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .filter-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
        .filter-field label {
            font-size: 10px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.06em; color: var(--text-muted);
        }
        .filter-field input[type="date"] {
            font-family: inherit; font-size: 13px; color: var(--text-main);
            padding: 7px 8px; border: 1px solid var(--border-color);
            border-radius: 8px; background: var(--bg-subcard); min-width: 0; width: 100%; box-sizing: border-box;
        }
        .filter-field input[type="date"]:focus { outline: 2px solid rgba(13, 110, 253,0.35); border-color: var(--color-mean); }
        .popover-error { font-size: 11px; font-weight: 600; color: #dc2626; min-height: 0; }
        .popover-error:empty { display: none; }
        .popover-actions { display: flex; justify-content: flex-end; gap: 8px; }
        .btn-secondary, .btn-primary {
            font-family: inherit; font-size: 12px; font-weight: 600;
            padding: 7px 14px; border-radius: 8px; cursor: pointer;
        }
        .btn-secondary { background: transparent; border: 1px solid var(--border-color); color: var(--text-muted); }
        .btn-secondary:hover { color: var(--text-main); }
        .btn-primary { background: var(--color-mean); border: 1px solid var(--color-mean); color: #fff; }
        .btn-primary:hover { filter: brightness(1.08); }

        .preset-group { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
        .preset-btn {
            font-family: inherit; font-size: 12px; font-weight: 600;
            padding: 7px 12px; border-radius: 8px; cursor: pointer;
            border: 1px solid var(--border-color); background: var(--bg-subcard);
            color: var(--text-muted);
        }
        .preset-btn:hover { border-color: var(--color-mean); color: var(--color-mean); }
        .preset-btn.active { background: rgba(13, 110, 253,0.1); border-color: rgba(13, 110, 253,0.4); color: var(--color-mean); }
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
            background: rgba(13, 110, 253, 0.08);
            color: var(--color-mean);
            font-weight: 600;
            padding: 5px 14px;
            border-radius: 20px;
            border: 1px solid rgba(13, 110, 253, 0.2);
        }

        /* Provisioned specifications (same look as the daily health report) */
        .spec-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
        .spec-card {
            background: var(--bg-subcard); border-radius: 8px;
            border-left: 4px solid #2563eb; padding: 12px 16px;
            display: flex; flex-direction: column; gap: 4px; min-width: 0;
        }
        .spec-card .spec-label {
            font-size: 11px; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.04em; color: var(--text-muted);
        }
        .spec-card .spec-value { font-size: 16px; font-weight: 700; color: var(--text-main); overflow-wrap: anywhere; }

        .section-title {
            font-size: 13px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            margin-bottom: 16px;
        }

        /* ---------- Collapsible sections ---------- */
        .metric-section > summary {
            list-style: none;
            cursor: pointer;
            user-select: none;
            display: flex;
            align-items: center;
            gap: 8px;
            border-radius: 6px;
        }
        .metric-section > summary::-webkit-details-marker { display: none; }
        .metric-section > summary:hover { color: var(--text-main); }
        .metric-section > summary:focus-visible { outline: 2px solid rgba(13, 110, 253,0.35); outline-offset: 2px; }
        .section-chevron {
            width: 14px; height: 14px; flex-shrink: 0;
            transition: transform 0.2s ease;
        }
        .metric-section[open] > summary .section-chevron { transform: rotate(90deg); }
        .metric-section:not([open]) > summary { margin-bottom: 0; }
        .section-count {
            font-size: 11px; font-weight: 600; letter-spacing: 0;
            text-transform: none; color: var(--text-muted);
            background: var(--bg-subcard); border: 1px solid var(--border-color);
            border-radius: 10px; padding: 1px 8px;
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
        .bullet-title-group { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

        /* Bar / Line toggle */
        .view-toggle {
            display: inline-flex; padding: 2px; gap: 2px;
            background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 8px;
        }
        .view-toggle button {
            font-family: inherit; font-size: 11px; font-weight: 600;
            display: inline-flex; align-items: center; gap: 5px;
            padding: 3px 9px; border: 0; border-radius: 6px; cursor: pointer;
            background: transparent; color: var(--text-muted);
        }
        .view-toggle button svg { width: 12px; height: 12px; }
        .view-toggle button:hover { color: var(--text-main); }
        .view-toggle button[aria-pressed="true"] { background: rgba(13, 110, 253,0.1); color: var(--color-mean); }
        .view-toggle button:focus-visible { outline: 2px solid rgba(13, 110, 253,0.35); }

        /* Line chart */
        .line-chart { position: relative; width: 100%; height: 210px; outline: none; }
        .line-chart:focus-visible { box-shadow: 0 0 0 2px rgba(13, 110, 253,0.35); border-radius: 6px; }
        .line-chart svg { display: block; overflow: visible; }
        .line-chart .axis-label { font-size: 10px; fill: var(--text-muted); font-family: inherit; }
        .line-chart .grid { stroke: rgba(0,0,0,0.07); stroke-width: 1; }
        .line-chart .baseline { stroke: rgba(0,0,0,0.18); stroke-width: 1; }
        .line-chart .series { fill: none; stroke-width: 1.5; stroke-linejoin: round; stroke-linecap: round; }
        .line-chart .band { opacity: 0.15; stroke: none; }
        .line-chart .seg { cursor: pointer; }
        .line-chart .seg:hover { filter: brightness(1.08); }
        .line-chart .seg-cap { stroke-width: 2; visibility: hidden; pointer-events: none; }
        .line-chart .hover-band { fill: rgba(15, 23, 42, 0.05); }
        .line-chart .crosshair { stroke: var(--text-muted); stroke-width: 1; opacity: 0.6; }
        .line-chart .hover-dot { stroke: var(--bg-subcard); stroke-width: 2; }
        .chart-tooltip {
            position: absolute; top: 0; pointer-events: none; z-index: 5;
            background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 8px;
            box-shadow: 0 8px 20px -6px rgba(15,23,42,0.2);
            padding: 8px 10px; font-size: 11px; min-width: 130px;
        }
        .chart-tooltip[hidden] { display: none; }
        .chart-tooltip .tt-date { font-weight: 600; color: var(--text-muted); margin-bottom: 4px; }
        .chart-tooltip .tt-row { display: flex; align-items: center; gap: 6px; padding: 1px 0; }
        .chart-tooltip .tt-row svg { flex-shrink: 0; }
        .chart-tooltip .tt-val { font-weight: 700; color: var(--text-main); }
        .chart-tooltip .tt-name { color: var(--text-muted); }
        .chart-tooltip .tt-empty { color: var(--text-muted); font-style: italic; }
        .chart-legend { display: flex; gap: 6px; flex-wrap: wrap; }

        /* Global Bar / Line switch in the filter bar */
        .view-toggle-global button { font-size: 12px; padding: 5px 11px; }

        /* Clickable series pickers (badges + legend) in line view */
        .series-pick { cursor: pointer; font-family: inherit; transition: opacity 0.15s ease, box-shadow 0.15s ease; }
        .series-pick:focus-visible { outline: 2px solid rgba(13, 110, 253,0.35); outline-offset: 1px; }
        .legend-item {
            display: inline-flex; align-items: center; gap: 6px;
            font-size: 11px; font-weight: 600; color: var(--text-muted);
            background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 3px 8px;
        }
        .legend-item:hover { background: var(--bg-card); border-color: var(--border-color); color: var(--text-main); }
        .legend-item.is-highlight { background: var(--bg-card); border-color: var(--border-color); color: var(--text-main); }

        /* Highlight one series, fade the rest */
        .line-chart [data-series] { transition: opacity 0.15s ease, stroke-width 0.15s ease; }
        .bullet-row.has-highlight .line-chart [data-series]:not(.is-highlight) { opacity: 0.15; }
        .bullet-row.has-highlight .line-chart path.band:not(.is-highlight) { opacity: 0.04; }
        .bullet-row.has-highlight .line-chart path.band.is-highlight { opacity: 0.32; }
        .bullet-row.has-highlight .line-chart path.series.is-highlight { stroke-width: 2.5; }
        .bullet-row.has-highlight .line-chart .seg-cap.is-highlight { visibility: visible; }
        .bullet-row.has-highlight .line-chart .tt-row[data-series]:not(.is-highlight) { opacity: 0.45; }
        .bullet-row.has-highlight .series-pick:not(.is-highlight) { opacity: 0.4; }
        .bullet-row.has-highlight .badge-pill.is-highlight { box-shadow: 0 0 0 1.5px currentColor; }

        .bullet-badges { display: flex; gap: 8px; flex-wrap: wrap; }

        .badge-pill {
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid rgba(0, 0, 0, 0.08);
        }

        .badge-pill.mean { color: var(--ink-mean); border-color: rgba(13, 110, 253, 0.35); background: rgba(13, 110, 253, 0.08); }
        .badge-pill.p95  { color: var(--ink-p95);  border-color: rgba(25, 135, 84, 0.40); background: rgba(25, 135, 84, 0.08); }
        .badge-pill.p99  { color: var(--ink-p99);  border-color: rgba(73, 80, 87, 0.45); background: rgba(73, 80, 87, 0.10); }
        .badge-pill.max  { color: var(--ink-max);  border-color: rgba(220, 53, 69, 0.40); background: rgba(220, 53, 69, 0.08); }

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
            background: rgba(13, 110, 253, 0.04);
        }

        .kpi-tile-header {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-main);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .kpi-tile-header-group { display: flex; flex-direction: column; gap: 4px; }

        /* "Mean" caption under the tile title; same size/weight as "Max Allowed" */
        .kpi-mean-label {
            font-size: 11px;
            font-weight: 600;
            color: var(--ink-mean);
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

        /* P95 / P99 / Max (Mean is shown as the main value above) */
        .kpi-tile-footer {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
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
            .filter-divider { display: none; }
            .spec-grid { gap: 8px; }
            .spec-card { padding: 10px 10px 10px 12px; }
            .spec-card .spec-value { font-size: 14px; }
            .filter-select, #scope-btn { max-width: 100%; }
        }
    </style>
</head>
<body>

    <div class="container">
        <div class="global-header">
            <h1 class="global-title">Aggregated Database Health Analysis</h1>

            <div class="filter-bar">
                <div class="scope-wrap">
                    <button type="button" class="filter-select" id="scope-btn" aria-haspopup="true" aria-expanded="false" aria-label="Project and instance filter">All instances</button>
                    <div class="scope-menu" id="scope-menu" hidden>
                        <input type="search" class="scope-search" id="scope-search" placeholder="Search projects or instances" aria-label="Search projects or instances">
                        <div class="scope-list" id="scope-list" role="menu"></div>
                    </div>
                </div>
                <div class="filter-divider"></div>
                <div class="preset-group">
                    <button class="preset-btn" data-days="week">This week</button>
                    <button class="preset-btn" data-days="month">This month</button>
                    <div class="custom-wrap">
                        <button class="preset-btn custom-btn" id="custom-btn" data-days="custom" aria-haspopup="dialog" aria-expanded="false">
                            Custom
                            <svg class="custom-caret" viewBox="0 0 10 6" aria-hidden="true"><path d="M0 0h10L5 6z" fill="currentColor"/></svg>
                        </button>
                        <div class="custom-popover" id="custom-popover" role="dialog" aria-label="Custom date range" hidden>
                            <div class="popover-fields">
                                <div class="filter-field">
                                    <label for="date-from">From</label>
                                    <input type="date" id="date-from">
                                </div>
                                <div class="filter-field">
                                    <label for="date-to">To</label>
                                    <input type="date" id="date-to">
                                </div>
                            </div>
                            <div class="popover-error" id="custom-error"></div>
                            <div class="popover-actions">
                                <button class="btn-secondary" id="custom-cancel" type="button">Cancel</button>
                                <button class="btn-primary" id="custom-apply" type="button">Apply</button>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="filter-divider"></div>
                <div class="view-toggle view-toggle-global" id="view-toggle" role="group" aria-label="Chart type for all parameters">
                    <button type="button" data-view="bar" aria-pressed="false"><svg viewBox="0 0 12 12" aria-hidden="true"><rect x="1" y="2" width="10" height="3" rx="1" fill="currentColor"/><rect x="1" y="7" width="6" height="3" rx="1" fill="currentColor"/></svg>Bar</button>
                    <button type="button" data-view="line" aria-pressed="true"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1 10l3.5-4 3 2.5L11 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>Line</button>
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

        // ---------- instance filter ----------
        // One entry per project+instance (instance names can repeat across projects)
        const instanceList = [...new Map(dailyRecords.map(r => [r.p + "\u0000" + r.i, { p: r.p, i: r.i }])).values()]
            .sort((a, b) => a.p.localeCompare(b.p) || a.i.localeCompare(b.i));
        const projectList = [...new Set(instanceList.map(x => x.p))];
        // { type: 'all' } | { type: 'project', p } | { type: 'instance', p, i }
        let selection = { type: 'all' };

        function recordMatchesInstance(r) {
            if (selection.type === 'all') return true;
            if (selection.type === 'project') return r.p === selection.p;
            return r.p === selection.p && r.i === selection.i;
        }

        function selectionLabel() {
            if (selection.type === 'all') return 'All instances';
            if (selection.type === 'project') return `${selection.p} · all instances`;
            return `${selection.p} / ${selection.i}`;
        }

        const escapeHtml = v => String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

        function filteredRecords(fromIso, toIso) {
            return dailyRecords.filter(r => r.d >= fromIso && r.d <= toIso && recordMatchesInstance(r));
        }

        // ---------- aggregation for a date range (same rules as before) ----------
        // Mean/P95/P99 = average of the daily values; Max = highest daily max.
        function aggregate(fromIso, toIso) {
            const acc = {};
            filteredRecords(fromIso, toIso)
                .sort((a, b) => a.d.localeCompare(b.d))
                .forEach(r => {
                    const key = r.p + "\u0000" + r.i;
                    if (!acc[key]) acc[key] = { project_id: r.p, instance_name: r.i, dates: new Set(), maxConn: null, metrics: {}, specs: {}, maxAllowed: {} };
                    const a = acc[key];
                    a.dates.add(r.d);
                    if (r.mc !== null && r.mc !== undefined) a.maxConn = r.mc;  // latest in range wins
                    // specs: records arrive oldest -> newest, so the latest report in range wins
                    Object.entries(r.s || {}).forEach(([k, v]) => { a.specs[k] = { value: v }; });
                    Object.entries(r.ma || {}).forEach(([k, v]) => { a.maxAllowed[k] = v; });   // latest in range wins
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
                    max_allowed: a.maxAllowed,
                    specs: a.specs,
                    resource_utilization: util
                };
            });
            return out;
        }

        // ---------- metric rows: Line (shaded bands) / Bar (stacked) views ----------
        // Colours as agreed; solid 1.5px lines like the daily health report charts.
        const SERIES = [
            { k: 'Mean', idx: 0, color: '#0d6efd', dash: '' },
            { k: 'P95',  idx: 1, color: '#198754', dash: '' },
            { k: 'P99',  idx: 2, color: '#495057', dash: '' },
            { k: 'Max',  idx: 3, color: '#dc3545', dash: '' },
        ];
        let chartView = 'line';           // one switch for the whole report: 'line' (default) | 'bar'
        const highlights = new Map();     // rowKey -> 'Mean' | 'P95' | 'P99' | 'Max'

        const lineKeySvg = (s) => `<svg width="18" height="6" aria-hidden="true"><line x1="1" y1="3" x2="17" y2="3" stroke="${s.color}" stroke-width="2" stroke-linecap="round" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''}/></svg>`;
        const boxKeySvg  = (s) => `<svg width="12" height="12" aria-hidden="true"><rect x="0.5" y="0.5" width="11" height="11" rx="2" fill="${s.color}"/></svg>`;

        function metricRowHtml(inst, metricKey) {
            const m = inst.resource_utilization[metricKey];
            const label = m["header-name"] || metricKey;
            const isPct = label.includes("(%)");
            const unit = isPct ? '%' : '';
            const instId = safeId(inst.project_id + "__" + inst.instance_name);
            const rowKey = instId + "::" + metricKey;
            const key = chartView === 'bar' ? boxKeySvg : lineKeySvg;   // legend mirrors the mark
            const badge = (k, cls, val) =>
                `<button type="button" class="badge-pill ${cls} series-pick" data-series="${k}" aria-pressed="false" title="Highlight ${k}">${k}: ${formatNumber(val)}${unit}</button>`;
            return `
                <div class="bullet-row" data-row-key="${encodeURIComponent(rowKey)}" data-inst="${instId}" data-metric="${encodeURIComponent(metricKey)}">
                    <div class="bullet-header">
                        <div class="bullet-title">${label}</div>
                        <div class="bullet-badges">
                            ${badge('Mean', 'mean', m.Mean)}
                            ${badge('P95', 'p95', m.P95)}
                            ${badge('P99', 'p99', m.P99)}
                            ${badge('Max', 'max', m.Max)}
                        </div>
                    </div>
                    <div class="line-chart" tabindex="0" aria-label="${label} daily trend. Use left and right arrow keys to step through days."></div>
                    <div class="chart-legend">${SERIES.map(s =>
                        `<button type="button" class="legend-item series-pick" data-series="${s.k}" aria-pressed="false" title="Highlight ${s.k}">${key(s)}${s.k}</button>`).join('')}</div>
                </div>`;
        }

        // Daily [mean, p95, p99, max] for one instance+metric, one slot per calendar day in range (null = no report)
        function dailySeries(inst, metricKey) {
            const byDate = {};
            filteredRecords(currentFrom, currentTo)
                .filter(r => r.p === inst.project_id && r.i === inst.instance_name && r.m[metricKey])
                .forEach(r => { byDate[r.d] = r.m[metricKey]; });
            const days = [];
            for (let d = currentFrom; d <= currentTo; d = shiftDate(d, 1)) days.push({ d, v: byDate[d] || null });
            return days;
        }

        const shortDate = iso => new Date(iso + "T00:00:00").toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
        const SVGNS = 'http://www.w3.org/2000/svg';
        function svgEl(tag, attrs, parent) {
            const el = document.createElementNS(SVGNS, tag);
            Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
            if (parent) parent.appendChild(el);
            return el;
        }

        // Smooth curve through points without overshooting (monotone cubic, Fritsch–Carlson).
        // Returns only the "C ..." segments; the caller supplies the starting M/L.
        function monoCurve(pts) {
            const n = pts.length;
            if (n < 2) return '';
            const dx = [], m = [];
            for (let i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; m[i] = (pts[i + 1][1] - pts[i][1]) / dx[i]; }
            const t = [m[0]];
            for (let i = 1; i < n - 1; i++) t[i] = (m[i - 1] * m[i] <= 0) ? 0 : (m[i - 1] + m[i]) / 2;
            t[n - 1] = m[n - 2];
            for (let i = 0; i < n - 1; i++) {
                if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
                const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b;
                if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
            }
            let d = '';
            for (let i = 0; i < n - 1; i++) {
                const [x0, y0] = pts[i], [x1, y1] = pts[i + 1], h = dx[i] / 3;
                d += `C${(x0 + h).toFixed(1)},${(y0 + t[i] * h).toFixed(1)} ${(x1 - h).toFixed(1)},${(y1 - t[i + 1] * h).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
            }
            return d;
        }

        // Consecutive runs of days that have a report (lines and bands break at missing days)
        function runsOf(days) {
            const runs = []; let cur = [];
            days.forEach((day, i) => { if (day.v) cur.push(i); else if (cur.length) { runs.push(cur); cur = []; } });
            if (cur.length) runs.push(cur);
            return runs;
        }

        function drawChart(row) {
            const host = row.querySelector('.line-chart');
            if (!host) return;
            const inst = findInstance(row.dataset.inst);
            const metricKey = decodeURIComponent(row.dataset.metric);
            if (!inst || !inst.resource_utilization[metricKey]) return;
            const m = inst.resource_utilization[metricKey];
            const isPct = (m["header-name"] || metricKey).includes("(%)");
            const unit = isPct ? '%' : '';
            const scale = isPct ? getSnapCeiling(m.Max) : getDynamicCountScale(m.Max);
            const days = dailySeries(inst, metricKey);
            const rowKey = decodeURIComponent(row.dataset.rowKey);

            host.innerHTML = '';
            const W = Math.max(host.clientWidth, 200), H = host.clientHeight;
            const M = { l: 46, r: 14, t: 10, b: 26 };
            const iw = W - M.l - M.r, ih = H - M.t - M.b;
            const n = days.length;
            const slot = iw / n;
            const x = i => M.l + (i + 0.5) * slot;                 // same day positions in both views
            const y = v => M.t + ih - Math.min(1, Math.max(0, v / scale)) * ih;
            const base = y(0);

            const svg = svgEl('svg', { width: W, height: H, role: 'img' }, host);

            // y grid + labels
            for (let t = 0; t <= 4; t++) {
                const v = scale * t / 4, yy = y(v);
                svgEl('line', { x1: M.l, x2: W - M.r, y1: yy, y2: yy, class: t === 0 ? 'baseline' : 'grid' }, svg);
                const lbl = svgEl('text', { x: M.l - 8, y: yy + 3, 'text-anchor': 'end', class: 'axis-label' }, svg);
                lbl.textContent = isPct ? `${Math.round(v)}%` : formatTickValue(v);
            }

            // x labels: first, last and evenly spaced ones in between (no overlap)
            const maxLabels = Math.max(2, Math.floor(iw / 70));
            const step = Math.max(1, Math.ceil((n - 1) / (maxLabels - 1)));
            const labelIdx = new Set([0, n - 1]);
            for (let i = step; i < n - 1; i += step) if (n - 1 - i >= step * 0.6) labelIdx.add(i);
            labelIdx.forEach(i => {
                const t = svgEl('text', { x: x(i), y: H - 6, 'text-anchor': 'middle', class: 'axis-label' }, svg);
                t.textContent = shortDate(days[i].d);
            });

            // hover band behind the hovered day
            const band = svgEl('rect', { y: M.t, height: ih, width: slot, class: 'hover-band', visibility: 'hidden' }, svg);
            const marks = svgEl('g', {}, svg);

            if (chartView === 'bar') {
                // Stacked bars: each piece runs from the previous value up to its own,
                // so the top of every coloured piece sits at that value (Mean ≤ P95 ≤ P99 ≤ Max).
                const bw = Math.max(3, Math.min(28, slot * 0.62));
                const GAP = n > 1 && bw > 6 ? 1 : 0;                 // 2px surface gap between pieces
                days.forEach((day, i) => {
                    if (!day.v) return;
                    let prev = 0;
                    SERIES.forEach((s, k) => {
                        const v = Math.max(prev, day.v[s.idx]);
                        const y0 = y(prev), y1 = y(v);
                        const hgt = Math.max(0, (y0 - y1) - (k > 0 ? GAP * 2 : GAP));
                        const top = y1 + (k === SERIES.length - 1 ? 0 : GAP);
                        if (hgt > 0) {
                            const isTop = k === SERIES.length - 1 || day.v[SERIES[k + 1].idx] <= v;
                            const r = isTop ? Math.min(4, bw / 2, hgt) : 0;
                            const xl = x(i) - bw / 2, xr = x(i) + bw / 2, yb = top + hgt;
                            const d = r
                                ? `M${xl},${yb}V${top + r}Q${xl},${top} ${xl + r},${top}H${xr - r}Q${xr},${top} ${xr},${top + r}V${yb}Z`
                                : `M${xl},${yb}V${top}H${xr}V${yb}Z`;
                            svgEl('path', { d, fill: s.color, class: 'seg series-pick', 'data-series': s.k }, marks);
                        }
                        // value marker at the top of the piece, shown when this series is highlighted
                        svgEl('line', { x1: x(i) - bw / 2 - 2, x2: x(i) + bw / 2 + 2, y1: y1, y2: y1, stroke: s.color, class: 'seg-cap', 'data-series': s.k }, marks);
                        prev = v;
                    });
                });
            } else {
                // Shaded bands like the health report: Mean fills to the axis, each next series fills down to the one below
                const runs = runsOf(days);
                runs.forEach(run => {
                    const pts = SERIES.map(s => run.map(i => [x(i), y(days[i].v[s.idx])]));
                    SERIES.forEach((s, k) => {
                        const upper = pts[k];
                        const lower = k === 0 ? run.map(i => [x(i), base]) : pts[k - 1];
                        if (run.length > 1) {
                            const back = [...lower].reverse();
                            const dArea = `M${upper[0][0]},${upper[0][1]}${monoCurve(upper)}L${back[0][0]},${back[0][1]}${k === 0 ? `L${back[back.length - 1][0]},${back[back.length - 1][1]}` : monoCurve(back)}Z`;
                            svgEl('path', { d: dArea, fill: s.color, class: 'band', 'data-series': s.k }, marks);
                        }
                    });
                    SERIES.forEach((s, k) => {
                        const p = pts[k];
                        if (run.length > 1) {
                            svgEl('path', { d: `M${p[0][0]},${p[0][1]}${monoCurve(p)}`, class: 'series', 'data-series': s.k, stroke: s.color, ...(s.dash ? { 'stroke-dasharray': s.dash } : {}) }, marks);
                        } else {
                            svgEl('circle', { cx: p[0][0], cy: p[0][1], r: 3, fill: s.color, 'data-series': s.k }, marks);
                        }
                    });
                });
            }

            // hover layer
            const cross = svgEl('line', { y1: M.t, y2: M.t + ih, class: 'crosshair', visibility: 'hidden' }, svg);
            const dots = chartView === 'line'
                ? SERIES.map(s => svgEl('circle', { r: 4, fill: s.color, class: 'hover-dot', 'data-series': s.k, visibility: 'hidden' }, svg))
                : [];
            const hit = svgEl('rect', { x: M.l, y: 0, width: iw, height: H, fill: 'transparent', class: 'hit' }, svg);
            if (chartView === 'bar') svg.appendChild(marks);          // bar pieces stay clickable above the hit area

            const tip = document.createElement('div');
            tip.className = 'chart-tooltip';
            tip.hidden = true;
            host.appendChild(tip);

            let cur = -1;
            function show(i) {
                cur = i;
                const day = days[i], cx = x(i);
                if (chartView === 'line') {
                    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
                } else {
                    band.setAttribute('x', cx - slot / 2); band.setAttribute('visibility', 'visible');
                }
                tip.innerHTML = '';
                const head = document.createElement('div'); head.className = 'tt-date'; head.textContent = prettyDate(day.d); tip.appendChild(head);
                if (!day.v) {
                    const e = document.createElement('div'); e.className = 'tt-empty'; e.textContent = 'No report this day'; tip.appendChild(e);
                    dots.forEach(dt => dt.setAttribute('visibility', 'hidden'));
                } else {
                    const hk = highlights.get(rowKey);
                    // Max first in bar view so the list reads top-to-bottom like the bar
                    (chartView === 'bar' ? [...SERIES].reverse() : SERIES).forEach(s => {
                        const k = SERIES.indexOf(s);
                        if (dots[k]) { dots[k].setAttribute('cx', cx); dots[k].setAttribute('cy', y(day.v[s.idx])); dots[k].setAttribute('visibility', 'visible'); }
                        const r = document.createElement('div'); r.className = 'tt-row'; r.dataset.series = s.k;
                        if (hk === s.k) r.classList.add('is-highlight');
                        r.insertAdjacentHTML('beforeend', chartView === 'bar' ? boxKeySvg(s) : lineKeySvg(s));
                        const val = document.createElement('span'); val.className = 'tt-val'; val.textContent = formatNumber(day.v[s.idx]) + unit;
                        const nm = document.createElement('span'); nm.className = 'tt-name'; nm.textContent = s.k;
                        r.append(val, nm); tip.appendChild(r);
                    });
                }
                tip.hidden = false;
                const tw = tip.offsetWidth;
                let left = cx + 14;
                if (left + tw > W) left = cx - 14 - tw;
                tip.style.left = `${Math.max(0, left)}px`;
                tip.style.top = `${M.t}px`;
            }
            function hide() {
                cur = -1; tip.hidden = true;
                cross.setAttribute('visibility', 'hidden');
                band.setAttribute('visibility', 'hidden');
                dots.forEach(dt => dt.setAttribute('visibility', 'hidden'));
            }
            const nearest = (px) => Math.max(0, Math.min(n - 1, Math.floor((px - M.l) / slot)));
            svg.addEventListener('pointermove', e => {
                const px = e.clientX - svg.getBoundingClientRect().left;
                if (px < M.l || px > W - M.r) return hide();
                show(nearest(px));
            });
            svg.addEventListener('pointerleave', hide);
            host.addEventListener('keydown', e => {
                if (e.key === 'ArrowRight') { show(Math.min(n - 1, cur < 0 ? n - 1 : cur + 1)); e.preventDefault(); }
                if (e.key === 'ArrowLeft')  { show(Math.max(0, cur < 0 ? n - 1 : cur - 1)); e.preventDefault(); }
                if (e.key === 'Escape') hide();
            });
            host.addEventListener('focus', () => { if (cur < 0) show(n - 1); });
            host.addEventListener('blur', hide);
            applyHighlight(row);
        }

        function applyHighlight(row) {
            const hk = highlights.get(decodeURIComponent(row.dataset.rowKey)) || null;
            row.classList.toggle('has-highlight', !!hk);
            row.querySelectorAll('[data-series]').forEach(el => el.classList.toggle('is-highlight', el.dataset.series === hk));
            row.querySelectorAll('button.series-pick').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.series === hk)));
            // bring the highlighted line (and its band) in front of the others
            const svg = row.querySelector('.line-chart svg');
            if (hk && svg) {
                const g = svg.querySelector('g');
                svg.querySelectorAll(`path.band[data-series="${hk}"], path.series[data-series="${hk}"]`).forEach(el => el.parentNode.appendChild(el));
            }
        }

        function drawAllCharts(root) {
            (root || document).querySelectorAll('.bullet-row').forEach(row => {
                if (row.querySelector('.line-chart')) drawChart(row);
            });
        }
        const drawAllLineCharts = drawAllCharts;

        // Global Bar / Line switch: redraw every chart (keeps the opened tile, dates, filters, highlights)
        const viewToggle = document.getElementById('view-toggle');
        viewToggle.addEventListener('click', e => {
            const btn = e.target.closest('button[data-view]');
            if (!btn || btn.dataset.view === chartView) return;
            chartView = btn.dataset.view;
            viewToggle.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
            document.querySelectorAll('#instances-list .bullet-row').forEach(row => {
                const inst = findInstance(row.dataset.inst);
                if (!inst) return;
                const tmp = document.createElement('div');
                tmp.innerHTML = metricRowHtml(inst, decodeURIComponent(row.dataset.metric));
                row.replaceWith(tmp.firstElementChild);
            });
            drawAllCharts();
        });

        // Click Mean / P95 / P99 / Max (badge, legend, or a bar piece) to highlight it; click again to clear
        document.addEventListener('click', e => {
            const pick = e.target.closest('.series-pick');
            if (!pick) return;
            e.stopPropagation();
            const row = pick.closest('.bullet-row');
            const rowKey = decodeURIComponent(row.dataset.rowKey);
            if (highlights.get(rowKey) === pick.dataset.series) highlights.delete(rowKey);
            else highlights.set(rowKey, pick.dataset.series);
            applyHighlight(row);
        });

        // Redraw line charts when the page width changes
        let resizeTimer = null;
        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => drawAllLineCharts(), 120);
        });

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

            container.innerHTML = metricRowHtml(targetInst, metricKey);
            container.classList.add('active');
            drawAllLineCharts(container);
        }

        // ---------- collapsible sections ----------
        // Remembers which sections are collapsed so a date-range change keeps them that way.
        const collapsedSections = new Set();

        function onSectionToggle(el) {
            if (el.open) { collapsedSections.delete(el.dataset.key); drawAllLineCharts(el); }
            else collapsedSections.add(el.dataset.key);
        }

        function sectionHtml(key, title, count, bodyHtml) {
            const isOpen = !collapsedSections.has(key);
            return `
                <details class="metric-section" data-key="${key}" ${isOpen ? 'open' : ''} ontoggle="onSectionToggle(this)">
                    <summary class="section-title">
                        <svg class="section-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3l5 5-5 5"/></svg>
                        <span>${title}</span>
                        <span class="section-count">${count}</span>
                    </summary>
                    ${bodyHtml}
                </details>`;
        }

        // ---------- render ----------
        function renderDashboard() {
            const container = document.getElementById('instances-list');
            container.innerHTML = '';

            const projects = Object.keys(dashboardData).sort();
            if (projects.length === 0) {
                const who = selection.type === 'all' ? '' : ` for <strong>${escapeHtml(selectionLabel())}</strong>`;
                container.innerHTML = `<div class="empty-state">No reports found${who} in the selected date range.</div>`;
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
                            pctRowsHtml += metricRowHtml(inst, metricKey);
                        } else {
                            let maxAllowedStr = "N/A";
                            const tileLimit = (inst.max_allowed || {})[metricKey];
                            if (tileLimit !== undefined && tileLimit !== null) {
                                maxAllowedStr = formatNumber(tileLimit);
                            } else if (label.includes("Connections") && inst.max_allowed_connections) {
                                maxAllowedStr = formatNumber(inst.max_allowed_connections);
                            }

                            kpiTilesHtml += `
                                <div id="tile-${safeInstId}-${safeId(metricKey)}"
                                     class="kpi-tile tile-${safeInstId}"
                                     onclick="handleTileClick('${safeInstId}', '${metricKey}')">
                                    <div class="kpi-tile-header-group">
                                        <div class="kpi-tile-header truncate" title="${label}">
                                            <span>${label}</span>
                                        </div>
                                        <div class="kpi-mean-label">Mean</div>
                                    </div>
                                    <div class="main-val-container">
                                        <div class="kpi-tile-main-val">${formatNumber(m.Mean)}</div>
                                        <div class="max-allowed-badge">Max Allowed: ${maxAllowedStr}</div>
                                    </div>
                                    <div class="kpi-tile-footer">
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--ink-p95);">P95</span>
                                            <span class="kpi-footer-val">${formatNumber(m.P95)}</span>
                                        </div>
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--ink-p99);">P99</span>
                                            <span class="kpi-footer-val">${formatNumber(m.P99)}</span>
                                        </div>
                                        <div class="kpi-footer-item">
                                            <span class="kpi-footer-label" style="color: var(--ink-max);">Max</span>
                                            <span class="kpi-footer-val">${formatNumber(m.Max)}</span>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }
                    });

                    const pctCount = (pctRowsHtml.match(/class="bullet-row"/g) || []).length;
                    const kpiCount = (kpiTilesHtml.match(/class="kpi-tile /g) || []).length;

                    const pctSection = pctRowsHtml ? sectionHtml(
                        `${safeInstId}::pct`, 'Capacity Utilization', pctCount,
                        `<div class="bullet-group">${pctRowsHtml}</div>`) : '';

                    const countSection = kpiTilesHtml ? sectionHtml(
                        `${safeInstId}::count`, 'IO & Traffic Statistics', kpiCount,
                        `<div class="kpi-tile-grid">${kpiTilesHtml}</div>
                         <div id="shared-bar-${safeInstId}" class="shared-bar-container"></div>`) : '';

                    const SPEC_ORDER = ['CPU', 'Memory', 'Storage'];
                    const specs = inst.specs || {};
                    const specSection = SPEC_ORDER.some(k => specs[k]) ? `
                        <div>
                            <div class="section-title">Provisioned Specifications</div>
                            <div class="spec-grid">
                                ${SPEC_ORDER.map(k => {
                                    const sp = specs[k];
                                    return `<div class="spec-card">
                                                <div class="spec-label">${k}</div>
                                                <div class="spec-value">${sp ? escapeHtml(sp.value) : 'N/A'}</div>
                                            </div>`;
                                }).join('')}
                            </div>
                        </div>` : '';

                    card.innerHTML = `
                        <div class="instance-header">
                            <div class="breadcrumb">
                                <span class="breadcrumb-project">${inst.project_id}</span>
                                <span class="breadcrumb-sep">/</span>
                                <span class="breadcrumb-instance">${inst.instance_name}</span>
                            </div>
                            <span class="days-badge" title="Reports found for ${inst.days_counted} of the ${currentRangeDays} day(s) in the selected range">${inst.days_counted} / ${currentRangeDays} Day(s) Aggregated</span>
                        </div>
                        ${specSection}
                        ${pctSection}
                        ${countSection}
                    `;
                    container.appendChild(card);
                });
            });
            drawAllLineCharts(container);
        }

        // ---------- date range + instance controls ----------
        const fromInput = document.getElementById('date-from');
        const toInput = document.getElementById('date-to');
        const presetBtns = document.querySelectorAll('.preset-btn');
        const customBtn = document.getElementById('custom-btn');
        const popover = document.getElementById('custom-popover');
        const customError = document.getElementById('custom-error');
        const scopeBtn = document.getElementById('scope-btn');
        const scopeMenu = document.getElementById('scope-menu');
        const scopeSearch = document.getElementById('scope-search');
        const scopeList = document.getElementById('scope-list');

        let currentFrom = null, currentTo = null, currentPreset = 'week', currentRangeDays = 0;

        // Today's date in the viewer's local time zone
        function todayIso() {
            const t = new Date();
            return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
        }

        function daysInclusive(fromIso, toIso) {
            const ms = new Date(toIso + "T00:00:00") - new Date(fromIso + "T00:00:00");
            return Math.round(ms / 86400000) + 1;
        }

        function refresh() {
            dashboardData = aggregate(currentFrom, currentTo);
            currentRangeDays = daysInclusive(currentFrom, currentTo);
            const reportDays = new Set(filteredRecords(currentFrom, currentTo).map(r => r.d)).size;
            document.getElementById('range-summary').innerHTML =
                `Showing <strong>${prettyDate(currentFrom)}</strong> – <strong>${prettyDate(currentTo)}</strong> · ${reportDays} report day(s)`;
            presetBtns.forEach(b => b.classList.toggle('active', b.dataset.days === currentPreset));
            scopeBtn.classList.toggle('active', selection.type !== 'all');
            scopeBtn.textContent = selectionLabel();
            scopeBtn.title = selectionLabel();
            renderDashboard();
        }

        function applyRange(fromIso, toIso, preset) {
            if (fromIso > toIso) [fromIso, toIso] = [toIso, fromIso];  // tolerate reversed picks
            currentFrom = fromIso;
            currentTo = toIso;
            currentPreset = preset;
            refresh();
        }

        // Presets run from the start of the current week / month up to today.
        // Weeks start on Monday.
        const MAX_CUSTOM_DAYS = 30;
        function applyPreset(which) {
            const today = todayIso();
            const t = new Date(today + "T00:00:00");
            if (which === 'week') {
                const sinceMonday = (t.getDay() + 6) % 7;       // Mon=0 ... Sun=6
                return applyRange(shiftDate(today, -sinceMonday), today, 'week');
            }
            if (which === 'month') return applyRange(today.slice(0, 8) + '01', today, 'month');
        }

        // ----- Custom range popover -----
        function openPopover() {
            fromInput.value = currentFrom;
            toInput.value = currentTo;
            customError.textContent = '';
            popover.hidden = false;
            customBtn.setAttribute('aria-expanded', 'true');
            // keep the popover on screen
            popover.style.left = '0px';
            const r = popover.getBoundingClientRect();
            const overflowRight = r.right - (window.innerWidth - 8);
            if (overflowRight > 0) popover.style.left = `${-Math.min(overflowRight, r.left - 8)}px`;
            fromInput.focus();
        }

        function closePopover() {
            popover.hidden = true;
            customBtn.setAttribute('aria-expanded', 'false');
        }

        function applyCustom() {
            if (!fromInput.value || !toInput.value) {
                customError.textContent = 'Pick both a From and a To date.';
                return;
            }
            const lo = fromInput.value < toInput.value ? fromInput.value : toInput.value;
            const hi = fromInput.value < toInput.value ? toInput.value : fromInput.value;
            if (daysInclusive(lo, hi) > MAX_CUSTOM_DAYS) {
                customError.textContent = `Custom range can be at most ${MAX_CUSTOM_DAYS} days (this is ${daysInclusive(lo, hi)}).`;
                return;
            }
            closePopover();
            applyRange(fromInput.value, toInput.value, 'custom');
        }

        // ----- Project / instance dropdown -----
        const expandedProjects = new Set();
        const CHECK = `<svg class="scope-check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5"/></svg>`;
        const CHEV = `<svg class="scope-chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3l5 5-5 5"/></svg>`;

        const isSel = (type, p, i) => selection.type === type && (type === 'all' || selection.p === p) && (type !== 'instance' || selection.i === i);
        const optionBtn = (type, p, i, text, extraClass) => `
            <button type="button" role="menuitemradio" aria-checked="${isSel(type, p, i)}"
                    class="scope-item ${extraClass || ''} ${isSel(type, p, i) ? 'selected' : ''}"
                    data-type="${type}" data-p="${escapeHtml(p || '')}" data-i="${escapeHtml(i || '')}">
                <span class="scope-name" title="${escapeHtml(text)}">${escapeHtml(text)}</span>${CHECK}
            </button>`;

        function renderScopeList() {
            const q = scopeSearch.value.trim().toLowerCase();
            let html = q ? '' : optionBtn('all', '', '', 'All instances') + `<div class="scope-sep"></div>`;
            let shown = 0;

            projectList.forEach(proj => {
                const all = instanceList.filter(x => x.p === proj);
                const projMatch = !q || proj.toLowerCase().includes(q);
                const insts = projMatch ? all : all.filter(x => x.i.toLowerCase().includes(q));
                if (!insts.length) return;
                shown++;
                const open = !!q || expandedProjects.has(proj);
                html += `
                    <button type="button" class="scope-item scope-project" aria-expanded="${open}" data-project="${escapeHtml(proj)}">
                        ${CHEV}<span class="scope-name" title="${escapeHtml(proj)}">${escapeHtml(proj)}</span>
                        <span class="scope-count">${all.length}</span>
                    </button>`;
                if (open) {
                    html += `<div class="scope-children" role="group" aria-label="${escapeHtml(proj)}">` +
                        optionBtn('project', proj, '', `All in ${proj}`, 'scope-all-in') +
                        insts.map(x => optionBtn('instance', proj, x.i, x.i)).join('') +
                        `</div>`;
                }
            });

            if (!shown) html += `<div class="scope-empty">No projects or instances match “${escapeHtml(scopeSearch.value.trim())}”</div>`;
            scopeList.innerHTML = html;
        }

        function openScopeMenu() {
            closePopover();
            if (selection.type !== 'all') expandedProjects.add(selection.p);
            scopeSearch.value = '';
            renderScopeList();
            scopeMenu.hidden = false;
            scopeBtn.setAttribute('aria-expanded', 'true');
            scopeMenu.style.left = '0px';
            const r = scopeMenu.getBoundingClientRect();
            const overflowRight = r.right - (window.innerWidth - 8);
            if (overflowRight > 0) scopeMenu.style.left = `${-Math.min(overflowRight, r.left - 8)}px`;
            const sel = scopeList.querySelector('.scope-item.selected');
            (sel || scopeSearch).focus();
        }

        function closeScopeMenu() {
            scopeMenu.hidden = true;
            scopeBtn.setAttribute('aria-expanded', 'false');
        }

        function initScopeMenu() {
            scopeBtn.addEventListener('click', e => {
                e.stopPropagation();
                scopeMenu.hidden ? openScopeMenu() : closeScopeMenu();
            });
            scopeMenu.addEventListener('click', e => {
                e.stopPropagation();
                const projBtn = e.target.closest('.scope-project');
                if (projBtn) {
                    const proj = projBtn.dataset.project;
                    expandedProjects.has(proj) ? expandedProjects.delete(proj) : expandedProjects.add(proj);
                    renderScopeList();
                    const again = [...scopeList.querySelectorAll('.scope-project')].find(b => b.dataset.project === proj);
                    if (again) again.focus();
                    return;
                }
                const opt = e.target.closest('.scope-item[data-type]');
                if (!opt) return;
                const t = opt.dataset.type;
                selection = t === 'all' ? { type: 'all' }
                          : t === 'project' ? { type: 'project', p: opt.dataset.p }
                          : { type: 'instance', p: opt.dataset.p, i: opt.dataset.i };
                closeScopeMenu();
                scopeBtn.focus();
                refresh();
            });
            scopeSearch.addEventListener('input', renderScopeList);
            // arrow keys move through the visible rows
            scopeMenu.addEventListener('keydown', e => {
                if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
                const items = [scopeSearch, ...scopeList.querySelectorAll('.scope-item')];
                const at = items.indexOf(document.activeElement);
                const next = e.key === 'ArrowDown' ? Math.min(items.length - 1, at + 1) : Math.max(0, at - 1);
                items[next].focus();
                e.preventDefault();
            });
            document.addEventListener('click', () => { if (!scopeMenu.hidden) closeScopeMenu(); });
            document.addEventListener('keydown', e => { if (e.key === 'Escape' && !scopeMenu.hidden) { closeScopeMenu(); scopeBtn.focus(); } });
        }

        if (!minDate) {
            document.getElementById('instances-list').innerHTML =
                `<div class="empty-state">No report data was embedded in this dashboard.</div>`;
            document.querySelector('.filter-bar').style.display = 'none';
        } else {
            const maxPick = maxDate > todayIso() ? maxDate : todayIso();
            const clampWindow = () => {
                const span = MAX_CUSTOM_DAYS - 1;
                const minOf = (a, b) => a < b ? a : b, maxOf = (a, b) => a > b ? a : b;
                toInput.min = fromInput.value ? maxOf(minDate, fromInput.value) : minDate;
                toInput.max = fromInput.value ? minOf(maxPick, shiftDate(fromInput.value, span)) : maxPick;
                fromInput.min = toInput.value ? maxOf(minDate, shiftDate(toInput.value, -span)) : minDate;
                fromInput.max = toInput.value ? minOf(maxPick, toInput.value) : maxPick;
                customError.textContent = '';
            };
            fromInput.addEventListener('change', clampWindow);
            toInput.addEventListener('change', clampWindow);
            fromInput.addEventListener('focus', clampWindow);

            presetBtns.forEach(b => {
                if (b === customBtn) return;
                b.addEventListener('click', () => { closePopover(); applyPreset(b.dataset.days); });
            });
            customBtn.addEventListener('click', e => {
                e.stopPropagation();
                closeScopeMenu();
                popover.hidden ? openPopover() : closePopover();
            });
            document.getElementById('custom-apply').addEventListener('click', applyCustom);
            document.getElementById('custom-cancel').addEventListener('click', closePopover);
            [fromInput, toInput].forEach(inp => inp.addEventListener('keydown', e => { if (e.key === 'Enter') applyCustom(); }));
            popover.addEventListener('click', e => e.stopPropagation());
            document.addEventListener('click', () => { if (!popover.hidden) closePopover(); });
            document.addEventListener('keydown', e => { if (e.key === 'Escape' && !popover.hidden) { closePopover(); customBtn.focus(); } });

            initScopeMenu();

            applyPreset('week');
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
    parser.add_argument("--bucket", default="db_health_check_report_store", help="Name of GCS bucket")
    parser.add_argument("--prefix", default="", help='Only read reports under this folder in the bucket, e.g. "Updated_databasehealth_report_samples/"')
    parser.add_argument("--local-dir", help="Read reports from a local folder instead of GCS (for testing)")
    parser.add_argument("--history-days", type=int, default=90,
                        help="How many days of reports to embed; the dashboard's date picker can choose any range inside this")
    parser.add_argument("--out", default="trend_analysis.html", help="Output HTML file path")
    args = parser.parse_args()

    records = collect_daily_records(bucket_name=args.bucket, local_dir=args.local_dir, prefix=args.prefix,
                                    history_days=args.history_days)

    if not records:
        print(f"❌ No valid HTML report data found for the past {args.history_days} days.")
    else:
        generate_trend_html(records, args.out)
