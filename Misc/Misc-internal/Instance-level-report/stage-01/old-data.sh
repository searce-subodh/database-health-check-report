# Option 1: Hourly Averages (3600s)
# 1. Set variables
export PROJECT_ID="data-analyst-504209"
export INSTANCE_ID="csql-mysql-db" 

export START_TIME=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
export END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 2. Run curl command
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fcpu%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=3600s\
&aggregation.perSeriesAligner=ALIGN_MEAN"





# 
# Option 2: Daily Averages (86400s)

export START_TIME=$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ)
export END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22cloudsql.googleapis.com%2Fdatabase%2Fcpu%2Futilization%22%20AND%20resource.labels.database_id%3D%22${PROJECT_ID}%3A${INSTANCE_ID}%22\
&interval.startTime=${START_TIME}\
&interval.endTime=${END_TIME}\
&aggregation.alignmentPeriod=86400s\
&aggregation.perSeriesAligner=ALIGN_MEAN" | jq '.timeSeries[0].points[] | {Time: .interval.endTime, CPU_Percent: ((.value.doubleValue * 100 * 100 | round) / 100)}'

