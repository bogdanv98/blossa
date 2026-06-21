-- hr_install_auto.sql ---------------------------------------------------------
-- Non-interactive HR install. Oracle's own hr_install.sql uses ACCEPT/HIDE
-- prompts that are unreliable when fed over a pipe, so we replicate its setup
-- deterministically and then call its (prompt-free) sub-scripts directly.
--
-- Must be run FROM the human_resources/ directory (so @@ includes resolve),
-- connected as a privileged user (SYSTEM):
--   cd /tmp/db-sample-schemas/human_resources
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/hr_install_auto.sql
--
-- HR password is fixed to "oracle" to match samples/hr.yml.

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET SERVEROUTPUT ON

-- Drop a pre-existing HR so re-runs are idempotent.
DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'HR';
   IF n > 0 THEN
      EXECUTE IMMEDIATE 'DROP USER HR CASCADE';
      DBMS_OUTPUT.PUT_LINE('Dropped existing HR schema.');
   END IF;
END;
/

CREATE USER hr IDENTIFIED BY "oracle"
               DEFAULT TABLESPACE USERS
               QUOTA UNLIMITED ON USERS;

GRANT CREATE MATERIALIZED VIEW,
      CREATE PROCEDURE,
      CREATE SEQUENCE,
      CREATE SESSION,
      CREATE SYNONYM,
      CREATE TABLE,
      CREATE TRIGGER,
      CREATE TYPE,
      CREATE VIEW
  TO hr;

ALTER SESSION SET CURRENT_SCHEMA=HR;
ALTER SESSION SET NLS_LANGUAGE=American;
ALTER SESSION SET NLS_TERRITORY=America;

-- These three are pure DDL/DML, no prompts.
-- Single @ resolves relative to the current working directory (we cd into
-- human_resources/ before running), unlike @@ which is relative to THIS script.
@hr_create.sql
@hr_populate.sql
@hr_code.sql

SET HEADING ON
SET FEEDBACK OFF
SELECT 'employees' AS "Table", 107 AS "expected", COUNT(1) AS "actual" FROM hr.employees
UNION ALL
SELECT 'departments', 27, COUNT(1) FROM hr.departments
UNION ALL
SELECT 'jobs', 19, COUNT(1) FROM hr.jobs;

exit
