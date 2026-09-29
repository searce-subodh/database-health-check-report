1 & 2. Active Lock Contention & Idle in Transaction
Goal: Create a table, start a transaction in Session A that modifies a row but intentionally forgets to COMMIT. Then, use Session B to try to modify that exact same row, causing it to freeze (Contention). Session A simultaneously becomes a stale "Idle in Transaction" lock.

PostgreSQL & MySQL (8.0+) Setup (Run anywhere):

SQL
CREATE TABLE test_lock_contention (id INT PRIMARY KEY, status VARCHAR(20));
INSERT INTO test_lock_contention (id, status) VALUES (1, 'pending');
Execute in Session A (The Blocker):

SQL
-- PostgreSQL & MySQL
BEGIN;
UPDATE test_lock_contention SET status = 'processing' WHERE id = 1;
-- DO NOT COMMIT. Leave this session open and idle.
-- Wait 11+ seconds here before running your MySQL Idle transaction check.
Execute in Session B (The Blocked / Victim):

SQL
-- PostgreSQL & MySQL
-- This query will hang immediately waiting for Session A to release the row lock.
UPDATE test_lock_contention SET status = 'completed' WHERE id = 1;
(Now run your Sub-section 1 & 2 Health Check queries in a third session to see the blocking tree and the idle transaction).

3. Deadlock Occurrences (Historical Tracking)
Goal: Force a circular dependency where Session A locks Row 1, Session B locks Row 2, and then both try to lock each other's row. The database engine will violently kill one of them to resolve the loop, incrementing the deadlock counter.

PostgreSQL & MySQL Setup (Run anywhere):

SQL
CREATE TABLE test_deadlock (id INT PRIMARY KEY, val INT);
INSERT INTO test_deadlock (id, val) VALUES (1, 100), (2, 200);
Step 1: Execute in Session A:

SQL
BEGIN;
UPDATE test_deadlock SET val = 101 WHERE id = 1;
Step 2: Execute in Session B:

SQL
BEGIN;
UPDATE test_deadlock SET val = 201 WHERE id = 2;
Step 3: Execute in Session A:

SQL
-- This will hang, waiting for Session B
UPDATE test_deadlock SET val = 202 WHERE id = 2;
Step 4: Execute in Session B:

SQL
-- This triggers the deadlock. The DB will instantly abort one of the transactions.
UPDATE test_deadlock SET val = 102 WHERE id = 1;
(Now run your Sub-section 3 Health Check queries to see the deadlocks counter increment).

4. Metadata and Table-Level Locks (DDL Blockers)
Goal: Simulate an environment where a long-running read query (or uncommitted transaction) holds a shared lock on a table, and a DBA attempts to modify the table schema. The schema change requires an exclusive lock and gets blocked.

PostgreSQL & MySQL Setup (Run anywhere):

SQL
CREATE TABLE test_metadata_lock (id INT PRIMARY KEY, payload TEXT);
INSERT INTO test_metadata_lock (id, payload) VALUES (1, 'data');
Execute in Session A (The Active User):

SQL
BEGIN;
-- A standard SELECT places a shared lock on the table in Postgres, 
-- and a metadata read lock in MySQL.
SELECT * FROM test_metadata_lock; 
-- DO NOT COMMIT.
Execute in Session B (The Blocked DBA):

SQL
-- This structural change requires an exclusive lock. 
-- It will hang indefinitely until Session A commits or rolls back.
ALTER TABLE test_metadata_lock ADD COLUMN new_column INT;
(Now run your Sub-section 4 Health Check queries in a third session to catch the pending AccessExclusiveLock in Postgres or PENDING metadata lock in MySQL).