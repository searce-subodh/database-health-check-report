-- Here are the SQL statements to generate the tables, populate the dummy data, and execute the specific test queries required to trigger the health check counters for both PostgreSQL and MySQL (8.0+ using recursive CTEs for data generation).

-- You can execute these directly via your DB client or wrap them in your Python script's `execute()` functions.
-- 
-- ### 1. Unused Indexes

-- **Goal:** Create a table, add an index, and deliberately *never* query the indexed column so its usage statistics remain at zero.

-- **PostgreSQL:**

-- ```sql
-- Create table and populate 1,000 rows
CREATE TABLE test_unused_idx (id SERIAL PRIMARY KEY, category VARCHAR(50));
INSERT INTO test_unused_idx (category) 
SELECT 'cat_' || (random() * 10)::int FROM generate_series(1, 1000);

-- Create the index that will not be used
CREATE INDEX idx_never_used ON test_unused_idx (category);

-- Execute a query that bypasses the index (queries PK instead)
SELECT * FROM test_unused_idx WHERE id = 500;

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- Create table and populate 1,000 rows
CREATE TABLE test_unused_idx (id INT AUTO_INCREMENT PRIMARY KEY, category VARCHAR(50));
INSERT INTO test_unused_idx (category)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 1000)
SELECT CONCAT('cat_', FLOOR(RAND() * 10)) FROM cte;

-- Create the index that will not be used
CREATE INDEX idx_never_used ON test_unused_idx (category);

-- Execute a query that bypasses the index
SELECT * FROM test_unused_idx WHERE id = 500;

-- ```

-- ---

-- ### 2. Duplicate Indexes

-- **Goal:** Create a table and define two separate indexes on the exact same column(s). Execute a query so the optimizer is forced to choose one, leaving the other redundant.

-- **PostgreSQL:**

-- ```sql
-- Create table and populate 1,000 rows
CREATE TABLE test_duplicate_idx (id SERIAL PRIMARY KEY, status VARCHAR(20));
INSERT INTO test_duplicate_idx (status) 
SELECT CASE WHEN random() > 0.5 THEN 'active' ELSE 'inactive' END FROM generate_series(1, 1000);

-- Create exact duplicate indexes
CREATE INDEX idx_status_dominant ON test_duplicate_idx (status);
CREATE INDEX idx_status_redundant ON test_duplicate_idx (status);

-- Query the column to generate index usage statistics
SELECT count(*) FROM test_duplicate_idx WHERE status = 'active';

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- Create table and populate 1,000 rows
CREATE TABLE test_duplicate_idx (id INT AUTO_INCREMENT PRIMARY KEY, status VARCHAR(20));
INSERT INTO test_duplicate_idx (status)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 1000)
SELECT IF(RAND() > 0.5, 'active', 'inactive') FROM cte;

-- Create exact duplicate indexes
CREATE INDEX idx_status_dominant ON test_duplicate_idx (status);
CREATE INDEX idx_status_redundant ON test_duplicate_idx (status);

-- Query the column to generate index usage statistics
SELECT COUNT(*) FROM test_duplicate_idx WHERE status = 'active';

-- ```

-- ---

-- ### 3. High Volume Full Table Scans

-- **Goal:** Create a table large enough to bypass small-table exclusions (your Postgres query requires `> 1MB`), insert significant data, and run a query that forces a sequential/full scan.

-- **PostgreSQL:**

-- ```sql
-- Create table
CREATE TABLE test_seq_scan (id SERIAL PRIMARY KEY, payload TEXT);

-- Insert 20,000 rows of padded text to guarantee the table exceeds 1MB in size
INSERT INTO test_seq_scan (payload) 
SELECT repeat('A', 150) || random()::text FROM generate_series(1, 20000);

-- Run a non-sargable query multiple times to simulate high volume sequential scans
SELECT count(*) FROM test_seq_scan WHERE payload LIKE '%B%';
SELECT count(*) FROM test_seq_scan WHERE payload LIKE '%C%';
SELECT count(*) FROM test_seq_scan WHERE payload LIKE '%D%';

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- Create table
CREATE TABLE test_seq_scan (id INT AUTO_INCREMENT PRIMARY KEY, payload TEXT);

-- Insert 10,000 rows of padded text
INSERT INTO test_seq_scan (payload)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 10000)
SELECT REPEAT(CHAR(FLOOR(65 + (RAND() * 26))), 200) FROM cte;

-- Run a non-sargable query multiple times to simulate high volume full table scans
SELECT COUNT(*) FROM test_seq_scan WHERE payload LIKE '%B%';
SELECT COUNT(*) FROM test_seq_scan WHERE payload LIKE '%C%';
SELECT COUNT(*) FROM test_seq_scan WHERE payload LIKE '%D%';

-- ```



-- Here are the SQL scripts to generate dummy data and artificially trigger the conditions for the **Performance** section.

-- To reliably trigger these metrics—especially disk spills and dirty pages—we will use a mix of large payload inserts, aggressive `UPDATE` statements, and session-level memory restrictions.

-- ### 1 & 2. Buffer Pool Hit Ratio & Dirty Pages

-- **Goal:** Generate a large table to force data pages out of the cache (triggering disk reads) and run massive `UPDATE` statements to flood the buffer pool with modified, unwritten pages (dirty pages).

-- **PostgreSQL:**

-- ```sql
-- 1. Create a large table
CREATE TABLE test_io_bound (id SERIAL PRIMARY KEY, payload TEXT);

-- 2. Insert 100,000 rows with padded text to consume significant MBs of disk/memory
INSERT INTO test_io_bound (payload) 
SELECT repeat('XYZ', 1000) FROM generate_series(1, 100000);

-- 3. Massive read to trigger blks_read (disk reads) if data exceeds shared_buffers
SELECT count(*) FROM test_io_bound WHERE payload LIKE '%Q%';

-- 4. Massive UPDATE to flood memory with "Dirty Pages"
UPDATE test_io_bound SET payload = repeat('ABC', 1000) WHERE id % 2 = 0;

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- 1. Create a large table
CREATE TABLE test_io_bound (id INT AUTO_INCREMENT PRIMARY KEY, payload TEXT);

-- 2. Insert 50,000 rows (adjusted slightly for MySQL recursive CTE limits)
INSERT INTO test_io_bound (payload)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 50000)
SELECT REPEAT('XYZ', 1000) FROM cte;

-- 3. Massive read to trigger physical disk reads
SELECT COUNT(*) FROM test_io_bound WHERE payload LIKE '%Q%';

-- 4. Massive UPDATE to increase MODIFIED_DATABASE_PAGES (Dirty Pages)
UPDATE test_io_bound SET payload = REPEAT('ABC', 1000) WHERE MOD(id, 2) = 0;

-- ```

-- ---

-- ### 4. Temporary Table / Disk Spill Usage

-- **Goal:** We will dynamically restrict the session's memory limit to a tiny threshold, forcing the database engine to write intermediate query processing results (like sorting or hashing) to disk.

-- **PostgreSQL:**

-- ```sql
-- 1. Create table for sorting/grouping
CREATE TABLE test_spill (id SERIAL PRIMARY KEY, category INT, payload TEXT);
INSERT INTO test_spill (category, payload)
SELECT (random() * 1000)::int, repeat('A', 500) FROM generate_series(1, 100000);

-- 2. Restrict session memory to an extremely low 64 kilobyte threshold
SET work_mem = '64kB';

-- 3. Execute a heavy GROUP BY and ORDER BY that will immediately spill to disk
SELECT category, count(*), string_agg(substring(payload, 1, 10), ',')
FROM test_spill
GROUP BY category
ORDER BY category DESC;

-- 4. Reset memory back to default
RESET work_mem;

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- 1. Create table for sorting/grouping
CREATE TABLE test_spill (id INT AUTO_INCREMENT PRIMARY KEY, category INT, payload TEXT);
INSERT INTO test_spill (category, payload)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 50000)
SELECT FLOOR(RAND() * 1000), REPEAT('A', 500) FROM cte;

-- 2. Restrict session temporary table sizes to the minimum limit (~1KB)
SET SESSION tmp_table_size = 1024;
SET SESSION max_heap_table_size = 1024;

-- 3. Execute a heavy GROUP BY and ORDER BY that will immediately spill to disk
SELECT category, COUNT(*), GROUP_CONCAT(SUBSTRING(payload, 1, 10))
FROM test_spill
GROUP BY category
ORDER BY category DESC;

-- ```

-- ---

-- ### 5. Top Queries by Execution Time

-- **Goal:** Ensure the database logs a query with a high `total_latency` / `total_exec_time` without actually maxing out CPU or I/O. We use native sleep functions.


-- **PostgreSQL:**

-- ```sql
-- Execute a query that intentionally sleeps for 5 seconds total (1 sec per row x 5)
SELECT pg_sleep(1), 'Simulated Slow Query' AS label 
FROM generate_series(1, 5);

-- ```

-- **MySQL (8.0+):**

-- ```sql
-- Execute a query that intentionally sleeps for 5 seconds total
SELECT SLEEP(1), 'Simulated Slow Query' AS label 
FROM (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) AS t;

-- ```





-- To test the **Locks** section, you must simulate concurrency. This means you cannot run these sequentially in a single script; you need to open **two separate database connections** (Session A and Session B) to create conflicts, plus a third connection to run your health check queries.

-- If you are writing this in Python, you will need to instantiate two separate connection objects (e.g., `conn_a = psycopg2.connect(...)` and `conn_b = psycopg2.connect(...)`) and use multithreading or async, or simply run these manually in two different SQL client tabs.

-- Here is the step-by-step SQL to generate each locking scenario for both databases.

-- ### 1 & 2. Active Lock Contention & Idle in Transaction

-- **Goal:** Create a table, start a transaction in Session A that modifies a row but intentionally forgets to `COMMIT`. Then, use Session B to try to modify that exact same row, causing it to freeze (Contention). Session A simultaneously becomes a stale "Idle in Transaction" lock.

-- **PostgreSQL & MySQL (8.0+) Setup (Run anywhere):**

-- ```sql
CREATE TABLE test_lock_contention (id INT PRIMARY KEY, status VARCHAR(20));
INSERT INTO test_lock_contention (id, status) VALUES (1, 'pending');

-- ```

-- **Execute in Session A (The Blocker):**

-- ```sql
-- PostgreSQL & MySQL
BEGIN;
UPDATE test_lock_contention SET status = 'processing' WHERE id = 1;
-- DO NOT COMMIT. Leave this session open and idle.
-- Wait 11+ seconds here before running your MySQL Idle transaction check.

-- ```

-- **Execute in Session B (The Blocked / Victim):**

-- ```sql
-- PostgreSQL & MySQL
-- This query will hang immediately waiting for Session A to release the row lock.
UPDATE test_lock_contention SET status = 'completed' WHERE id = 1;

-- ```

-- *(Now run your Sub-section 1 & 2 Health Check queries in a **third** session to see the blocking tree and the idle transaction).*

-- ---

-- ### 3. Deadlock Occurrences (Historical Tracking)

-- **Goal:** Force a circular dependency where Session A locks Row 1, Session B locks Row 2, and then both try to lock each other's row. The database engine will violently kill one of them to resolve the loop, incrementing the deadlock counter.

-- **PostgreSQL & MySQL Setup (Run anywhere):**

-- ```sql
CREATE TABLE test_deadlock (id INT PRIMARY KEY, val INT);
INSERT INTO test_deadlock (id, val) VALUES (1, 100), (2, 200);

-- ```

-- **Step 1: Execute in Session A:**

-- ```sql
BEGIN;
UPDATE test_deadlock SET val = 101 WHERE id = 1;

-- ```

-- **Step 2: Execute in Session B:**

-- ```sql
BEGIN;
UPDATE test_deadlock SET val = 201 WHERE id = 2;

-- ```

-- **Step 3: Execute in Session A:**

-- ```sql
-- This will hang, waiting for Session B
UPDATE test_deadlock SET val = 202 WHERE id = 2;

-- ```

-- **Step 4: Execute in Session B:**

-- ```sql
-- This triggers the deadlock. The DB will instantly abort one of the transactions.
UPDATE test_deadlock SET val = 102 WHERE id = 1;

-- ```

-- *(Now run your Sub-section 3 Health Check queries to see the deadlocks counter increment).*

-- ---

-- ### 4. Metadata and Table-Level Locks (DDL Blockers)

-- **Goal:** Simulate an environment where a long-running read query (or uncommitted transaction) holds a shared lock on a table, and a DBA attempts to modify the table schema. The schema change requires an exclusive lock and gets blocked.

-- **PostgreSQL & MySQL Setup (Run anywhere):**

-- ```sql
CREATE TABLE test_metadata_lock (id INT PRIMARY KEY, payload TEXT);
INSERT INTO test_metadata_lock (id, payload) VALUES (1, 'data');

-- ```

-- **Execute in Session A (The Active User):**

-- ```sql
BEGIN;
-- A standard SELECT places a shared lock on the table in Postgres, 
-- and a metadata read lock in MySQL.
SELECT * FROM test_metadata_lock; 
-- DO NOT COMMIT.

-- ```

-- **Execute in Session B (The Blocked DBA):**

-- ```sql
-- This structural change requires an exclusive lock. 
-- It will hang indefinitely until Session A commits or rolls back.
ALTER TABLE test_metadata_lock ADD COLUMN new_column INT;

-- ```

-- *(Now run your Sub-section 4 Health Check queries in a third session to catch the pending `AccessExclusiveLock` in Postgres or `PENDING` metadata lock in MySQL).*


To test the **Connections** section, we are shifting from manipulating database tables to manipulating the database *client*. SQL alone cannot spawn new connections—your Python script must handle this by deliberately opening, holding, and crashing connections.

Here is the strategy and the code logic you will need to add to your Python script to trigger these metrics for both PostgreSQL and MySQL.

### 1 & 3. Active Connections & Max Utilization

**Goal:** Spike the number of concurrent connections to increase the "Currently Connected" and "Peak Used" counters, and show active states in the summary.

**Python Strategy (Using threading or async):**
To simulate a connection spike, write a Python loop that spawns 10–20 background threads. Each thread opens a connection, executes a harmless sleep command so it stays "Active", and then safely closes.

**PostgreSQL & MySQL Execution via Python:**

```python
import threading
import time
# Use psycopg2 for Postgres or pymysql for MySQL

def simulate_active_connection():
    # 1. Open connection
    # conn = connect_to_db(...)
    
    # 2. Keep the connection "Active" (running a query) for 10 seconds
    # cursor = conn.cursor()
    # Postgres: cursor.execute("SELECT pg_sleep(10);")
    # MySQL: cursor.execute("SELECT SLEEP(10);")
    
    # 3. Close connection
    # conn.close()

# Spawn 15 simultaneous connections
threads = []
for _ in range(15):
    t = threading.Thread(target=simulate_active_connection)
    threads.append(t)
    t.start()

# While these threads are sleeping, run your Health Check Queries 1 & 3 in your main thread!

```

---

### 2. Aborted / Failed Connections

**Goal:** Intentionally fail authentication to trigger `ER_ACCESS_DENIED_ERROR` (MySQL) and `sessions_fatal` (PostgreSQL), and abruptly kill a connection to trigger `sessions_abandoned`.

**Python Strategy:**

1. **Authentication Failures:** Deliberately pass a wrong password in a loop.
2. **Abandoned Connections:** Open a valid connection, find its Process ID, and kill it from *another* connection.

**PostgreSQL & MySQL Execution via Python:**

```python
# --- Trigger Authentication Failures ---
for _ in range(5):
    try:
        # Intentionally use a bad password
        # connect_to_db(user="root", password="WRONG_PASSWORD_123")
    except Exception as e:
        pass # Ignore the expected auth failure

# --- Trigger Abandoned / Terminated Connections ---
# 1. Open Session A
# conn_a = connect_to_db(...)
# cursor_a = conn_a.cursor()

# 2. Get Session A's Process ID
# Postgres: cursor_a.execute("SELECT pg_backend_pid();")
# MySQL: cursor_a.execute("SELECT CONNECTION_ID();")
# pid_to_kill = cursor_a.fetchone()[0]

# 3. Open Session B and kill Session A
# conn_b = connect_to_db(...)
# cursor_b = conn_b.cursor()
# Postgres: cursor_b.execute(f"SELECT pg_terminate_backend({pid_to_kill});")
# MySQL: cursor_b.execute(f"KILL {pid_to_kill};")

# (Now run your Sub-section 2 Health Check query)

```

---

### 4. Idle Connection Bloat (Memory Wasters)

**Goal:** Create a connection that does absolutely nothing (Command state: `Sleep` or `idle`) for more than 5 minutes (300 seconds) so your health check flags it.

**Python Strategy:**
Because waiting 5 minutes during a test script execution is usually annoying, **I highly recommend temporarily changing the `> 300` to `> 5` in your SQL health check query just for this dummy test.**

**PostgreSQL & MySQL Execution via Python:**

```python
# 1. Open a connection
# idle_conn = connect_to_db(...)
# cursor = idle_conn.cursor()

# 2. Run a fast query to transition the state from 'authenticating' to 'idle'
# cursor.execute("SELECT 1;")
# cursor.fetchall()

# 3. The python script sleeps, doing NOTHING with the database connection.
# The connection is now "Idle in transaction" (if autocommit is off) or just "Idle/Sleep".
import time
time.sleep(10) # Wait 10 seconds

# (Run your Sub-section 4 Health Check query NOW from a separate connection. 
# Make sure you changed the query threshold from > 300 to > 5 seconds for testing!)

# 4. Cleanup
# idle_conn.close()

```




Here are the SQL scripts to generate dummy data and artificially trigger the conditions for the **Storage** section.

To reliably trigger these metrics, we need to manipulate table physical structures: generating dead tuples (bloat), inserting massive strings to force out-of-page/TOAST storage, and generating enough volume to top the largest tables list.

### 1. Table & Index Fragmentation (Reclaimable Space / Bloat)

**Goal:** Create a table, insert rows, and then aggressively `UPDATE` and `DELETE` them.
*(Note for PostgreSQL: We will temporarily disable `autovacuum` on this specific test table. If autovacuum runs too fast, it will clean up the dead tuples before your Python script can query them).*

**PostgreSQL:**

```sql
-- 1. Create table with autovacuum disabled to guarantee dead tuples stick around
CREATE TABLE test_bloat (id SERIAL PRIMARY KEY, payload TEXT) WITH (autovacuum_enabled = false);

-- 2. Insert 20,000 rows
INSERT INTO test_bloat (payload) 
SELECT repeat('A', 200) FROM generate_series(1, 20000);

-- 3. Update all rows (in Postgres, an UPDATE is technically a DELETE + INSERT of a new tuple)
UPDATE test_bloat SET payload = repeat('B', 250);

-- 4. Delete half the rows to create massive fragmentation
DELETE FROM test_bloat WHERE id % 2 = 0;

-- 5. Analyze to immediately update pg_stat_user_tables
ANALYZE test_bloat;

```

**MySQL (8.0+):**

```sql
-- 1. Create table
CREATE TABLE test_bloat (id INT AUTO_INCREMENT PRIMARY KEY, payload VARCHAR(1000));

-- 2. Insert 20,000 rows
INSERT INTO test_bloat (payload)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 20000)
SELECT REPEAT('A', 200) FROM cte;

-- 3. Delete half the rows to leave "data_free" gaps in the InnoDB tablespace
DELETE FROM test_bloat WHERE MOD(id, 2) = 0;

-- 4. Force stats update so information_schema.tables reflects the data_free size
ANALYZE TABLE test_bloat;

```

---

### 2 & 3. Top Largest Tables & TOAST / Off-Page Column Bloat

**Goal:** We can satisfy both of these checks with a single table. We will create a table with `TEXT`/`JSON` columns and insert rows with exceptionally large text strings.
In PostgreSQL, column values exceeding ~2KB are compressed and moved out of the main table pages into a hidden "TOAST" table. In MySQL, your health check query simply looks for the presence of `blob/text/json` data types in the schema.

**PostgreSQL:**

```sql
-- 1. Create a table with a TEXT column
CREATE TABLE test_large_toast (
    id SERIAL PRIMARY KEY, 
    large_text TEXT, 
    metadata JSONB
);

-- 2. Insert 10,000 rows with 15KB strings to forcefully trigger TOAST off-page storage
-- This will simultaneously make it one of the largest tables in the database.
INSERT INTO test_large_toast (large_text, metadata) 
SELECT 
    repeat('XYZ', 5000), 
    '{"status": "active", "category": "test"}'::jsonb 
FROM generate_series(1, 10000);

-- 3. Update stats
ANALYZE test_large_toast;

```

**MySQL (8.0+):**

```sql
-- 1. Create a table with LONGTEXT and JSON to trigger your specific schema check
CREATE TABLE test_large_toast (
    id INT AUTO_INCREMENT PRIMARY KEY, 
    large_text LONGTEXT,
    metadata JSON
);

-- 2. Insert 10,000 rows with massive strings to inflate data_length for the Largest Tables check
INSERT INTO test_large_toast (large_text, metadata)
WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 10000)
SELECT REPEAT('XYZ', 5000), '{"status": "active", "category": "test"}' FROM cte;

-- 3. Update stats
ANALYZE TABLE test_large_toast;

```

---

### 4. Database-Level Size Distribution

**Goal:** Summarize overall instance size.

**No additional data generation is required here.** The 10,000 to 20,000 heavily padded rows you generated in Steps 1, 2, and 3 will consume tens of megabytes. Once those scripts run, this query will automatically aggregate and reflect the bloated sizes of your test database.