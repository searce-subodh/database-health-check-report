-- =============================
-- INDEX
-- =============================

-- 1. Unused Indexes
-- Indexes speed up read operations but slow down INSERT, UPDATE, and DELETE operations while consuming storage. This check identifies indexes that have been created but are never used by the query optimizer to fetch data. (Note: For PostgreSQL, we intentionally exclude Unique/PK indexes as they are required to enforce data integrity even if not scanned).

-- MySQL Query
-- Prerequisites / Privileges Required: Requires PERFORMANCE_SCHEMA : enabled, SELECT access to the sys schema.

-- (Find if there are any other queries)
SELECT 
    object_schema AS "Database Name", 
    object_name AS "Table Name", 
    index_name AS "Index Name"
FROM sys.schema_unused_indexes
WHERE object_schema NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
LIMIT 15;

-- +----------------+-----------+----------------------+
-- | Database       | Table     | Unused Index Name    |
-- +----------------+-----------+----------------------+
-- | test_unused_db | employees | idx_hire_date_unused |
-- +----------------+-----------+----------------------+
-- PostgreSQL Query
-- Prerequisites / Extensions Required: None (Uses native pg_stat_user_indexes and pg_index).


SELECT 
    schemaname AS "Schema Name", 
    relname AS "Table Name", 
    indexrelname AS "Index Name", 
    pg_size_pretty(pg_relation_size(indexrelid)) AS "Index Size", 
    idx_scan AS "Total Scans"
FROM pg_stat_user_indexes 
JOIN pg_index USING (indexrelid)
WHERE idx_scan = 0 
  AND indisunique IS FALSE 
  AND schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 15;
-- Production Safety & Risks
-- Safe to run anytime. These rely on in-memory cumulative statistics counters. Be aware that statistics reset on database restart (or statistics reset commands); ensure the database has been running for a representative amount of time before dropping "unused" indexes.


--  Schema Name | Table Name  |    Index Name     | Index Size | Total Scans 
-- -------------+-------------+-------------------+------------+-------------
--  public      | stress_test | idx_stress_status | 1336 kB    |           0
--  public      | orders      | idx_unused_notes  | 1144 kB    |           0
--  public      | orders      | idx_cust_1        | 712 kB     |           0
--  public      | orders      | idx_cust_2        | 712 kB     |           0
-- (4 rows)





-- 2. Duplicate
-- Duplicate  indexes (where one index covers the exact same columns ) waste significant disk space and unnecessarily increase transaction overhead.

-- MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT access to the sys schema.

SELECT 
    table_schema AS "Database Name", 
    table_name AS "Table Name", 
    redundant_index_name AS "Index 1 (Redundant)", 
    dominant_index_name AS "Index 2 (Dominant)", 
    dominant_index_columns AS "Index Columns"
FROM sys.schema_redundant_indexes WHERE table_schema NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
  AND redundant_index_columns = dominant_index_columns
LIMIT 15;


-- +---------------+------------+---------------------+--------------------+---------------+
-- | Database Name | Table Name | Index 1 (Redundant) | Index 2 (Dominant) | Index Columns |
-- +---------------+------------+---------------------+--------------------+---------------+
-- | hr_db         | employees  | idx_emp_dept_v2     | idx_emp_dept       | dept_id       |
-- | test_index_db | user_logs  | idx_user_b          | idx_user_a         | user_id       |
-- +---------------+------------+---------------------+--------------------+---------------+


-- IF PERFORMANCE_SCHEMA=ON

SELECT 
    sri.table_schema AS "Database Name", 
    sri.table_name AS "Table Name", 
    sri.redundant_index_columns AS "Indexed Columns",
    sri.redundant_index_name AS "Index 1 (Redundant)", 
    COALESCE(u1.COUNT_STAR, 0) AS "Idx 1 Usage Count",
    sri.dominant_index_name AS "Index 2 (Dominant)", 
    COALESCE(u2.COUNT_STAR, 0) AS "Idx 2 Usage Count",
    COALESCE(
        CONCAT(
            ROUND(
                (COALESCE(u1.COUNT_STAR, 0) / 
                NULLIF(COALESCE(u1.COUNT_STAR, 0) + COALESCE(u2.COUNT_STAR, 0), 0)) * 100, 
            1), 
        '%'), 
    'No Usage Yet') AS "Idx 1 Usage %"
FROM sys.schema_redundant_indexes sri
LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage u1 
    ON sri.table_schema = u1.OBJECT_SCHEMA 
    AND sri.table_name = u1.OBJECT_NAME 
    AND sri.redundant_index_name = u1.INDEX_NAME
LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage u2 
    ON sri.table_schema = u2.OBJECT_SCHEMA 
    AND sri.table_name = u2.OBJECT_NAME 
    AND sri.dominant_index_name = u2.INDEX_NAME
WHERE sri.table_schema NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
  AND sri.redundant_index_columns = sri.dominant_index_columns
ORDER BY (COALESCE(u1.COUNT_STAR, 0) + COALESCE(u2.COUNT_STAR, 0)) DESC
LIMIT 15;

-- TESTED OUTPUT
-- +---------------+------------+-----------------+---------------------+-------------------+--------------------+-------------------+---------------+
-- | Database Name | Table Name | Indexed Columns | Index 1 (Redundant) | Idx 1 Usage Count | Index 2 (Dominant) | Idx 2 Usage Count | Idx 1 Usage % |
-- +---------------+------------+-----------------+---------------------+-------------------+--------------------+-------------------+---------------+
-- | test_index_db | user_logs  | user_id         | idx_user_b          |                 3 | idx_user_a         |                 6 | 33.3%         |
-- | hr_db         | employees  | dept_id         | idx_emp_dept_v2     |                 0 | idx_emp_dept       |                 0 | No Usage Yet  |
-- +---------------+------------+-----------------+---------------------+-------------------+--------------------+-------------------+---------------+


-- PostgreSQL Query
-- Prerequisites / Extensions Required: None (Uses native system catalogs).

SELECT                           
    p1.schemaname AS "Schema Name",
    p1.tablename AS "Table Name",
    p1.indexname AS "Index 1 Name",
    COALESCE(s1.idx_scan, 0) AS "Idx 1 Usage Count",
    p2.indexname AS "Index 2 Name",
    COALESCE(s2.idx_scan, 0) AS "Idx 2 Usage Count",
    CASE 
        WHEN COALESCE(s1.idx_scan, 0) + COALESCE(s2.idx_scan, 0) = 0 THEN 'No Usage Yet'
        ELSE ROUND((COALESCE(s1.idx_scan, 0)::numeric / (COALESCE(s1.idx_scan, 0) + COALESCE(s2.idx_scan, 0))) * 100, 1)::text || '%'
    END AS "Idx 1 Usage %",
    substring(p1.indexdef from ' USING ([a-zA-Z0-9_]+) ') AS "Index Type",
    substring(p1.indexdef from '\((.*)\)') AS "Indexed Columns"
FROM pg_indexes p1 
JOIN pg_indexes p2 
  ON p1.schemaname = p2.schemaname 
 AND p1.tablename = p2.tablename 
 AND p1.indexname < p2.indexname
 AND substring(p1.indexdef from '\((.*)\)') = substring(p2.indexdef from '\((.*)\)')
 AND substring(p1.indexdef from ' USING ([a-zA-Z0-9_]+) ') = substring(p2.indexdef from ' USING ([a-zA-Z0-9_]+) ')
LEFT JOIN pg_stat_user_indexes s1 
  ON p1.schemaname = s1.schemaname 
 AND p1.tablename = s1.relname 
 AND p1.indexname = s1.indexrelname
LEFT JOIN pg_stat_user_indexes s2 
  ON p2.schemaname = s2.schemaname 
 AND p2.tablename = s2.relname 
 AND p2.indexname = s2.indexrelname
WHERE p1.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY (COALESCE(s1.idx_scan, 0) + COALESCE(s2.idx_scan, 0)) DESC, p1.tablename;


-- Schema Name | Table Name |     Index 1 Name      | Idx 1 Usage Count |         Index 2 Name         | Idx 2 Usage Count | Idx 1 Usage % | Index Type |   Indexed Columns    
-- -------------+------------+-----------------------+-------------------+------------------------------+-------------------+---------------+------------+----------------------
--  public      | customers  | idx_customers_country |                 0 | idx_customers_country_lowsel |                 2 | 0.0%          | btree      | country
--  public      | index_test | idx_cat_standard_1    |                 0 | idx_cat_standard_2           |                 0 | No Usage Yet  | btree      | category_id
--  public      | index_test | idx_func_1            |                 0 | idx_func_2                   |                 0 | No Usage Yet  | btree      | lower((email)::text)
--  public      | products   | idx_products_stock    |                 0 | idx_products_stock_unused    |                 0 | No Usage Yet  | btree      | stock
-- (4 rows)


-- Production Safety & Risks
-- Safe to run anytime. These queries only read metadata. The PostgreSQL query specifically looks for exact duplicates (same columns, predicates, and expressions).



-- 
-- 3. High Volume Full Table Scans
-- 
-- Queries causing full table scans indicate missing indexes, out-of-date table statistics, or non-sargable query predicates. This check flags the tables generating the highest disk I/O due to sequential/full table scans.

-- MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT access to the sys schema.


SELECT 
    db AS "Database Name", 
    IFNULL(query, 'Unknown') AS "Query Snippet", 
    exec_count AS "Execution Count", 
    sys.format_time(total_latency) AS "Total Latency", 
    rows_examined_avg AS "Avg Rows Examined"
FROM sys.statements_with_full_table_scans
WHERE db NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
ORDER BY total_latency DESC
LIMIT 15;


-- +----------------+-------------------------------------------------------------------+-----------------+---------------+-------------------+
-- | Database Name  | Query Snippet                                                     | Execution Count | Total Latency | Avg Rows Examined |
-- +----------------+-------------------------------------------------------------------+-----------------+---------------+-------------------+
-- | test_index_db  | SELECT `sri` . `table_schema`  ...  . `redundant_index_columns` = |               1 | 9.43 ms ps    |               366 |
-- | test_unused_db | SELECT `object_schema` AS ? ,  ... xes` WHERE `object_schema` = ? |               1 | 8.53 ms ps    |               249 |
-- | test_unused_db | SELECT `table_schema` AS ? , T ... ominant_index_columns` LIMIT ? |               1 | 8.08 ms ps    |                 2 |
-- | test_unused_db | SELECT * FROM `employees` WHERE NAME = ?                          |               1 | 676.21 us ps  |                 4 |
-- | test_unused_db | SELECT * FROM `sys` . `schema_unused_indexes`                     |               1 | 12.69 ms ps   |              4088 |
-- +----------------+-------------------------------------------------------------------+-----------------+---------------+-------------------+


--  PostgreSQL Query
-- Prerequisites / Extensions Required: None (relies on pg_stat_user_tables).

  -- Ignore tiny tables (under 1MB) because Seq Scans are normal for them

SELECT 
    schemaname AS "Schema Name", 
    relname AS "Table Name", 
    seq_scan AS "Total Full Scans", 
    seq_tup_read AS "Total Rows Read", 
    ROUND(seq_tup_read::numeric / NULLIF(seq_scan, 0), 0) AS "Avg Rows Per Scan",
    pg_size_pretty(pg_table_size(relid)) AS "Table Size"
FROM pg_stat_user_tables
WHERE seq_scan > 0 
  AND pg_table_size(relid) > 1048576 
  AND schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY seq_tup_read DESC
LIMIT 15;



--  Schema Name |    Table Name    | Total Full Scans | Total Rows Read | Avg Rows Per Scan | Table Size 
-- -------------+------------------+------------------+-----------------+-------------------+------------
--  public      | stress_test      |               45 |         1880000 |             41778 | 64 MB
--  public      | orders           |               15 |          700000 |             46667 | 36 MB
--  public      | fragmented_table |                3 |           30000 |             10000 | 10040 kB

-- Production Safety & Risks
-- Safe to run anytime. It queries performance metadata. For MySQL, it identifies the exact query snippet causing the scan. For PostgreSQL, we identify the exact tables experiencing the highest volume of scanned rows, as extracting raw query plans requires log parsing or the auto_explain module.


-- =============================
-- PERFORMANCE
-- =============================

-- 📊 1. Buffer Pool / Cache Hit Ratio
-- The Buffer Pool (MySQL) or Shared Buffers (PostgreSQL) is the memory space used to cache table and index data. A hit ratio below 95% generally indicates the database is reading too frequently from disk (I/O bound) rather than memory, which can severely degrade performance.

-- 🐬 MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT on information_schema.


-- IF (performance_schema)

-- (NOT PRESENT ON DATABASE LEVEL, PRESENT ON INSTANCE LEVEL)
SELECT 
    'InnoDB Buffer Pool' AS "Component",
    CAST(SUM(variable_value) AS UNSIGNED) AS "Total Read Requests",
    CAST(SUM(CASE WHEN variable_name = 'INNODB_BUFFER_POOL_READS' THEN variable_value ELSE 0 END) AS UNSIGNED) AS "Physical Disk Reads",
    ROUND(
        (1 - (
            SUM(CASE WHEN variable_name = 'INNODB_BUFFER_POOL_READS' THEN variable_value ELSE 0 END) / 
            NULLIF(SUM(CASE WHEN variable_name = 'INNODB_BUFFER_POOL_READ_REQUESTS' THEN variable_value ELSE 0 END), 0)
        )) * 100, 2
    ) AS "Hit Ratio (%)"
FROM performance_schema.global_status 
WHERE variable_name IN ('INNODB_BUFFER_POOL_READ_REQUESTS', 'INNODB_BUFFER_POOL_READS');


-- +--------------------+---------------------+---------------------+---------------+
-- | Component          | Total Read Requests | Physical Disk Reads | Hit Ratio (%) |
-- +--------------------+---------------------+---------------------+---------------+
-- | InnoDB Buffer Pool |              649717 |                1188 |         99.82 |
-- +--------------------+---------------------+---------------------+---------------+

-- 🐘 PostgreSQL Query
-- Prerequisites / Extensions Required: None.

SELECT                         
    datname AS "Database Name",
    blks_hit AS "Cache Hits",
    blks_read AS "Disk Reads",
    ROUND(
        (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) * 100, 2
    ) AS "Hit Ratio (%)"
FROM pg_stat_database
WHERE datname NOT LIKE 'template%' 
ORDER BY "Hit Ratio (%)" ASC
LIMIT 15;


--    Database Name   | Cache Hits | Disk Reads | Hit Ratio (%) 
-- -------------------+------------+------------+---------------
--  postgres          |   98399644 |      18007 |         99.98
--  sbtest            |  103124320 |      15345 |         99.99
--  cloudsqladmin     |  211783155 |       7005 |        100.00
--  autovacuum_poc_db |   94804839 |       3826 |        100.00
--  test_autovacuum   |   94423126 |       3821 |        100.00
--  application       |   71911478 |       2459 |        100.00



-- ⚠️ Production Safety & Risks
-- Safe to run anytime. These query aggregate statistics counters that are kept entirely in memory.



-- 📊 2. Dirty Pages Percentage
-- "Dirty pages" are blocks of data in memory that have been modified but not yet flushed to disk. A consistently high percentage of dirty pages means the background flushing process cannot keep up with write volume, risking severe latency spikes during aggressive checkpointing.

-- 🐬 MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT on information_schema.

-- (NOT PRESENT ON DATABASE LEVEL, PRESENT ON INSTANCE LEVEL)

SELECT 
    POOL_ID AS "Buffer Pool ID",
    CONCAT(ROUND(POOL_SIZE * 16384 / 1024 / 1024, 2), ' MB') AS "Pool Size",
    MODIFIED_DATABASE_PAGES AS "Dirty Pages",
    ROUND((MODIFIED_DATABASE_PAGES / NULLIF(POOL_SIZE, 0)) * 100, 2) AS "Dirty Pages (%)"
FROM information_schema.innodb_buffer_pool_stats
ORDER BY "Dirty Pages (%)" DESC;


-- +----------------+------------+-------------+-----------------+
-- | Buffer Pool ID | Pool Size  | Dirty Pages | Dirty Pages (%) |
-- +----------------+------------+-------------+-----------------+
-- |              0 | 1407.88 MB |           0 |            0.00 |
-- +----------------+------------+-------------+-----------------+

-- 🐘 PostgreSQL Query
-- Prerequisites / Extensions Required: None (Uses pg_stat_bgwriter to analyze flushing behavior over time, as exact point-in-time dirty page counts require the heavy pg_buffercache extension).

SELECT 
    datname AS "Database Name",
    blks_hit AS "Buffer Hits (RAM Reads)",
    blks_read AS "Disk Reads",    
    ROUND(
        (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) * 100, 
    3) AS "Buffer Hit Ratio (%)"
FROM pg_stat_database;


-- ----------------+------------+-------------+-----------------+
-- | Buffer Pool ID | Pool Size  | Dirty Pages | Dirty Pages (%) |
-- +----------------+------------+-------------+-----------------+
-- |              0 | 1407.88 MB |           0 |            0.00 |
-- +----------------+------------+-------------+-----------------+

-- ⚠️ Production Safety & Risks
-- Safe to run anytime. Uses lightweight system catalog views.

-- 
-- -- -- 📊 3. Redo Log / WAL Generation & Contention
-- -- The Redo Log (MySQL) or Write-Ahead Log / WAL (PostgreSQL) ensures crash recovery. High wait times or massive generation volumes indicate write-heavy bottlenecks.

-- -- 🐬 MySQL Query
-- -- Prerequisites / Privileges Required: Requires SELECT on information_schema.

-- SELECT 
--     NAME AS "Metric Name",
--     COUNT AS "Event Count",
--     CONCAT(ROUND(COUNT / 1024 / 1024, 2), ' MB') AS "Volume (if bytes)",
--     COMMENT AS "Description"
-- FROM information_schema.innodb_metrics 
-- WHERE name IN ('innodb_os_log_written', 'log_waits', 'log_write_requests')
-- ORDER BY name;


-- -- 🐘 PostgreSQL Query
-- -- Prerequisites / Extensions Required: None (Requires PG 14+).


-- SELECT 
--     wal_records AS "WAL Records Generated",
--     pg_size_pretty(wal_bytes) AS "Total WAL Volume",
--     wal_buffers_full AS "WAL Buffers Full (Contention)",
--     wal_write_time AS "Time Spent Writing (ms)",
--     wal_sync_time AS "Time Spent Syncing (ms)"
-- FROM pg_stat_wal;

-- -- ⚠️ Production Safety & Risks
-- -- Safe to run anytime.



-- 📊 4. Temporary Table / Disk Spill Usage
-- When queries execute complex GROUP BY, ORDER BY, or JOIN operations that exceed tmp_table_size (MySQL) or work_mem (PostgreSQL), they "spill" to disk. Disk-based temporary tables cause massive I/O degradation.

-- 🐬 MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT on sys.

SELECT 
    db AS "Database Name",
    IFNULL(query, 'Unknown') AS "Query Snippet",
    exec_count AS "Execution Count",
    memory_tmp_tables AS "Memory Temp Tables",
    disk_tmp_tables AS "Disk Temp Tables",
    tmp_tables_to_disk_pct AS "Disk Spill Ratio (%)"
FROM sys.statements_with_temp_tables
WHERE db NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
ORDER BY disk_tmp_tables DESC
LIMIT 15;


-- +----------------+-------------------------------------------------------------------+-----------------+--------------------+------------------+----------------------+
-- | Database Name  | Query Snippet                                                     | Execution Count | Memory Temp Tables | Disk Temp Tables | Disk Spill Ratio (%) |
-- +----------------+-------------------------------------------------------------------+-----------------+--------------------+------------------+----------------------+
-- | test_index_db  | SELECT `sri` . `table_schema`  ...  . `redundant_index_columns` = |               1 |                  5 |                0 |                    0 |
-- | test_unused_db | SELECT `table_schema` AS ? , T ... ominant_index_columns` LIMIT ? |               1 |                  5 |                0 |                    0 |
-- | test_unused_db | SELECT `POOL_ID` AS ? , `CONCA ... er_pool_stats` ORDER BY ? DESC |               1 |                  1 |                0 |                    0 |
-- | test_unused_db | SELECT NAME AS ? , `COUNT` AS  ... RE NAME IN (...) ORDER BY NAME |               2 |                  2 |                0 |                    0 |
-- +----------------+-------------------------------------------------------------------+-----------------+--------------------+------------------+----------------------+


-- 🐘 PostgreSQL Query
-- Prerequisites / Extensions Required: None.

SELECT 
    datname AS "Database Name",
    temp_files AS "Temp Files Created",
    pg_size_pretty(temp_bytes) AS "Total Disk Spill Vol",
    ROUND(temp_bytes::numeric / NULLIF(temp_files, 0), 2) AS "Avg Spill Size (Bytes)"
FROM pg_stat_database
WHERE temp_bytes > 0
ORDER BY temp_bytes DESC
LIMIT 15;

--  Database Name | Temp Files Created | Total Disk Spill Vol | Avg Spill Size (Bytes) 
-- ---------------+--------------------+----------------------+------------------------
--  postgres      |                 25 | 189 MB               |             7920363.52
--  sbtest        |                  8 | 7872 kB              |             1007616.00



-- IF (pg_stat_statements is enabled)


SELECT 
    d.datname AS "Database Name",
    substring(s.query, 1, 100) || '...' AS "Query Snippet",
    s.calls AS "Execution Count",
    ROUND((s.temp_blks_written * 8.0) / 1024.0, 2) AS "Total Disk Temp Written (MB)",
    ROUND((s.temp_blks_read * 8.0) / 1024.0, 2) AS "Total Disk Temp Read (MB)",
    ROUND(((s.temp_blks_written + s.temp_blks_read) * 8.0) / 1024.0 / s.calls, 2) AS "Avg Temp Data Per Call (MB)"
FROM pg_stat_statements s
JOIN pg_database d ON s.dbid = d.oid
WHERE s.temp_blks_written > 0 
  AND d.datname NOT LIKE 'template%'
ORDER BY s.temp_blks_written DESC
LIMIT 15;


-- Database Name |                              Query Snippet                              | Execution Count | Total Disk Temp Written (MB) | Total Disk Temp Read (MB) | Avg Temp Data Per Call (MB) 
-- ---------------+-------------------------------------------------------------------------+-----------------+------------------------------+---------------------------+-----------------------------
--  postgres      | CALL generate_healthcheck_traffic()...                                  |               8 |                       189.90 |                    185.47 |                       46.92
--  postgres      | CREATE INDEX idx_unused_notes ON orders(notes)...                       |               1 |                        29.44 |                     29.45 |                       58.89
--  postgres      | SELECT                                                                 +|               1 |                         3.34 |                      3.34 |                        6.67
--                |     $1 AS "Buffer Pool ID",                                            +|                 |                              |                           | 
--                |                                                                        +|                 |                              |                           | 
--                |     -- Calculate total pool size in Megabytes (Blocks * Blo...          |                 |                              |                           | 
--  postgres      | select * from pg_buffercache...                                         |               1 |                         3.34 |                      3.34 |                        6.67


-- ⚠️ Production Safety & Risks
-- Safe to run anytime. Extremely useful for identifying queries that need index optimization or session-level memory tuning.

-- 📊 5. Top Queries by Execution Time
-- Identifies the exact queries consuming the most cumulative time in your database, making them the primary targets for query refactoring or indexing.

-- 🐬 MySQL Query
-- Prerequisites / Privileges Required: Requires SELECT on sys.

SELECT 
    db AS "Database Name",
    IFNULL(query, 'Unknown') AS "Query Snippet",
    exec_count AS "Execution Count",
    sys.format_time(total_latency) AS "Total Latency",
    sys.format_time(avg_latency) AS "Avg Latency",
    sys.format_time(max_latency) AS "Max Latency"
FROM sys.statement_analysis
WHERE db NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
ORDER BY total_latency DESC
LIMIT 15;


-- +----------------+-------------------------------------------------------------------+-----------------+---------------+--------------+--------------+
-- | Database Name  | Query Snippet                                                     | Execution Count | Total Latency | Avg Latency  | Max Latency  |
-- +----------------+-------------------------------------------------------------------+-----------------+---------------+--------------+--------------+
-- | test_index_db  | SHOW SCHEMAS                                                      |               1 | 951.76 us ps  | 951.76 us ps | 951.76 us ps |
-- | test_index_db  | SELECT `sri` . `table_schema`  ...  . `redundant_index_columns` = |               1 | 9.43 ms ps    | 9.43 ms ps   | 9.43 ms ps   |
-- | test_unused_db | SELECT `db` AS ? , `IFNULL` (  ... Y `total_latency` DESC LIMIT ? |               1 | 9.35 ms ps    | 9.35 ms ps   | 9.35 ms ps   |
-- | test_unused_db | SELECT `db` AS ? , `IFNULL` (  ... `disk_tmp_tables` DESC LIMIT ? |               1 | 894.55 us ps  | 894.55 us ps | 894.55 us ps |
-- | test_index_db  | DROP TABLE IF EXISTS `user_logs`                                  |               1 | 859.71 us ps  | 859.71 us ps | 859.71 us ps |
-- | test_unused_db | SELECT `object_schema` AS ? ,  ... xes` WHERE `object_schema` = ? |               1 | 8.53 ms ps    | 8.53 ms ps   | 8.53 ms ps   |
-- | test_unused_db | SELECT `table_schema` AS ? , T ... ominant_index_columns` LIMIT ? |               1 | 8.08 ms ps    | 8.08 ms ps   | 8.08 ms ps   |
-- | test_unused_db | SELECT `db` AS ? , `IFNULL` (  ... `tmp_disk_tables` DESC LIMIT ? |               1 | 767.33 us ps  | 767.33 us ps | 767.33 us ps |
-- | test_unused_db | CREATE TABLE `employees` ( `em ... date_unused` ( `hire_date` ) ) |               1 | 74.00 ms ps   | 74.00 ms ps  | 74.00 ms ps  |
-- | test_unused_db | SELECT * FROM `employees` WHERE NAME = ?                          |               1 | 676.21 us ps  | 676.21 us ps | 676.21 us ps |
-- | test_unused_db | SELECT NAME AS ? , `COUNT` AS  ... RE NAME IN (...) ORDER BY NAME |               2 | 6.98 ms ps    | 3.49 ms ps   | 5.49 ms ps   |
-- | test_unused_db | INSERT INTO `employees` ( NAME ... te` ) VALUES (...) /* , ... */ |               1 | 6.96 ms ps    | 6.96 ms ps   | 6.96 ms ps   |
-- | test_unused_db | SELECT * FROM `sys` . `statements_with_temp_tables`               |               1 | 6.86 ms ps    | 6.86 ms ps   | 6.86 ms ps   |
-- | test_unused_db | SELECT `db` AS ? , `IFNULL` (  ... `disk_tmp_tables` DESC LIMIT ? |               1 | 6.04 ms ps    | 6.04 ms ps   | 6.04 ms ps   |
-- | test_index_db  | SELECT * FROM `user_logs` FORC ... _user_b` ) WHERE `user_id` = ? |               1 | 549.45 us ps  | 549.45 us ps | 549.45 us ps |
-- +----------------+-------------------------------------------------------------------+-----------------+---------------+--------------+--------------+


-- 🐘 PostgreSQL Query
-- Prerequisites / Extensions Required: Requires pg_stat_statements extension enabled. (Standard in GCP Cloud SQL).

-- IF (pg_stat_statements)

SELECT 
    (SELECT datname FROM pg_database WHERE oid = dbid) AS "Database Name",
    SUBSTRING(query FROM 1 FOR 80) AS "Query Snippet",
    calls AS "Execution Count",
    ROUND(total_exec_time::numeric, 2) AS "Total Time (ms)",
    ROUND(mean_exec_time::numeric, 2) AS "Avg Time (ms)",
    ROUND(max_exec_time::numeric, 2) AS "Max Time (ms)"
FROM pg_stat_statements
WHERE (SELECT datname FROM pg_database WHERE oid = dbid) NOT LIKE 'template%'
ORDER BY total_exec_time DESC
LIMIT 15;

--   Database Name   |                                  Query Snippet                                   | Execution Count | Total Time (ms) | Avg Time (ms) | Max Time (ms) 
-- -------------------+----------------------------------------------------------------------------------+-----------------+-----------------+---------------+---------------
--  cloudsqladmin     | SELECT setting, current_timestamp FROM pg_catalog.pg_settings WHERE name = $1    |          630376 |      2485481.85 |          3.94 |         81.83
--  postgres          | SELECT default_version, installed_version FROM pg_catalog.pg_available_extension |           59287 |      1648878.70 |         27.81 |        313.75
--  sbtest            | SELECT default_version, installed_version FROM pg_catalog.pg_available_extension |           59035 |      1310878.90 |         22.21 |        104.94
--  autovacuum_poc_db | SELECT default_version, installed_version FROM pg_catalog.pg_available_extension |           59199 |      1249391.25 |         21.10 |        142.94
--  test_autovacuum   | SELECT default_version, installed_version FROM pg_catalog.pg_available_extension |           59075 |      1202951.84 |         20.36 |        158.42
--  cloudsqladmin     | SELECT default_version, installed_version FROM pg_catalog.pg_available_extension |           63357 |      1194250.76 |         18.85 |        383.47
--  cloudsqladmin     | SELECT d.datname, pg_catalog.pg_database_size(d.datname), current_timestamp FROM |           59287 |      1112029.74 |         18.76 |        143.08
--  appl

-- ⚠️ Production Safety & Risks
-- Safe to run anytime. Both envi÷ronments use lightweight sampling and aggregation to avoid overhead.




-- =======================
-- LOCKS
-- =======================

-- ### 📊 1. Active Lock Contention (The Blocking Tree)

-- Identifies queries that are currently stuck waiting to acquire a row or table lock, and maps them to the exact session holding that lock. This is critical for resolving immediate application stalls and determining which connection to kill.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on the `sys` schema.

SELECT 
    wait_age AS "Wait Duration",
    locked_table AS "Locked Table",
    waiting_pid AS "Blocked Process ID",
    waiting_query AS "Blocked Query",
    blocking_pid AS "Blocking Process ID",
    blocking_query AS "Blocking Query",
    sql_kill_blocking_connection AS "Quick Fix (Kill Command)"
FROM sys.innodb_lock_waits
WHERE locked_table_schema NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
ORDER BY wait_age DESC
LIMIT 15;


-- #### 🐘 PostgreSQL Query
-- **Prerequisites / Extensions Required:** None (Cross-references `pg_locks` and `pg_stat_activity`).

SELECT 
    blocked.pid AS "Blocked Process ID",
    -- Calculate how long the query has been waiting
    ROUND(EXTRACT(EPOCH FROM (now() - blocked.query_start))::numeric, 2)::text || ' seconds' AS "Wait Duration",
    blocked.query AS "Blocked Query",
    blocking.pid AS "Blocking Process ID",
    blocking.query AS "Blocking Query",
    -- Generate the Postgres command to kill the blocking query
    'SELECT pg_terminate_backend(' || blocking.pid || ');' AS "Quick Fix (Kill Command)"
FROM pg_stat_activity blocked
-- This built-in function safely finds the exact PIDs blocking our query
JOIN unnest(pg_blocking_pids(blocked.pid)) blocking_pid ON true
JOIN pg_stat_activity blocking ON blocking.pid = blocking_pid
WHERE blocked.wait_event_type = 'Lock'
ORDER BY now() - blocked.query_start DESC;

-- #### ⚠️ Production Safety & Risks
-- **Safe to run anytime.** These queries only read in-memory lock structures.

---

-- ### 📊 2. Idle in Transaction (Stale Locks)
-- 
-- Transactions left "open" without being committed or rolled back will hold onto row locks indefinitely. This blocks other queries, exhausts connection pools, and prevents the database engine from cleaning up dead rows (vacuum/purge).

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    trx_id AS "Transaction ID",
    trx_mysql_thread_id AS "Connection ID",
    trx_state AS "State",
    TIME_TO_SEC(TIMEDIFF(NOW(), trx_started)) AS "Duration (s)",
    trx_rows_locked AS "Rows Locked",
    IFNULL(trx_query, 'Idle / No Active Query') AS "Last Active Query"
FROM information_schema.innodb_trx
WHERE trx_state = 'RUNNING' 
  AND trx_query IS NULL
  AND TIME_TO_SEC(TIMEDIFF(NOW(), trx_started)) > 10
ORDER BY "Duration (s)" DESC
LIMIT 15;


-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    pid AS "Process ID",
    usename AS "Username",
    application_name AS "Application",
    state AS "State",
    EXTRACT(EPOCH FROM (NOW() - xact_start)) AS "Duration (s)",
    SUBSTRING(query FROM 1 FOR 80) AS "Last Executed Query"
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND pid != pg_backend_pid()
ORDER BY "Duration (s)" DESC
LIMIT 15;

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** It is highly recommended to set up automated alerts for any query returning a "Duration (s)" greater than 60 seconds.

---

-- ### 📊 3. Deadlock Occurrences (Historical Tracking)

-- A deadlock occurs when two transactions hold locks that the other needs, causing the engine to aggressively terminate one of them. This metric tracks how often your application logic is causing these fatal concurrency collisions.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    name AS "Metric Name",
    count AS "Total Occurrences",
    comment AS "Description"
FROM information_schema.innodb_metrics 
WHERE name = 'innodb_deadlocks';


-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.


SELECT 
    datname AS "Database Name",
    deadlocks AS "Total Deadlocks",
    conflicts AS "Recovery Conflicts (Standby Nodes)",
    blks_hit AS "Cache Hits"
FROM pg_stat_database
WHERE datname NOT LIKE 'template%' 
  AND deadlocks > 0
ORDER BY deadlocks DESC
LIMIT 15;


-- #### ⚠️ Production Safety & Risks
-- **Safe to run anytime.** Note that these are cumulative counters. They reset to zero if the database instance is rebooted.

---

-- ### 📊 4. Metadata and Table-Level Locks (DDL Blockers)

-- Heavy operations like `ALTER TABLE`, `TRUNCATE`, or massive index builds require exclusive table-level locks. This query identifies sessions holding or waiting on these schema-level locks, which will block all other reads and writes to the table.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `performance_schema`.

-- [IF performance_schema]

SELECT 
    object_type AS "Object Type",
    object_schema AS "Database Name",
    object_name AS "Table Name",
    lock_type AS "Lock Type",
    lock_status AS "Status",
    owner_thread_id AS "Owning Thread ID"
FROM performance_schema.metadata_locks
WHERE object_schema NOT IN ('performance_schema', 'information_schema', 'mysql', 'sys')
  AND lock_status = 'PENDING'
ORDER BY object_name
LIMIT 15;

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    l.locktype AS "Lock Type",
    c.relname AS "Table Name",
    a.pid AS "Process ID",
    l.mode AS "Lock Mode",
    l.granted AS "Is Granted?",
    SUBSTRING(a.query FROM 1 FOR 80) AS "Query Snippet"
FROM pg_locks l
JOIN pg_stat_activity a ON l.pid = a.pid
JOIN pg_class c ON l.relation = c.oid
WHERE c.relnamespace::regnamespace::text NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND l.locktype = 'relation'
  AND l.mode IN ('AccessExclusiveLock', 'ExclusiveLock', 'ShareLock')
  AND l.granted IS FALSE
ORDER BY l.mode DESC
LIMIT 15;

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Extremely useful for troubleshooting why a database deployment or schema migration is suddenly hanging and causing application downtime.

---




-- ===========================================
-- CONNECTIONS
-- ==========================================

---

-- ### 📊 1. Active Connections Summary (By User, IP, and State)

-- This query groups all current connections to provide a high-level view of who is connecting, from where, and what the connections are currently doing (e.g., executing queries, sleeping, or authenticating).

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `PROCESS` privilege or `SELECT` on `information_schema`.

SELECT 
    user AS "Username", 
    SUBSTRING_INDEX(host, ':', 1) AS "Client IP", 
    command AS "Connection State", 
    COUNT(*) AS "Total Connections" 
FROM information_schema.processlist 
WHERE user NOT IN ('system user', 'event_scheduler', 'rdsadmin', 'cloudsqladmin') 
GROUP BY user, SUBSTRING_INDEX(host, ':', 1), command 
ORDER BY "Total Connections" DESC 
LIMIT 15;


-- +----------+------------+------------------+-------------------+
-- | Username | Client IP  | Connection State | Total Connections |
-- +----------+------------+------------------+-------------------+
-- | root     | 127.0.0.1  | Sleep            |                 5 |
-- | root     | 10.128.0.4 | Query            |                 1 |
-- +----------+------------+------------------+-------------------+

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None (Uses standard `pg_stat_activity`).

SELECT 
    usename AS "Username", 
    client_addr AS "Client IP", 
    state AS "Connection State", 
    COUNT(*) AS "Total Connections" 
FROM pg_stat_activity 
WHERE pid != pg_backend_pid()
  AND usename NOT IN ('cloudsqladmin', 'cloudsqlagent')
GROUP BY usename, client_addr, state 
ORDER BY COUNT(*) DESC 
LIMIT 15;



-- #### ⚠️ Production Safety & Risks
-- 
-- **Safe to run anytime.** These summarize active thread states in memory.

---

-- ### 📊 2. Aborted / Failed Connections

-- High numbers of aborted connections typically indicate network firewalls dropping packets, application connection pools misbehaving (dying without closing), or failed authentication attempts (bad passwords/brute force).

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `performance_schema`.
-- [If PERFORMANCE_SCHEMA]

SELECT 
    user AS "User",
    host AS "Host",
    error_number AS "Error Code",
    error_name AS "Error Name",
    sum_error_raised AS "Total Occurrences",
    first_seen AS "First Seen",
    last_seen AS "Last Seen"
FROM performance_schema.events_errors_summary_by_account_by_error
WHERE error_name IN (
    'ER_ACCESS_DENIED_ERROR',
    'ER_ABORTING_CONNECTION',
    'ER_NEW_ABORTING_CONNECTION'
)
  AND sum_error_raised > 0
ORDER BY last_seen DESC;


-- +------------------------------+-----------+------------+------------------------+-------------------+---------------------+---------------------+
-- | User                         | Host      | Error Code | Error Name             | Total Occurrences | First Seen          | Last Seen           |
-- +------------------------------+-----------+------------+------------------------+-------------------+---------------------+---------------------+
-- | __google_connectivity_prober | localhost |       1045 | ER_ACCESS_DENIED_ERROR |              7480 | 2026-09-25 10:19:04 | 2026-09-25 16:30:26 |
-- +------------------------------+-----------+------------+------------------------+-------------------+---------------------+---------------------+



-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** Requires PostgreSQL 14 or higher (Uses native `pg_stat_database` session tracking).

SELECT 
    datname AS "Database Name", 
    sessions AS "Total Sessions Initiated", 
    sessions_abandoned AS "Abandoned Sessions (Lost Connection)", 
    sessions_fatal AS "Fatal Sessions (Errors/Terminated)", 
    ROUND(
        ((sessions_abandoned + sessions_fatal)::numeric / NULLIF(sessions, 0)) * 100, 2
    ) AS "Failure Rate (%)"
FROM pg_stat_database 
WHERE datname NOT LIKE 'template%' 
  AND (sessions_abandoned > 0 OR sessions_fatal > 0)
ORDER BY "Failure Rate (%)" DESC NULLS LAST 
LIMIT 15;

--  Database Name | Total Sessions Initiated | Abandoned Sessions (Lost Connection) | Fatal Sessions (Errors/Terminated) | Failure Rate (%) 
-- ---------------+--------------------------+--------------------------------------+------------------------------------+------------------
--  postgres      |                    48660 |                                   40 |                                  0 |             0.08
--  ecommerce     |                    48502 |                                   11 |                                  0 |             0.02

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** These read system-level cumulative counters.

-- -


-- ### 📊 3. [Suggested Topic] Max Connection Utilization (High Watermark)

-- Checks how close the database has come to hitting its hard limit for concurrent connections. If Peak/Current utilization exceeds 80%, you risk throwing `Too many connections` / `FATAL: sorry, too many clients already` errors to the application.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `performance_schema`.

-- (IF performance_schema)

SELECT 
    (SELECT CAST(variable_value AS UNSIGNED) FROM performance_schema.global_variables WHERE variable_name = 'max_connections') AS "Max Allowed Connections",
    (SELECT CAST(variable_value AS UNSIGNED) FROM performance_schema.global_status WHERE variable_name = 'Threads_connected') AS "Currently Connected",
    (SELECT CAST(variable_value AS UNSIGNED) FROM performance_schema.global_status WHERE variable_name = 'Max_used_connections') AS "Peak Used",
    ROUND(
        (SELECT CAST(variable_value AS FLOAT) FROM performance_schema.global_status WHERE variable_name = 'Max_used_connections') / 
        (SELECT CAST(variable_value AS FLOAT) FROM performance_schema.global_variables WHERE variable_name = 'max_connections') * 100, 2
    ) AS "Peak Utilization (%)"
;

-- +-------------------------+---------------------+-----------+----------------------+
-- | Max Allowed Connections | Currently Connected | Peak Used | Peak Utilization (%) |
-- +-------------------------+---------------------+-----------+----------------------+
-- |                    4030 |                   6 |        11 |                 0.27 |
-- +-------------------------+---------------------+-----------+----------------------+

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    current_setting('max_connections')::integer AS "Max Allowed Connections",
    (SELECT COUNT(*) FROM pg_stat_activity) AS "Currently Connected",
    current_setting('superuser_reserved_connections')::integer AS "Reserved for Superusers",
    ROUND(
        ((SELECT COUNT(*) FROM pg_stat_activity)::numeric / current_setting('max_connections')::numeric) * 100, 2
    ) AS "Current Utilization (%)"
;


--  Max Allowed Connections | Currently Connected | Reserved for Superusers | Current Utilization (%) 
-- -------------------------+---------------------+-------------------------+-------------------------
--                      100 |                  13 |                       3 |                   13.00
-- (1 row)

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Very fast parameter reads.
---
-- ### 📊 4. [Suggested Topic] Idle Connection Bloat (Memory Wasters)
-- Every open connection consumes RAM on the database server (roughly ~2-10MB per connection). Connections that have been sitting completely idle (Sleeping) for more than 5 minutes represent wasted resources and often point to an unoptimized application connection pool (e.g., PgBouncer, HikariCP, or SQLAlchemy configurations).

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `PROCESS` privilege.


SELECT 
    user AS "Username", 
    SUBSTRING_INDEX(host, ':', 1) AS "Client IP", 
    db AS "Database Name", 
    time AS "Idle Time (Seconds)",
    CONCAT(ROUND(time / 60, 2), ' Minutes') AS "Idle Duration"
FROM information_schema.processlist 
WHERE command = 'Sleep' 
  AND user NOT IN ('system user', 'event_scheduler', 'rdsadmin', 'cloudsqladmin') 
  AND time > 300 
ORDER BY time DESC 
LIMIT 15;

-- (NO DATA)

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    usename AS "Username", 
    client_addr AS "Client IP", 
    datname AS "Database Name", 
    ROUND(EXTRACT(EPOCH FROM (NOW() - state_change))::numeric, 2) AS "Idle Time (Seconds)",
    TO_CHAR((NOW() - state_change), 'HH24:MI:SS') AS "Idle Duration"
FROM pg_stat_activity 
WHERE state = 'idle' 
  AND usename NOT IN ('cloudsqladmin', 'cloudsqlagent')
  AND EXTRACT(EPOCH FROM (NOW() - state_change)) > 300
ORDER BY "Idle Time (Seconds)" DESC 
LIMIT 15;

-- (NO DATA)

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Helps identify targets that are safe to terminate if memory pressure becomes a concern.

---
-- =========================================
-- STORAGE
-- =========================================


-- -

-- ### 📊 1. Table & Index Fragmentation (Reclaimable Space / Bloat)

-- Frequent `UPDATE` and `DELETE` operations leave dead space in table pages. In MySQL, this appears as unallocated free space in InnoDB tablespaces. In PostgreSQL, dead tuples create table "bloat" where pages remain allocated on disk even when empty.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    table_schema AS "Database Name",
    table_name AS "Table Name",
    CONCAT(ROUND(data_length / 1024 / 1024, 2), ' MB') AS "Data Size",
    CONCAT(ROUND(index_length / 1024 / 1024, 2), ' MB') AS "Index Size",
    CONCAT(ROUND(data_free / 1024 / 1024, 2), ' MB') AS "Reclaimable Free Space",
    ROUND((data_free / NULLIF(data_length + index_length + data_free, 0)) * 100, 2) AS "Fragmentation (%)"
FROM information_schema.tables
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
  AND table_type = 'BASE TABLE'
  AND data_free > 0
ORDER BY data_free DESC
LIMIT 15;


-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None (Uses dead-tuple heuristic estimations via `pg_stat_user_tables`).

SELECT 
    schemaname AS "Schema Name",
    relname AS "Table Name",
    n_dead_tup AS "Dead Tuples",
    n_live_tup AS "Live Rows",
    ROUND((n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0)) * 100, 2) AS "Bloat Ratio (%)",
    pg_size_pretty(pg_total_relation_size(relid)) AS "Total Allocated Space",
    pg_size_pretty(
        ROUND((n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0)) * pg_relation_size(relid))::bigint
    ) AS "Estimated Reclaimable Space"
FROM pg_stat_user_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND n_dead_tup > 0
ORDER BY (n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0)) DESC NULLS LAST
LIMIT 15;


-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** These rely on system metadata and statistic counters rather than performing full table scans.

---

-- ### 📊 2. [Suggested Topic] Top Largest Tables (Storage Hotspots)

-- Identifies the largest tables and their associated indexes consuming disk space. Tracking these top consumers helps prioritize partitioning, archival policies, or compression strategies.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    table_schema AS "Database Name",
    table_name AS "Table Name",
    table_rows AS "Estimated Rows",
    CONCAT(ROUND(data_length / 1024 / 1024, 2), ' MB') AS "Data Size",
    CONCAT(ROUND(index_length / 1024 / 1024, 2), ' MB') AS "Index Size",
    CONCAT(ROUND((data_length + index_length) / 1024 / 1024, 2), ' MB') AS "Total Size"
FROM information_schema.tables
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
  AND table_type = 'BASE TABLE'
ORDER BY (data_length + index_length) DESC
LIMIT 15;


-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    schemaname AS "Schema Name",
    relname AS "Table Name",
    pg_size_pretty(pg_relation_size(relid)) AS "Data Size",
    pg_size_pretty(pg_indexes_size(relid)) AS "Index Size",
    pg_size_pretty(pg_total_relation_size(relid)) AS "Total Size"
FROM pg_stat_user_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 15;


-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Reads dictionary metadata instantly.

---

-- ### 📊 3. [Suggested Topic] TOAST / Large Off-Page Column Bloat
-- 
-- Large text, JSON, or BLOB columns (`VARCHAR(MAX)`, `JSONB`, `TEXT`) are stored out-of-page (TOAST in PostgreSQL, External Pages in MySQL/InnoDB). Unoptimized updates to large columns can cause explosive storage growth that isn't captured by basic row counts.

-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    table_schema AS "Database Name",
    table_name AS "Table Name",
    column_name AS "Column Name",
    data_type AS "Data Type",
    character_maximum_length AS "Max Character Capacity"
FROM information_schema.columns
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
  AND data_type IN ('text', 'mediumtext', 'longtext', 'blob', 'mediumblob', 'longblob', 'json')
ORDER BY table_schema, table_name
LIMIT 15;


-- +---------------+---------------------+-------------+-----------+------------------------+
-- | Database Name | Table Name          | Column Name | Data Type | Max Character Capacity |
-- +---------------+---------------------+-------------+-----------+------------------------+
-- | hr_db         | performance_reviews | comments    | text      |                  65535 |
-- +---------------+---------------------+-------------+-----------+------------------------+

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    c.relnamespace::regnamespace::text AS "Schema Name",
    c.relname AS "Main Table Name",
    pg_size_pretty(pg_relation_size(c.reltoastrelid)) AS "TOAST Storage Size",
    pg_size_pretty(pg_total_relation_size(c.oid)) AS "Total Table Size Including TOAST"
FROM pg_class c
WHERE c.reltoastrelid != 0
  AND c.relnamespace::regnamespace::text NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(c.reltoastrelid) DESC
LIMIT 15;


--  Schema Name | Main Table Name  | TOAST Storage Size | Total Table Size Including TOAST 
-- -------------+------------------+--------------------+----------------------------------
--  public      | orders           | 0 bytes            | 45 MB
--  public      | fragmented_table | 0 bytes            | 10 MB
--  public      | stress_test      | 0 bytes            | 71 MB

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Highly effective for discovering hidden space consumption driven by JSON/text-heavy workloads.

---

-- ### 📊 4. [Suggested Topic] Database-Level Size Distribution
-- Provides an executive summary of total disk usage broken down by database, showing where storage resources are distributed across your database instance.
-- #### 🐬 MySQL Query

-- **Prerequisites / Privileges Required:** Requires `SELECT` on `information_schema`.

SELECT 
    table_schema AS "Database Name",
    CONCAT(ROUND(SUM(data_length + index_length) / 1024 / 1024, 2), ' MB') AS "Total Size",
    CONCAT(ROUND(SUM(data_length) / 1024 / 1024, 2), ' MB') AS "Data Size",
    CONCAT(ROUND(SUM(index_length) / 1024 / 1024, 2), ' MB') AS "Index Size",
    COUNT(*) AS "Total Tables"
FROM information_schema.tables
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
GROUP BY table_schema
ORDER BY SUM(data_length + index_length) DESC
LIMIT 15;


-- +----------------+------------+-----------+------------+--------------+
-- | Database Name  | Total Size | Data Size | Index Size | Total Tables |
-- +----------------+------------+-----------+------------+--------------+
-- | ecommerce_db   | 0.19 MB    | 0.08 MB   | 0.11 MB    |            5 |
-- | logistics_db   | 0.19 MB    | 0.08 MB   | 0.11 MB    |            5 |
-- | hr_db          | 0.17 MB    | 0.08 MB   | 0.09 MB    |            5 |
-- | test_index_db  | 0.05 MB    | 0.02 MB   | 0.03 MB    |            1 |
-- | test_unused_db | 0.05 MB    | 0.02 MB   | 0.03 MB    |            1 |
-- | test_locks_db  | 0.02 MB    | 0.02 MB   | 0.00 MB    |            1 |

-- #### 🐘 PostgreSQL Query

-- **Prerequisites / Extensions Required:** None.

SELECT 
    datname AS "Database Name",
    pg_size_pretty(pg_database_size(datname)) AS "Database Size"
FROM pg_database
WHERE datname NOT LIKE 'template%'
  AND datname != 'postgres'
ORDER BY pg_database_size(datname) DESC
LIMIT 15;



--    Database Name   | Database Size 
-- -------------------+---------------
--  sbtest            | 105 MB
--  autovacuum_poc_db | 17 MB
--  test_autovacuum   | 14 MB
--  cloudsqladmin     | 8342 kB
--  application       | 7814 kB

-- #### ⚠️ Production Safety & Risks

-- **Safe to run anytime.** Lightweight aggregate metadata check.

-- ---

-- Let me know if you would like to adjust any of these or if you are ready for the next **[TOPIC]** (such as **Autovacuum & Purge Health**, **Replication & HA**, or **Configuration / Security Warnings**)!

