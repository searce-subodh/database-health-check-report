-- 1. Buffer Pool Hit Ratio (Delivered over the last 24 hours via Sys Schema)
SELECT variable_name, variable_value FROM sys.metrics WHERE variable_name IN ('buffer_pool_reads', 'buffer_pool_read_requests');

-- Note: MySQL internal cumulative counter deltas over 24 hours require Performance Schema Digest/Statement History.

-- 2. Slow Queries Executed in the Last 24 Hours
SELECT start_time,user_host,query_time,lock_time,rows_sent,rows_examined,db,sql_text FROM mysql.slow_log WHERE start_time >= NOW() - INTERVAL 1 DAY;

-- 3. Historical Lock Waits Occurred in the Last 24 Hours
SELECT event_id,timer_start / 1000000000000 AS start_time_sec,timer_wait / 1000000000000 AS wait_time_sec,object_schema,object_name,index_name, lock_status FROM performance_schema.events_waits_history_long WHERE event_name LIKE 'wait/synch/lock/innodb%' AND timer_start >= ((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME = 'UPTIME') * 1000000000000  - (86400 * 1000000000000));

-- 4. Connection Errors and Aborted Attempts in the Last 24 Hours
SELECT sum_connect_errors FROM performance_schema.host_cache WHERE last_seen >= NOW() - INTERVAL 1 DAY;

-- 5. Queries Executed & Processed in the Last 24 Hours (Grouped by Query Pattern)
SELECT schema_name,digest_text,count_star AS exec_count,sum_timer_wait / 1000000000000 AS total_latency_sec,sum_lock_time / 1000000000000 AS total_lock_time_sec,sum_rows_examined,sum_rows_sent,first_seen,last_seen FROM performance_schema.events_statements_summary_by_digest WHERE last_seen >= NOW() - INTERVAL 1 DAY ORDER BY count_star DESC LIMIT 20;

-- 6. Table Operations Activity in the Last 24 Hours (Sys Schema Delta View)
SELECT table_schema,table_name,rows_fetched,rows_inserted,rows_updated,rows_deleted FROM sys.schema_table_statistics WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys');