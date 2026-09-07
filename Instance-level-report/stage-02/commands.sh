pip3 install google-api-python-client google-auth

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

export PROJECT_ID="data-analyst-504209"
export INSTANCE_ID="csql-mysql-db"

curl -s -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries:query" \
  -d '{
    "query": "fetch cloudsql_database | metric '\''cloudsql.googleapis.com/database/cpu/utilization'\'' | filter (resource.database_id == '\'${PROJECT_ID}:${INSTANCE_ID}\'') | within 12h | group_by [], [mean_val: mean(value.utilization), max_val: max(value.utilization), p95: percentile(value.utilization, 95), p99: percentile(value.utilization, 99)] | every 24h"
  }'