
-- **1. Memory & Buffer Pool Management**

-- (need to be worked)
-- Buffer Pool Hit Ratio Raw Counters 
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Innodb_buffer_pool_read_requests', 'Innodb_buffer_pool_reads');

-- Dirty Pages
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Innodb_buffer_pool_pages_dirty', 'Innodb_buffer_pool_pages_total');



-- **2. Connections & Thread Activity**

--[Looks Fine]
-- Connection Status vs Max Limit
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Threads_connected', 'Max_used_connections') 
UNION ALL 
SELECT variable_name, variable_value 
FROM performance_schema.global_variables 
WHERE variable_name = 'max_connections';

-- Thread Activity Breakdown (may be not needed now)
SELECT processlist_state AS state, COUNT(*) AS thread_count 
FROM performance_schema.threads 
WHERE processlist_state IS NOT NULL 
GROUP BY processlist_state 
ORDER BY thread_count DESC;

-- Aborted Connections --[Looks Fine]
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Aborted_connects', 'Aborted_clients');



-- **3. Redo Log & Transactions**

-- (need to be worked)
-- Redo Log Activity
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Innodb_os_log_written', 'Innodb_log_waits', 'Innodb_log_write_requests');



-- **4. Locks & Contention**


-- Active Lock Waits
SELECT waiting_thread_id AS waiting_thread, waiting_query, blocking_thread_id AS blocking_thread, blocking_query, wait_age_secs 
FROM sys.innodb_lock_waits;



-- **5. Storage & Query Optimization**


-- Table Fragmentation (we have better query)
SELECT table_schema AS database_name, table_name, engine, 
       ROUND(data_length / 1024 / 1024, 2) AS data_size_mb, 
       ROUND(data_free / 1024 / 1024, 2) AS data_free_mb, 
       ROUND((data_free / NULLIF(data_length + index_length + data_free, 0)) * 100, 2) AS fragmentation_pct 
FROM information_schema.tables 
WHERE table_schema = 'application' AND data_free > 0 
ORDER BY data_free_mb DESC LIMIT 10;
 
-- Temporary Table Usage (need to work)
SELECT variable_name, variable_value 
FROM performance_schema.global_status 
WHERE variable_name IN ('Created_tmp_tables', 'Created_tmp_disk_tables');

-- Unused Indexes
SELECT sui.object_schema AS database_name, sui.object_name AS table_name, sui.index_name, 
       ROUND((iis.stat_value * @@innodb_page_size) / 1024 / 1024, 2) AS index_size_mb 
FROM sys.schema_unused_indexes sui 
JOIN mysql.innodb_index_stats iis ON sui.index_name = iis.index_name 
WHERE sui.object_schema = 'application' 
ORDER BY index_size_mb DESC LIMIT 10;

-- Redundant Indexes
SELECT table_schema AS database_name, table_name, redundant_index_name, 
       redundant_index_columns, dominant_index_name, dominant_index_columns, subpart_exists 
FROM sys.schema_redundant_indexes 
WHERE table_schema = 'application';

-- High Full Table Scan Queries
SELECT DIGEST_TEXT AS full_query, COUNT_STAR AS exec_count, 
       SUM_NO_INDEX_USED AS no_index_used_count, SUM_NO_GOOD_INDEX_USED AS no_good_index_used_count, 
       ROUND(SUM_TIMER_WAIT / 1000000000000, 2) AS total_latency_sec, 
       ROUND(SUM_ROWS_EXAMINED / COUNT_STAR, 0) AS rows_examined_avg 
FROM performance_schema.events_statements_summary_by_digest 
WHERE SUM_NO_INDEX_USED > 0 OR SUM_NO_GOOD_INDEX_USED > 0 
ORDER BY COUNT_STAR DESC LIMIT 10;

-- Low Selectivity Indexes
SELECT s.TABLE_SCHEMA AS database_name, s.TABLE_NAME, s.INDEX_NAME, s.COLUMN_NAME, 
       s.CARDINALITY, t.TABLE_ROWS, 
       ROUND((s.CARDINALITY / NULLIF(t.TABLE_ROWS, 0)) * 100, 2) AS selectivity_pct 
FROM INFORMATION_SCHEMA.STATISTICS s 
JOIN INFORMATION_SCHEMA.TABLES t ON s.TABLE_SCHEMA = t.TABLE_SCHEMA AND s.TABLE_NAME = t.TABLE_NAME 
WHERE s.TABLE_SCHEMA = 'application' AND s.INDEX_NAME != 'PRIMARY' AND t.TABLE_ROWS > 10000 
HAVING selectivity_pct < 5.00 
ORDER BY selectivity_pct ASC LIMIT 10;



-- **6. User Security & Password Hygiene**



