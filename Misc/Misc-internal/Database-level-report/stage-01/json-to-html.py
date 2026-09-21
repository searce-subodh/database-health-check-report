import json

# Load the JSON data
with open("database_health_report.json", "r") as f:
    report_data = json.load(f)

html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Database Health Check Report</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background-color: #f8fafc; color: #1e293b; padding: 20px; }
        h1 { color: #0f172a; border-bottom: 2px solid #2563eb; padding-bottom: 10px; }
        .server-card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .category-title { font-size: 1.2em; font-weight: bold; color: #2563eb; margin-top: 15px; border-bottom: 1px solid #e2e8f0; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; margin-bottom: 20px; }
        th, td { border: 1px solid #e2e8f0; padding: 10px; text-align: left; font-size: 0.9em; }
        th { background-color: #334155; color: white; }
        tr:nth-child(even) { background-color: #f8fafc; }
        .alert-badge { font-weight: bold; color: #991b1b; background-color: #fee2e2; padding: 4px 8px; border-radius: 4px; }
    </style>
</head>
<body>
    <h1>🏥 Database Health Check Audit Report</h1>
"""

for server, categories in report_data.items():
    html_content += f'<div class="server-card"><h2>🖥️ Server: {server}</h2>'
    for category, queries in categories.items():
        html_content += (
            f'<div class="category-title">Category: {category.upper()}</div>'
        )
        for key, rows in queries.items():
            print("KEY : ", key)
            html_content += f"<h4>Metric: {key}</h4>"
            if not rows:
                html_content += (
                    "<p><i>No issue or records flagged.</i></p>"
                )
                continue

            # Build Table Headers dynamically from JSON keys

            print( "ROWS : ", rows)

            headers = rows[0].keys()
            html_content += (
                "<table><thead><tr>"
                + "".join([f"<th>{h}</th>" for h in headers])
                + "</tr></thead><tbody>"
            )

            # Build Rows
            for row in rows:
                html_content += "<tr>"
                for h in headers:
                    val = str(row[h])
                    if "EXPIRED" in val or "NO PASSWORD" in val:
                        val = f'<span class="alert-badge">{val}</span>'
                    html_content += f"<td>{val}</td>"
                html_content += "</tr>"
            html_content += "</tbody></table>"
    html_content += "</div>"

html_content += "</body></html>"

# Save HTML file
with open("Database_Health_Report.html", "w") as f:
    f.write(html_content)

print("Rendered HTML report saved as Database_Health_Report.html")