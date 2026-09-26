import time
import threading
import psycopg2
import pymysql

# ==========================================
# CONFIGURATION
# ==========================================
# Set this list to control which databases to test: ['postgres', 'mysql']
RUN_ON = ['postgres', 'mysql']

# Define credentials for both databases
DATABASES = {
    'postgres': {
        'host': '10.35.112.5',
        'port': 5432,
        'database': 'health_test_db',
        'user': 'postgres',
        'password': 'postgres'
    },
    'mysql': {
        'host': '10.35.112.3',
        'port': 3306,
        'database': 'health_test_db',
        'user': 'root',
        'password': 'root'
    }
}

def get_connection(db_type, override_config=None):
    """Returns a new database connection based on the requested db_type."""
    config = override_config if override_config else DATABASES[db_type]
    
    if db_type == 'postgres':
        return psycopg2.connect(**config)
    elif db_type == 'mysql':
        # PyMySQL doesn't autocommit by default, which is required for our lock tests
        return pymysql.connect(**config, autocommit=False)
    else:
        raise ValueError(f"Unsupported db_type: {db_type}")

# ==========================================
# 1. INDEXES SECTION
# ==========================================
def generate_indexes_data(db_type):
    print(f"\n--- Generating Indexes Dummy Data for {db_type.upper()} ---")
    conn = get_connection(db_type)
    conn.autocommit = True
    cursor = conn.cursor()

    if db_type == 'postgres':
        # Unused Index
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_unused_idx_ (id SERIAL PRIMARY KEY, category VARCHAR(50));
            INSERT INTO test_unused_idx_ (category) SELECT 'cat_' || (random() * 10)::int FROM generate_series(1, 1000);
            CREATE INDEX IF NOT EXISTS idx__never_used ON test_unused_idx_ (category);
            SELECT * FROM test_unused_idx_ WHERE id = 500;
        """)
        # Duplicate Index
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_duplicate_idx_ (id SERIAL PRIMARY KEY, status VARCHAR(20));
            INSERT INTO test_duplicate_idx_ (status) SELECT CASE WHEN random() > 0.5 THEN 'active' ELSE 'inactive' END FROM generate_series(1, 1000);
            CREATE INDEX IF NOT EXISTS idx__status_dominant ON test_duplicate_idx_ (status);
            CREATE INDEX IF NOT EXISTS idx__status_redundant ON test_duplicate_idx_ (status);
            SELECT count(*) FROM test_duplicate_idx_ WHERE status = 'active';
        """)
        # High Volume Scans
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_seq_scan (id SERIAL PRIMARY KEY, payload TEXT);
            INSERT INTO test_seq_scan (payload) SELECT repeat('A', 150) || random()::text FROM generate_series(1, 20000);
            SELECT count(*) FROM test_seq_scan WHERE payload LIKE '%B%';
        """)
    elif db_type == 'mysql':
        # Increase the recursion depth for this session
        cursor.execute("SET SESSION cte_max_recursion_depth = 1000000;")

        # Unused Index
        cursor.execute("CREATE TABLE IF NOT EXISTS test_unused_idx_ (id INT AUTO_INCREMENT PRIMARY KEY, category VARCHAR(50));")
        cursor.execute("INSERT INTO test_unused_idx_ (category) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 1000) SELECT CONCAT('cat_', FLOOR(RAND() * 10)) FROM cte;")
        cursor.execute("CREATE INDEX idx__never_used ON test_unused_idx_ (category);")
        cursor.execute("SELECT * FROM test_unused_idx_ WHERE id = 500;")

        # Duplicate Index
        cursor.execute("CREATE TABLE IF NOT EXISTS test_duplicate_idx_ (id INT AUTO_INCREMENT PRIMARY KEY, status VARCHAR(20));")
        cursor.execute("INSERT INTO test_duplicate_idx_ (status) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 1000) SELECT IF(RAND() > 0.5, 'active', 'inactive') FROM cte;")
        cursor.execute("CREATE INDEX idx__status_dominant ON test_duplicate_idx_ (status);")
        cursor.execute("CREATE INDEX idx__status_redundant ON test_duplicate_idx_ (status);")
        cursor.execute("SELECT COUNT(*) FROM test_duplicate_idx_ WHERE status = 'active';")

        # High Volume Scans
        cursor.execute("CREATE TABLE IF NOT EXISTS test_seq_scan (id INT AUTO_INCREMENT PRIMARY KEY, payload TEXT);")
        cursor.execute("INSERT INTO test_seq_scan (payload) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 10000) SELECT REPEAT(CHAR(FLOOR(65 + (RAND() * 26))), 200) FROM cte;")
        cursor.execute("SELECT COUNT(*) FROM test_seq_scan WHERE payload LIKE '%B%';")

    print(f"Index data generated. (Wait 1-2 seconds for {db_type.upper()} stats to update)")
    input(f">>> Run your INDEX Health Check queries on {db_type.upper()} now, then press Enter to continue...")
    conn.close()

# ==========================================
# 2. PERFORMANCE SECTION
# ==========================================
def generate_performance_data(db_type):
    print(f"\n--- Generating Performance Dummy Data for {db_type.upper()} ---")
    conn = get_connection(db_type)
    conn.autocommit = True
    cursor = conn.cursor()

    if db_type == 'postgres':
        # IO Bound / Dirty Pages
        cursor.execute("CREATE TABLE IF NOT EXISTS test_io_bound (id SERIAL PRIMARY KEY, payload TEXT);")
        cursor.execute("INSERT INTO test_io_bound (payload) SELECT repeat('XYZ', 1000) FROM generate_series(1, 100000);")
        cursor.execute("SELECT count(*) FROM test_io_bound WHERE payload LIKE '%Q%';")
        cursor.execute("UPDATE test_io_bound SET payload = repeat('ABC', 1000) WHERE id % 2 = 0;")
        
        # Disk Spills
        cursor.execute("CREATE TABLE IF NOT EXISTS test_spill (id SERIAL PRIMARY KEY, category INT, payload TEXT);")
        cursor.execute("INSERT INTO test_spill (category, payload) SELECT (random() * 1000)::int, repeat('A', 500) FROM generate_series(1, 100000);")
        cursor.execute("SET work_mem = '64kB';")
        cursor.execute("SELECT category, count(*), string_agg(substring(payload, 1, 10), ',') FROM test_spill GROUP BY category ORDER BY category DESC;")
        cursor.execute("RESET work_mem;")
        
        # Slow Query
        cursor.execute("SELECT pg_sleep(5), 'Simulated Slow Query' AS label;")
        
    elif db_type == 'mysql':
        # Increase the recursion depth for this session
        cursor.execute("SET SESSION cte_max_recursion_depth = 1000000;")

        # IO Bound / Dirty Pages
        cursor.execute("CREATE TABLE IF NOT EXISTS test_io_bound (id INT AUTO_INCREMENT PRIMARY KEY, payload TEXT);")
        cursor.execute("INSERT INTO test_io_bound (payload) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 50000) SELECT REPEAT('XYZ', 1000) FROM cte;")
        cursor.execute("SELECT COUNT(*) FROM test_io_bound WHERE payload LIKE '%Q%';")
        cursor.execute("UPDATE test_io_bound SET payload = REPEAT('ABC', 1000) WHERE MOD(id, 2) = 0;")

        # Disk Spills
        cursor.execute("CREATE TABLE IF NOT EXISTS test_spill (id INT AUTO_INCREMENT PRIMARY KEY, category INT, payload TEXT);")
        cursor.execute("INSERT INTO test_spill (category, payload) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 50000) SELECT FLOOR(RAND() * 1000), REPEAT('A', 500) FROM cte;")
        cursor.execute("SET SESSION tmp_table_size = 1024;")
        cursor.execute("SET SESSION max_heap_table_size = 1024;")
        cursor.execute("SELECT category, COUNT(*), GROUP_CONCAT(SUBSTRING(payload, 1, 10)) FROM test_spill GROUP BY category ORDER BY category DESC;")
        
        # Slow Query
        cursor.execute("SELECT SLEEP(5), 'Simulated Slow Query' AS label FROM (SELECT 1 UNION SELECT 2) AS t LIMIT 1;")

    print("Performance data generated.")
    input(f">>> Run your PERFORMANCE Health Check queries on {db_type.upper()} now, then press Enter to continue...")
    conn.close()

# ==========================================
# 3. LOCKS SECTION
# ==========================================
def execute_blocking_query(conn, query):
    """Helper to run a query in a thread that is expected to hang."""
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        conn.commit()
    except Exception as e:
        print(f"Thread query aborted/deadlocked as expected: {e}")

def generate_locks_data(db_type):
    print(f"\n--- Generating Locks Dummy Data for {db_type.upper()} ---")
    
    # 1. Setup tables
    conn_setup = get_connection(db_type)
    conn_setup.autocommit = True
    c_setup = conn_setup.cursor()
    
    c_setup.execute("CREATE TABLE IF NOT EXISTS test_lock_contention (id INT PRIMARY KEY, status VARCHAR(20));")
    c_setup.execute("CREATE TABLE IF NOT EXISTS test_deadlock (id INT PRIMARY KEY, val INT);")
    c_setup.execute("CREATE TABLE IF NOT EXISTS test_metadata_lock (id INT PRIMARY KEY, payload TEXT);")
    
    if db_type == 'postgres':
        c_setup.execute("INSERT INTO test_lock_contention (id, status) VALUES (1, 'pending') ON CONFLICT DO NOTHING;")
        c_setup.execute("INSERT INTO test_deadlock (id, val) VALUES (1, 100), (2, 200) ON CONFLICT DO NOTHING;")
    elif db_type == 'mysql':
        c_setup.execute("INSERT IGNORE INTO test_lock_contention (id, status) VALUES (1, 'pending');")
        c_setup.execute("INSERT IGNORE INTO test_deadlock (id, val) VALUES (1, 100), (2, 200);")
    
    conn_setup.close()

    # --- 2. SIMULATE DEADLOCK (For Query 3) ---
    print("Simulating deadlock to increment historical counters...")
    def cause_deadlock():
        try:
            conn1 = get_connection(db_type)
            conn2 = get_connection(db_type)
            conn1.autocommit = False
            conn2.autocommit = False
            c1 = conn1.cursor()
            c2 = conn2.cursor()

            if db_type == 'postgres':
                c1.execute("BEGIN;")
                c2.execute("BEGIN;")
            else:
                c1.execute("START TRANSACTION;")
                c2.execute("START TRANSACTION;")
                
            c1.execute("UPDATE test_deadlock SET val = 101 WHERE id = 1;")
            c2.execute("UPDATE test_deadlock SET val = 201 WHERE id = 2;")
            
            # Cross-lock each other to force the engine to kill one
            def thread_cross_lock_1():
                try:
                    c1.execute("UPDATE test_deadlock SET val = 202 WHERE id = 2;")
                except Exception:
                    pass

            def thread_cross_lock_2():
                try:
                    time.sleep(0.5) # Offset slightly to ensure a collision
                    c2.execute("UPDATE test_deadlock SET val = 102 WHERE id = 1;")
                except Exception:
                    pass

            t_dl1 = threading.Thread(target=thread_cross_lock_1)
            t_dl2 = threading.Thread(target=thread_cross_lock_2)
            t_dl1.start()
            t_dl2.start()
            t_dl1.join()
            t_dl2.join()
            conn1.close()
            conn2.close()
        except Exception as e:
            pass # Deadlocks throw exceptions by design, we ignore them here

    cause_deadlock()

    # --- 3. SIMULATE CONTENTION & IDLE TRANSACTION (For Queries 1 & 2) ---
    print("Simulating lock contention and idle transaction...")
    conn_a = get_connection(db_type)
    conn_b = get_connection(db_type)
    conn_a.autocommit = False
    conn_b.autocommit = True
    cursor_a = conn_a.cursor()

    if db_type == 'postgres':
        cursor_a.execute("BEGIN;")
    else:
        cursor_a.execute("START TRANSACTION;")

    # Connection A locks the row and goes IDLE
    cursor_a.execute("UPDATE test_lock_contention SET status = 'processing' WHERE id = 1;")
    
    # Connection B tries to update the same row and HANGS (Contention)
    t_contention = threading.Thread(target=execute_blocking_query, args=(conn_b, "UPDATE test_lock_contention SET status = 'completed' WHERE id = 1;"))
    t_contention.start()

    # --- 4. SIMULATE METADATA LOCK (For Query 4) ---
    print("Simulating metadata lock / DDL blocker...")
    conn_c = get_connection(db_type)
    conn_d = get_connection(db_type)
    conn_c.autocommit = False
    conn_d.autocommit = True
    cursor_c = conn_c.cursor()
    
    if db_type == 'postgres':
        cursor_c.execute("BEGIN;")
        # Explicit share lock guarantees conflict with ALTER TABLE
        cursor_c.execute("LOCK TABLE test_metadata_lock IN SHARE MODE;") 
    else:
        cursor_c.execute("START TRANSACTION;")
        # FOR SHARE ensures metadata locks are held in MySQL
        cursor_c.execute("SELECT * FROM test_metadata_lock FOR SHARE;")
        
    # Connection D attempts DDL and HANGS waiting for Connection C
    t_meta = threading.Thread(target=execute_blocking_query, args=(conn_d, "ALTER TABLE test_metadata_lock ADD COLUMN IF NOT EXISTS new_col INT;"))
    t_meta.start()

    # Wait 12 seconds to ensure MySQL "Duration (s) > 10" threshold is met!
    print("Waiting 12 seconds to trigger 'Duration > 10s' threshold for Idle Transactions...")
    time.sleep(12)

    input(f">>> Run ALL your LOCKS Health Check queries on {db_type.upper()} now, then press Enter to release the locks...")
    
    # Cleanup: Rollback releases all locks so threads can exit cleanly
    conn_a.rollback()
    conn_c.rollback()
    t_contention.join()
    t_meta.join()
    
    conn_a.close()
    conn_b.close()
    conn_c.close()
    conn_d.close()


    print(f"\n--- Generating Locks Dummy Data for {db_type.upper()} ---")
    
    # Multiple connections to simulate locks
    conn_a = get_connection(db_type)
    conn_b = get_connection(db_type)
    conn_a.autocommit = False 
    conn_b.autocommit = False
    cursor_a = conn_a.cursor()
    cursor_b = conn_b.cursor()

    # 1. Setup tables (Autocommit on just for setup)
    conn_setup = get_connection(db_type)
    conn_setup.autocommit = True
    c_setup = conn_setup.cursor()
    c_setup.execute("CREATE TABLE IF NOT EXISTS test_lock_contention (id INT PRIMARY KEY, status VARCHAR(20));")
    
    # DB specific insert ignore/conflict syntax
    if db_type == 'postgres':
        c_setup.execute("INSERT INTO test_lock_contention (id, status) VALUES (1, 'pending') ON CONFLICT DO NOTHING;")
    elif db_type == 'mysql':
        c_setup.execute("INSERT IGNORE INTO test_lock_contention (id, status) VALUES (1, 'pending');")
        
    c_setup.execute("CREATE TABLE IF NOT EXISTS test_metadata_lock (id INT PRIMARY KEY, payload TEXT);")
    conn_setup.close()

    # 2. Simulate Active Lock Contention & Idle in Transaction
    print("Simulating lock contention...")
    cursor_a.execute("UPDATE test_lock_contention SET status = 'processing' WHERE id = 1;")
    # DO NOT COMMIT conn_a! It is now holding the lock.
    
    # Session B tries to update the same row. This will HANG, so we put it in a thread.
    t1 = threading.Thread(target=execute_blocking_query, args=(conn_b, "UPDATE test_lock_contention SET status = 'completed' WHERE id = 1;"))
    t1.start()

    # 3. Simulate Metadata Lock (DDL Blocker)
    print("Simulating metadata lock...")
    conn_c = get_connection(db_type)
    conn_c.autocommit = False
    cursor_c = conn_c.cursor()
    cursor_c.execute("SELECT * FROM test_metadata_lock;") # Holds shared lock
    
    conn_d = get_connection(db_type)
    # Attempting to alter table will hang because conn_c is reading it
    t2 = threading.Thread(target=execute_blocking_query, args=(conn_d, "ALTER TABLE test_metadata_lock ADD COLUMN IF NOT EXISTS new_column INT;"))
    t2.start()

    # Wait a few seconds for the idle transaction duration to tick up
    print("Waiting 6 seconds to trigger 'Duration' thresholds...")
    time.sleep(6)

    input(f">>> Run your LOCKS Health Check queries on {db_type.upper()} now, then press Enter to release the locks...")
    
    # Cleanup: Committing releases the locks so the threads can finish
    conn_a.commit()
    conn_c.commit()
    t1.join()
    t2.join()
    conn_a.close()
    conn_b.close()
    conn_c.close()
    conn_d.close()

# ==========================================
# 4. CONNECTIONS SECTION
# ==========================================
def keep_connection_alive(db_type):
    conn = get_connection(db_type)
    cursor = conn.cursor()
    if db_type == 'postgres':
        cursor.execute("SELECT pg_sleep(30);")
    elif db_type == 'mysql':
        cursor.execute("SELECT SLEEP(30);")
    conn.close()

def generate_connections_data(db_type):
    print(f"\n--- Generating Connections Dummy Data for {db_type.upper()} ---")
    
    # 1. Spike Active Connections
    print("Spawning 15 background connections...")
    threads = []
    for _ in range(15):
        t = threading.Thread(target=keep_connection_alive, args=(db_type,))
        threads.append(t)
        t.start()

    # 2. Simulate Auth Failures
    print("Simulating failed logins...")
    for _ in range(5):
        try:
            bad_config = DATABASES[db_type].copy()
            bad_config['password'] = 'WRONG_PASSWORD_123'
            get_connection(db_type, override_config=bad_config)
        except Exception:
            pass # Expected to fail

    # 3. Simulate Idle Connection
    idle_conn = get_connection(db_type)
    idle_cursor = idle_conn.cursor()
    idle_cursor.execute("SELECT 1;") # Transitions state to 'idle'
    
    print("Wait ~5 seconds for connections to register...")
    time.sleep(5)
    
    input(f">>> Run your CONNECTIONS Health Check queries on {db_type.upper()} now (Note: Use > 5 secs for idle check), then press Enter...")
    idle_conn.close()

# ==========================================
# 5. STORAGE SECTION
# ==========================================
def generate_storage_data(db_type):
    print(f"\n--- Generating Storage Dummy Data for {db_type.upper()} ---")
    conn = get_connection(db_type)
    conn.autocommit = True
    cursor = conn.cursor()

    if db_type == 'postgres':
        # Bloat
        cursor.execute("CREATE TABLE IF NOT EXISTS test_bloat (id SERIAL PRIMARY KEY, payload TEXT) WITH (autovacuum_enabled = false);")
        cursor.execute("INSERT INTO test_bloat (payload) SELECT repeat('A', 200) FROM generate_series(1, 20000);")
        cursor.execute("UPDATE test_bloat SET payload = repeat('B', 250);")
        cursor.execute("DELETE FROM test_bloat WHERE id % 2 = 0;")
        cursor.execute("ANALYZE test_bloat;")
        
        # TOAST / Largest tables
        cursor.execute("CREATE TABLE IF NOT EXISTS test_large_toast (id SERIAL PRIMARY KEY, large_text TEXT, metadata JSONB);")
        cursor.execute("INSERT INTO test_large_toast (large_text, metadata) SELECT repeat('XYZ', 5000), '{\"status\": \"active\"}'::jsonb FROM generate_series(1, 10000);")
        cursor.execute("ANALYZE test_large_toast;")
        
    elif db_type == 'mysql':
        # Increase the recursion depth for this session
        cursor.execute("SET SESSION cte_max_recursion_depth = 1000000;")

        # Bloat
        cursor.execute("CREATE TABLE IF NOT EXISTS test_bloat (id INT AUTO_INCREMENT PRIMARY KEY, payload VARCHAR(1000));")
        cursor.execute("INSERT INTO test_bloat (payload) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 20000) SELECT REPEAT('A', 200) FROM cte;")
        cursor.execute("DELETE FROM test_bloat WHERE MOD(id, 2) = 0;")
        cursor.execute("ANALYZE TABLE test_bloat;")

        # TOAST / Largest tables
        cursor.execute("CREATE TABLE IF NOT EXISTS test_large_toast (id INT AUTO_INCREMENT PRIMARY KEY, large_text LONGTEXT, metadata JSON);")
        cursor.execute("INSERT INTO test_large_toast (large_text, metadata) WITH RECURSIVE cte AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 10000) SELECT REPEAT('XYZ', 5000), '{\"status\": \"active\", \"category\": \"test\"}' FROM cte;")
        cursor.execute("ANALYZE TABLE test_large_toast;")

    print("Storage data generated.")
    input(f">>> Run your STORAGE Health Check queries on {db_type.upper()} now, then press Enter to finish this section...")
    conn.close()

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    print("Starting Database Health Check Mock Data Generator...")
    
    for current_db in RUN_ON:
        print(f"\n{'='*50}")
        print(f"    BEGINNING TEST SUITE FOR: {current_db.upper()}")
        print(f"{'='*50}")
        
        try:
            generate_indexes_data(current_db)
            generate_performance_data(current_db)
            generate_locks_data(current_db)
            generate_connections_data(current_db)
            generate_storage_data(current_db)
            
            print(f"\n All sections complete for {current_db.upper()}! You can now drop the 'test_%' tables from your database.")
        except Exception as e:
            print(f"\n An error occurred while testing {current_db.upper()}: {e}")
            print(f"Skipping the rest of {current_db.upper()} suite...")

    print("\n Finished running all requested database suites.")