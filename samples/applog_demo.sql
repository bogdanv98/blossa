-- applog_demo.sql ---------------------------------------------------------------
-- A small SYNTHETIC schema whose point is to contain realistic APPLICATION log /
-- error / audit tables — the kind a business app keeps to record what happened to
-- its data (NOT Oracle's own redo/alert logs). None of Oracle's sample schemas has
-- one, so this is the known-truth test bed for Blossa's log-table detection.
--
-- Tables (and what makes each a "log" Blossa should recognise):
--   ERROR_LOG    error/exception log: log_time + severity + message + source + db_user
--                + a business reference (ORDER_ID -> APP_ORDERS).
--   AUDIT_TRAIL  change audit: event_time + action + table/row + changed_by + details (CLOB).
--   JOB_RUN_LOG  batch-job log: started/finished + status + rows_processed + error_text.
--   APP_ORDERS   an ordinary BUSINESS table (NOT a log) — the negative case, and the
--                target of ERROR_LOG.ORDER_ID so a log can reference a business entity.
--
-- Some message text deliberately contains PII-shaped fragments (emails, card tails) so we
-- can exercise masking before any of it would ever reach a model.
--
-- Run as a privileged user (SYSTEM):
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/applog_demo.sql
-- Password is fixed to "oracle".

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET SERVEROUTPUT ON

DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'APPLOG';
   IF n > 0 THEN
      EXECUTE IMMEDIATE 'DROP USER APPLOG CASCADE';
   END IF;
END;
/

CREATE USER applog IDENTIFIED BY "oracle"
              DEFAULT TABLESPACE USERS
              QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE TO applog;
ALTER SESSION SET CURRENT_SCHEMA=APPLOG;

-- Business table (the NEGATIVE case: ordinary entity, must NOT be flagged as a log).
CREATE TABLE app_orders (
   order_id    NUMBER(10) NOT NULL,
   customer    VARCHAR2(60),
   amount      NUMBER(12,2),
   status      VARCHAR2(20),
   CONSTRAINT pk_app_orders PRIMARY KEY (order_id)
);

-- Error / exception log: the canonical case.
CREATE TABLE error_log (
   error_id    NUMBER(12) NOT NULL,
   log_time    TIMESTAMP  DEFAULT SYSTIMESTAMP NOT NULL,
   severity    VARCHAR2(10),                 -- FATAL / ERROR / WARN / INFO
   error_code  NUMBER(6),
   module      VARCHAR2(80),                 -- e.g. ORDER_API.SUBMIT_ORDER
   message     VARCHAR2(2000),               -- free-text error message
   db_user     VARCHAR2(30),
   order_id    NUMBER(10),                   -- business reference (nullable)
   CONSTRAINT pk_error_log PRIMARY KEY (error_id),
   CONSTRAINT fk_error_log_order FOREIGN KEY (order_id) REFERENCES app_orders (order_id)
);

-- Change audit trail.
CREATE TABLE audit_trail (
   audit_id    NUMBER(12) NOT NULL,
   event_time  TIMESTAMP  DEFAULT SYSTIMESTAMP NOT NULL,
   action      VARCHAR2(10),                 -- INSERT / UPDATE / DELETE
   object_name VARCHAR2(40),                 -- table that changed
   row_pk      VARCHAR2(40),
   changed_by  VARCHAR2(30),
   details     CLOB,                         -- before/after, free text
   CONSTRAINT pk_audit_trail PRIMARY KEY (audit_id)
);

-- Batch-job run log.
CREATE TABLE job_run_log (
   run_id          NUMBER(12) NOT NULL,
   job_name        VARCHAR2(60),
   started_at      TIMESTAMP,
   finished_at     TIMESTAMP,
   status          VARCHAR2(12),             -- SUCCESS / FAILED / RUNNING
   rows_processed  NUMBER(10),
   error_text      VARCHAR2(2000),
   CONSTRAINT pk_job_run_log PRIMARY KEY (run_id)
);

INSERT INTO app_orders VALUES (5001, 'Acme Ltd',  1290.00, 'PAID');
INSERT INTO app_orders VALUES (5002, 'Globex',     430.50, 'CANCELLED');
INSERT INTO app_orders VALUES (5003, 'Initech',   8800.00, 'PAID');
INSERT INTO app_orders VALUES (5004, 'Umbrella',    75.00, 'FAILED');

INSERT INTO error_log (error_id, log_time, severity, error_code, module, message, db_user, order_id)
   VALUES (1, SYSTIMESTAMP - 2, 'ERROR', 20001, 'ORDER_API.SUBMIT_ORDER',
           'Payment declined for card ending 4321 on order 5004', 'ORDERSVC', 5004);
INSERT INTO error_log (error_id, log_time, severity, error_code, module, message, db_user, order_id)
   VALUES (2, SYSTIMESTAMP - 1.5, 'WARN', NULL, 'ORDER_API.SUBMIT_ORDER',
           'Retrying payment gateway timeout', 'ORDERSVC', 5004);
INSERT INTO error_log (error_id, log_time, severity, error_code, module, message, db_user, order_id)
   VALUES (3, SYSTIMESTAMP - 1, 'FATAL', 600, 'BILLING.NIGHTLY_INVOICE',
           'ORA-00600 internal error while posting invoice for jane.doe@initech.com', 'BATCH', 5003);
INSERT INTO error_log (error_id, log_time, severity, error_code, module, message, db_user, order_id)
   VALUES (4, SYSTIMESTAMP - 0.2, 'ERROR', 20010, 'ORDER_API.CANCEL_ORDER',
           'Cannot cancel an order that is already PAID', 'ORDERSVC', 5001);
INSERT INTO error_log (error_id, log_time, severity, error_code, module, message, db_user, order_id)
   VALUES (5, SYSTIMESTAMP - 0.1, 'INFO', NULL, 'ORDER_API.SUBMIT_ORDER',
           'Order accepted', 'ORDERSVC', 5002);

INSERT INTO audit_trail (audit_id, event_time, action, object_name, row_pk, changed_by, details)
   VALUES (1, SYSTIMESTAMP - 2, 'UPDATE', 'APP_ORDERS', '5004', 'jsmith',
           'status: NEW -> FAILED');
INSERT INTO audit_trail (audit_id, event_time, action, object_name, row_pk, changed_by, details)
   VALUES (2, SYSTIMESTAMP - 1, 'DELETE', 'APP_ORDERS', '5099', 'admin',
           'removed test order');

INSERT INTO job_run_log (run_id, job_name, started_at, finished_at, status, rows_processed, error_text)
   VALUES (1, 'NIGHTLY_INVOICE', SYSTIMESTAMP - 1, SYSTIMESTAMP - 1 + 0.02, 'FAILED', 12,
           'ORA-00600 internal error while posting invoice');
INSERT INTO job_run_log (run_id, job_name, started_at, finished_at, status, rows_processed, error_text)
   VALUES (2, 'NIGHTLY_INVOICE', SYSTIMESTAMP - 0.05, SYSTIMESTAMP, 'SUCCESS', 14, NULL);
COMMIT;

SET HEADING ON
SELECT 'app_orders'  AS "Table", COUNT(1) AS "rows" FROM app_orders
UNION ALL SELECT 'error_log',    COUNT(1) FROM error_log
UNION ALL SELECT 'audit_trail',  COUNT(1) FROM audit_trail
UNION ALL SELECT 'job_run_log',  COUNT(1) FROM job_run_log;

exit
