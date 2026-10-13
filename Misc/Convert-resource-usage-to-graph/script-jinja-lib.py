from google.cloud import monitoring_v3
from datetime import datetime
import pandas as pd
import subprocess
import time
import json
import math
from jinja2 import Template

# ==========================================
# 1. HTML & Chart.js Template (Jinja2)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cloud SQL Health Check - {{ instance_id }}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            padding: 20px; 
            background-color: #f8f9fa; 
            color: #333;
        }
        .dashboard-header {
            text-align: center;
            margin-bottom: 30px;
        }
        .chart-card { 
            background: white; 
            padding: 20px; 
            border-radius: 8px; 
            box-shadow: 0 4px 6px rgba(0,0,0,0.1); 
            max-width: 1200px; 
            margin: 0 auto 30px auto; 
        }
        .chart-card h3 {
            margin-top: 0;
            color: #444;
            text-align: center;
        }
        .canvas-wrapper {
            position: relative;
            height: 300px; 
            width: 100%;
        }
    </style>
</head>
<body>

    <div class="dashboard-header">
        <h2>Database Health Report (Last 24h) - {{ instance_id }}</h2>
    </div>

    <!-- CPU Chart -->
    <div class="chart-card">
        <h3>CPU Utilization</h3>
        <div class="canvas-wrapper"><canvas id="cpuChart"></canvas></div>
    </div>

    <!-- Memory Chart -->
    <div class="chart-card">
        <h3>Memory Utilization</h3>
        <div class="canvas-wrapper"><canvas id="memChart"></canvas></div>
    </div>

    <!-- Disk Chart -->
    <div class="chart-card">
        <h3>Disk Utilization</h3>
        <div class="canvas-wrapper"><canvas id="diskChart"></canvas></div>
    </div>
    
    <!-- Connections Chart -->
    <div class="chart-card">
        <h3>Database Connections</h3>
        <div class="canvas-wrapper"><canvas id="connChart"></canvas></div>
    </div>

    <script>
        const labels = {{ labels | safe }};

        // Reusable function updated to accept dynamic units (e.g. '%' or '')
        function createMetricChart(canvasId, metricData, yConfig, unit) {
            const ctx = document.getElementById(canvasId).getContext('2d');
            return new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels, 
                    datasets: [
                        {
                            label: 'Mean',
                            data: metricData.mean,
                            borderColor: '#0d6efd',
                            backgroundColor: 'rgba(13, 110, 253, 0.15)', 
                            borderWidth: 1.5,                        
                            tension: 0.3, 
                            fill: 'origin',
                            pointRadius: 0,
                            pointHoverRadius: 6
                        },
                        {
                            label: 'P95',
                            data: metricData.p95,
                            borderColor: '#fd7e14',
                            backgroundColor: 'rgba(253, 126, 20, 0.15)',
                            borderWidth: 1.5,                                                
                            tension: 0.3, 
                            fill: '-1', 
                            pointRadius: 0,
                            pointHoverRadius: 6
                        },
                        {
                            label: 'P99',
                            data: metricData.p99,
                            borderColor: '#6c757d', 
                            backgroundColor: 'rgba(108, 117, 125, 0.15)',
                            borderWidth: 1.5,                  
                            tension: 0.3, 
                            fill: '-1', 
                            pointRadius: 0,
                            pointHoverRadius: 6
                        },
                        {
                            label: 'Max',
                            data: metricData.max,
                            borderColor: '#dc3545', 
                            backgroundColor: 'rgba(220, 53, 69, 0.15)',
                            borderWidth: 1.5,
                            tension: 0.3, 
                            fill: '-1', 
                            pointRadius: 0,
                            pointHoverRadius: 6
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false, 
                    
                    onHover: function(e, elements, chart) {
                        if (elements && elements.length > 0) {
                            chart.hoveredDatasetIndex = elements[0].datasetIndex;
                        }
                    },
                    
                    interaction: { mode: 'index', intersect: false },
                    hover: { mode: 'nearest', intersect: false },
                    
                    plugins: {
                        tooltip: {
                            padding: 12,
                            boxPadding: 6,
                            usePointStyle: false, 
                            itemSort: function(a, b) {
                                const hoveredIndex = a.chart.hoveredDatasetIndex;
                                if (a.datasetIndex === hoveredIndex) return -1;
                                if (b.datasetIndex === hoveredIndex) return 1;
                                return b.raw - a.raw; 
                            },
                            callbacks: {
                                title: function(context) {
                                    let d = new Date(context[0].label);
                                    let day = d.getDate();
                                    let month = d.toLocaleString('en-US', { month: 'short' });
                                    let year = d.getFullYear();
                                    let time = d.toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
                                    return `${day}, ${month}, ${year}, ${time}`;
                                },
                                label: function(context) {
                                    let label = context.dataset.label || '';
                                    if (label) label += ': ';
                                    if (context.parsed.y !== null) label += context.parsed.y + unit; // Dynamic unit applied here
                                    return label;
                                }
                            }
                        },
                        legend: { labels: { color: '#333' } }
                    },
                    scales: {
                        y: {
                            min: yConfig.min,
                            max: yConfig.max,
                            title: { display: true, text: yConfig.title, color: '#666' },
                            grid: { display: true, drawBorder: false },
                            ticks: { stepSize: yConfig.step }
                        },
                        x: {
                            title: { display: false },
                            offset: true, 
                            grid: { drawOnChartArea: false, drawTicks: true, tickLength: 8 },
                            ticks: {
                                callback: function(value, index, ticks) {
                                    let label = this.getLabelForValue(value);
                                    let d = new Date(label);
                                    if (index % 2 !== 0) return null;
                                    if (d.getHours() === 0 && d.getMinutes() === 0) {
                                        return d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
                                    } else {
                                        return d.toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
                                    }
                                }
                            }
                        }
                    }
                }
            });
        }

        // Initialize all four charts
        createMetricChart('cpuChart', {{ cpu_data | safe }}, {{ cpu_y_config | safe }}, '%');
        createMetricChart('memChart', {{ mem_data | safe }}, {{ mem_y_config | safe }}, '%');
        createMetricChart('diskChart', {{ disk_data | safe }}, {{ disk_y_config | safe }}, '%');
        createMetricChart('connChart', {{ conn_data | safe }}, {{ conn_y_config | safe }}, ''); // No % for counts

    </script>
</body>
</html>
"""

# ==========================================
# 2. Data Fetching & Processing
# ==========================================
def get_database_metrics(project_id, instance_id):
    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"
    
    now = int(time.time())
    start_time_sec = now - 86400
    interval = monitoring_v3.TimeInterval({
        "end_time": {"seconds": now}, 
        "start_time": {"seconds": start_time_sec} 
    })

    aggregation = monitoring_v3.Aggregation({
        "alignment_period": {"seconds": 60}, 
        "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
    })

    metrics = {
        "cpu": "cloudsql.googleapis.com/database/cpu/utilization",
        "memory": "cloudsql.googleapis.com/database/memory/utilization",
        "disk": "cloudsql.googleapis.com/database/disk/utilization",
        "connections": "cloudsql.googleapis.com/database/network/connections"
    }
    
    dataframes = []
    
    for metric_alias, metric_type in metrics.items():
        print(f"Fetching 24h {metric_alias.upper()} data for {instance_id}...")
        results = client.list_time_series(
            request={
                "name": project_name,
                "filter": f'metric.type="{metric_type}" AND resource.labels.database_id="{project_id}:{instance_id}"',
                "interval": interval,
                "aggregation": aggregation,
            }
        )
        
        data = []
        for result in results:
            for point in result.points:
                # Fallback to int64_value if double_value is empty (common for raw counts)
                val = getattr(point.value, 'double_value', getattr(point.value, 'int64_value', 0.0))
                data.append({
                    "time": pd.to_datetime(point.interval.start_time.timestamp(), unit='s'),
                    metric_alias: val
                })
        
        if not data:
            print(f"No data returned for {metric_alias}.")
            df = pd.DataFrame(columns=["time", metric_alias])
            df.set_index("time", inplace=True)
            dataframes.append(df)
        else:
            df = pd.DataFrame(data)
            df.set_index("time", inplace=True)
            dataframes.append(df)
            
    merged_df = dataframes[0]
    for i in range(1, len(dataframes)):
        merged_df = merged_df.join(dataframes[i], how='outer')

    if merged_df.empty:
        print("No data available across all metrics.")
        return pd.DataFrame()

    hourly_stats = merged_df.resample('1h').agg(
        cpu_mean=('cpu', 'mean'), cpu_max=('cpu', 'max'), cpu_p95=('cpu', lambda x: x.quantile(0.95)), cpu_p99=('cpu', lambda x: x.quantile(0.99)),
        mem_mean=('memory', 'mean'), mem_max=('memory', 'max'), mem_p95=('memory', lambda x: x.quantile(0.95)), mem_p99=('memory', lambda x: x.quantile(0.99)),
        disk_mean=('disk', 'mean'), disk_max=('disk', 'max'), disk_p95=('disk', lambda x: x.quantile(0.95)), disk_p99=('disk', lambda x: x.quantile(0.99)),
        conn_mean=('connections', 'mean'), conn_max=('connections', 'max'), conn_p95=('connections', lambda x: x.quantile(0.95)), conn_p99=('connections', lambda x: x.quantile(0.99))
    )

    # Multiply ONLY the utilization metrics by 100 (connections stay as raw counts)
    utilization_cols = [c for c in hourly_stats.columns if not c.startswith('conn_')]
    hourly_stats[utilization_cols] = hourly_stats[utilization_cols] * 100 

    start_dt = pd.to_datetime(start_time_sec, unit='s').ceil('h')
    end_dt = pd.to_datetime(now, unit='s').floor('h')
    full_24h_index = pd.date_range(start=start_dt, end=end_dt, freq='h')
    
    hourly_stats = hourly_stats.reindex(full_24h_index)
    
    return hourly_stats

# Helper updated to support uncapped values and dynamic scaling for large connection counts
def calculate_y_axis_config(df, columns, is_percentage, y_title):
    overall_min = df[columns].min().min()
    overall_max = df[columns].max().max()
    
    if pd.isna(overall_min) or pd.isna(overall_max):
        return {"min": 0, "max": 100 if is_percentage else 1000, "step": 10, "title": y_title}
        
    data_range = overall_max - overall_min
    
    if data_range <= 10: y_step = 2
    elif data_range <= 25: y_step = 5
    elif data_range <= 50: y_step = 10
    elif data_range <= 100: y_step = 20
    else:
        # Scale step size dynamically for large data ranges (like connections)
        magnitude = 10 ** math.floor(math.log10(data_range))
        normalized = data_range / magnitude
        if normalized <= 2: y_step = 0.5 * magnitude
        elif normalized <= 5: y_step = 1 * magnitude
        else: y_step = 2 * magnitude

    y_min = max(0, math.floor(overall_min / y_step) * y_step - y_step)
    
    if is_percentage:
        y_max = min(100, math.ceil(overall_max / y_step) * y_step + y_step)
    else:
        y_max = math.ceil(overall_max / y_step) * y_step + y_step
        
    return {"min": y_min, "max": y_max, "step": y_step, "title": y_title}

def clean_series(series):
    return [None if pd.isna(x) else round(x, 2) for x in series]

# ==========================================
# 3. Execution & HTML Generation
# ==========================================
if __name__ == "__main__":
    PROJECT_ID = "playground-poc-2"
    INSTANCE_ID = "csql-mysql-instance"
    
    df_metrics = get_database_metrics(PROJECT_ID, INSTANCE_ID)
    
    if not df_metrics.empty:
        print("\nGenerating HTML dashboard...")
        
        labels = df_metrics.index.strftime('%Y-%m-%dT%H:%M:%S').tolist()
        
        cpu_data = {
            "mean": clean_series(df_metrics['cpu_mean']), "max": clean_series(df_metrics['cpu_max']),
            "p95": clean_series(df_metrics['cpu_p95']), "p99": clean_series(df_metrics['cpu_p99'])
        }
        cpu_y_config = calculate_y_axis_config(df_metrics, ['cpu_mean', 'cpu_p95', 'cpu_p99', 'cpu_max'], True, 'Utilization (%)')

        mem_data = {
            "mean": clean_series(df_metrics['mem_mean']), "max": clean_series(df_metrics['mem_max']),
            "p95": clean_series(df_metrics['mem_p95']), "p99": clean_series(df_metrics['mem_p99'])
        }
        mem_y_config = calculate_y_axis_config(df_metrics, ['mem_mean', 'mem_p95', 'mem_p99', 'mem_max'], True, 'Utilization (%)')

        disk_data = {
            "mean": clean_series(df_metrics['disk_mean']), "max": clean_series(df_metrics['disk_max']),
            "p95": clean_series(df_metrics['disk_p95']), "p99": clean_series(df_metrics['disk_p99'])
        }
        disk_y_config = calculate_y_axis_config(df_metrics, ['disk_mean', 'disk_p95', 'disk_p99', 'disk_max'], True, 'Utilization (%)')

        conn_data = {
            "mean": clean_series(df_metrics['conn_mean']), "max": clean_series(df_metrics['conn_max']),
            "p95": clean_series(df_metrics['conn_p95']), "p99": clean_series(df_metrics['conn_p99'])
        }
        conn_y_config = calculate_y_axis_config(df_metrics, ['conn_mean', 'conn_p95', 'conn_p99', 'conn_max'], False, 'Connections (Count)')

        template = Template(HTML_TEMPLATE)
        rendered_html = template.render(
            instance_id=INSTANCE_ID,
            labels=json.dumps(labels),
            cpu_data=json.dumps(cpu_data), cpu_y_config=json.dumps(cpu_y_config),
            mem_data=json.dumps(mem_data), mem_y_config=json.dumps(mem_y_config),
            disk_data=json.dumps(disk_data), disk_y_config=json.dumps(disk_y_config),
            conn_data=json.dumps(conn_data), conn_y_config=json.dumps(conn_y_config)
        )
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{INSTANCE_ID}_health_report_{timestamp}.html"
        
        with open(output_filename, "w") as f:
            f.write(rendered_html)
            
        print(f"Report successfully saved to: {output_filename}")
        
        # --- Upload to Google Cloud Storage ---
        print(f"Uploading {output_filename} to Google Cloud Storage...")
        gcs_destination = "gs://db_health_check_report_store/demo_montoring_outputs/"
        
        try:
            subprocess.run(["gcloud", "storage", "cp", output_filename, gcs_destination], check=True)
            print("Upload to GCS successful!")
        except subprocess.CalledProcessError as e:
            print(f"Failed to upload report to GCS: {e}")
            
    else:
        print("Skipping HTML report generation because there is no data.")