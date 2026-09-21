import json
import os
import sys

def generate_html_report(input_json_file, output_html_file):
    # 1. Read and parse the input JSON file
    try:
        with open(input_json_file, 'r') as f:
            # Load to validate it's proper JSON
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: The file {input_json_file} was not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        sys.exit(1)

    # Convert the parsed Python dictionary back to a formatted JSON string
    json_string = json.dumps(data, indent=4)

    # 2. Define the HTML Template
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Database Health Check Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; color: #333; }
        h1, h2, h3, h4 { color: #2c3e50; }
        .header-controls {
            background-color: #f8f9fa; padding: 15px; border-radius: 5px;
            margin-bottom: 20px; border: 1px solid #ddd;
        }
        label { font-weight: bold; margin-right: 10px; }
        select { padding: 5px; font-size: 16px; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
        th, td { border: 1px solid #bdc3c7; padding: 10px; text-align: left; }
        th { background-color: #ecf0f1; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .section-container { margin-bottom: 40px; }
    </style>
</head>
<body>

    <div class="header-controls">
        <h1 id="project-name">Project Name: DB Health Check</h1>
        <label for="instance-select">Select Instance: </label>
        <select id="instance-select"></select>
    </div>

    <!-- Section 1 -->
    <div class="section-container">
        <h3>Section 1: Provisioned Specs</h3>
        <table>
            <thead>
                <tr>
                    <th>Engine</th>
                    <th>Edition</th>
                    <th>CPU</th>
                    <th>Memory</th>
                    <th>Storage</th>
                    <th>Availability</th>
                    <th>Read Replica Count</th>
                    <th>Last Backup Time</th>
                </tr>
            </thead>
            <tbody id="specs-body">
                <!-- Dynamically populated -->
            </tbody>
        </table>
    </div>

    <!-- Section 2 -->
    <div class="section-container">
        <h3>Section 2: Instance Monitoring Details</h3>
        <table>
            <thead>
                <tr>
                    <th>Metric</th>
                    <th>Mean</th>
                    <th>P95</th>
                    <th>P99</th>
                    <th>Max</th>
                </tr>
            </thead>
            <tbody id="monitoring-body">
                <!-- Dynamically populated -->
            </tbody>
        </table>
    </div>

    <!-- Section 3 -->
    <div class="section-container">
        <h3>Section 3: Database Health & Performance</h3>

        <h4>Full Table Scan Queries</h4>
        <div id="fts-container"></div>

        <h4>Top Fragmented Tables</h4>
        <div id="fragmentation-container"></div>

        <h4>Unused Indexes</h4>
        <div id="unused-indexes-container"></div>
    </div>

    <script>
        // Data injected from Python script
        const reportData = __JSON_DATA_PLACEHOLDER__;

        const instanceSelect = document.getElementById("instance-select");

        // Helper to handle Nulls
        const fmt = (val) => val === null || val === undefined ? "N/A" : val;

        // Initialize Dropdown
        Object.keys(reportData).forEach(instance => {
            const option = document.createElement("option");
            option.value = instance;
            option.textContent = instance;
            instanceSelect.appendChild(option);
        });

        // Event Listener for Dropdown Change
        instanceSelect.addEventListener("change", (e) => {
            renderDashboard(e.target.value);
        });

        // Generic Table Builder Function
        function buildDynamicTable(dataArray, containerId, emptyMessage) {
            const container = document.getElementById(containerId);
            container.innerHTML = ""; // Clear existing

            if (!dataArray || dataArray.length === 0) {
                container.innerHTML = `<p><em>${emptyMessage}</em></p>`;
                return;
            }

            const table = document.createElement("table");
            const thead = document.createElement("thead");
            const tbody = document.createElement("tbody");

            // Build headers based on the keys of the first object
            const headers = Object.keys(dataArray[0]);
            const trHead = document.createElement("tr");
            headers.forEach(header => {
                const th = document.createElement("th");
                th.textContent = header.replace(/_/g, " ").toUpperCase();
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);

            // Build rows
            dataArray.forEach(row => {
                const tr = document.createElement("tr");
                headers.forEach(header => {
                    const td = document.createElement("td");
                    td.textContent = fmt(row[header]);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });

            table.appendChild(thead);
            table.appendChild(tbody);
            container.appendChild(table);
        }

        function renderDashboard(instanceName) {
            const data = reportData[instanceName];

            // 1. Populate Provisioned Specs
            const specs = data.provisioned_specs || {};
            const specsBody = document.getElementById("specs-body");
            specsBody.innerHTML = `
                <tr>
                    <td>${fmt(specs.Engine)}</td>
                    <td>${fmt(specs.Edition)}</td>
                    <td>${fmt(specs.CPU)}</td>
                    <td>${fmt(specs.Memory)}</td>
                    <td>${fmt(specs.Storage)}</td>
                    <td>${fmt(specs.Availability)}</td>
                    <td>${fmt(specs["Read Replica count"])}</td>
                    <td>${fmt(specs["Last Backup time"])}</td>
                </tr>
            `;

            // 2. Populate Resource Utilization
            const res = data.resource_utilization || {};
            const resBody = document.getElementById("monitoring-body");
            const metricsToMap = [
                { title: "CPU Utilization (%)", key: "cpu_utilization" },
                { title: "Memory Utilization (%)", key: "memory_utilization" },
                { title: "Disk Read IO Ops", key: "disk_read_ops" },
                { title: "Disk Write IO Ops", key: "disk_write_ops" },
                { title: "Connections", key: "connections" },
                { title: "Disk Utilization (%)", key: "disk_utilization" },
                { title: "Disk Bytes Used / Throughput", key: "disk_bytes_used" }
            ];

            resBody.innerHTML = "";
            metricsToMap.forEach(metric => {
                const metricData = res[metric.key];
                if(metricData) {
                    resBody.innerHTML += `
                        <tr>
                            <td><strong>${metric.title}</strong></td>
                            <td>${fmt(metricData.mean)}</td>
                            <td>${fmt(metricData.p95)}</td>
                            <td>${fmt(metricData.p99)}</td>
                            <td>${fmt(metricData.max)}</td>
                        </tr>
                    `;
                }
            });

            // 3. Database Health Section (Dynamic schema handling)
            const health = data.health_checks || {};
            
            // Full Table Scans
            const ftsData = health.performance?.full_table_scans || [];
            buildDynamicTable(ftsData, "fts-container", "No full table scans detected.");

            // Table Fragmentation (Sort by fragmentation/bloat percent if applicable)
            let fragData = health.storage_and_system?.table_fragmentation_status || [];
            buildDynamicTable(fragData, "fragmentation-container", "No fragmentation data available.");

            // Unused Indexes
            const idxData = health.indexes?.unused_indexes || [];
            buildDynamicTable(idxData, "unused-indexes-container", "No unused indexes detected.");
        }

        // Initial render for the first instance
        if (Object.keys(reportData).length > 0) {
            renderDashboard(Object.keys(reportData)[0]);
        }
    </script>
</body>
</html>"""

    # 3. Inject the parsed JSON data into the HTML string
    final_html = html_template.replace("__JSON_DATA_PLACEHOLDER__", json_string)

    # 4. Write the final string to the output HTML file
    with open(output_html_file, 'w') as f:
        f.write(final_html)
        
    print(f"Success! Health check report generated at: {output_html_file}")

if __name__ == "__main__":
    # Define input JSON and desired output HTML file names
    # You can change these to match the names of your local files
    INPUT_JSON = 'data.json'
    OUTPUT_HTML = 'health_check_report.html'
    
    generate_html_report(INPUT_JSON, OUTPUT_HTML)