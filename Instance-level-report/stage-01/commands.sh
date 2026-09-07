
export PROJECT_ID="chtp-g3-staging-1-7fa98a9d"
export INSTANCE_ID="target-restore"

export START_TIME=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
export END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)


# FOR GIVEN TIME INTERVAL 

export START_TIME="2026-08-20T11:30:00Z"
export END_TIME="2026-08-20T23:00:00Z"

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fcpu%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MEAN"\
| jq '.timeSeries[0].points[] | {
    Timestamp: .interval.endTime,
    CPU_Percentage: ((.value.doubleValue * 10000 | round) / 100)
  }'

# MAX CPU UTILIZATION

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fcpu%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MAX" \
| jq '.timeSeries[0].points[] | {
    "Peak_CPU_Percentage": ((.value.doubleValue * 10000 | round) / 100),
    "Window_Start": .interval.startTime,
    "Window_End": .interval.endTime
  }'

# MAX MEMORY UTILIZATION

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fmemory%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MAX" \
| jq '.timeSeries[0].points[] | {
    "Peak_Memory_Percentage": ((.value.doubleValue * 10000 | round) / 100),
    "Window_Start": .interval.startTime,
    "Window_End": .interval.endTime
  }'


# MAX DISK UTILIZATION

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fdisk%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MAX" \
| jq '.timeSeries[0].points[] | {
    "Peak_Disk_Percentage": ((.value.doubleValue * 10000 | round) / 100),
    "Window_Start": .interval.startTime,
    "Window_End": .interval.endTime
  }'


# ACTIVE CONNECTIONS 

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fnetwork%2Fconnections%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MAX" \
| jq '.timeSeries[0].points[] | {
    "Peak_Active_Connections": .value.int64Value,
    "Window_Start": .interval.startTime,
    "Window_End": .interval.endTime
  }'
