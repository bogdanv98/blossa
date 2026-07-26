-- bank_eod_chain.sql --------------------------------------------------------------
-- A SYNTHETIC but realistic END-OF-DAY (EOD) core-banking batch, wired as a real
-- DBMS_SCHEDULER CHAIN on top of the BANKDEMO schema created by bank_demo.sql.
--
-- The point is to give Blossa a *running scheduled process* to discover and explain:
-- not one monolithic job, but a dependency graph of steps with parallel branches, an
-- AND join, and an error branch — the shape almost every real bank's nightly batch has.
--
--   S01_OPEN_BUSINESS_DATE       cut-off: open the accounting day and the batch log entry
--     ├─ S02_INGEST_CLEARING     inbound interbank clearing file -> PENDING transactions
--     └─ S03_RESET_CARD_LIMITS   roll card daily-spend counters, expire due cards
--          (AND) -> S04_SETTLE_PENDING   post PENDING txns, move balances, write the GL
--            ├─ S05_ACCRUE_INTEREST  ACT/365 loan accrual + savings credit interest
--            │    └─ S08_DELINQUENCY days-past-due buckets, IFRS 9 stage, provisions
--            ├─ S06_APPLY_FEES       overdraft + monthly maintenance fees
--            └─ S07_AML_SCREEN       structuring / high-value / dormant-reactivation
--                 (AND) -> S09_RECONCILE          DR=CR, balance vs statement, suspense
--                            -> S10_REGULATORY_EXTRACT  daily prudential metrics
--                                 -> S11_CLOSE_BATCH -> END 0
--   any step FAILED -> S99_FAIL_HANDLER -> END 1
--
-- A failed night is retried on the SAME business date, tracked by EOD_RUN.ATTEMPT_NO;
-- rolling forward instead would close the books on a day nobody ever processed. The
-- three steps that move money - clearing intake, interest accrual and fees - are keyed
-- on the business date so a retry cannot post twice. Screening, arrears and the extract
-- re-derive a fresh position on each attempt, which is harmless: every row they write
-- carries its RUN_ID, so the attempts stay distinguishable.
--
-- It exercises several Blossa features at once:
--   * scheduler objects -> a chain, 12 programs, 13 rules and a nightly job to map
--   * program logic     -> the EOD_BATCH package, one procedure per step
--   * cross-schema      -> every run opens/closes a row in APPLOG.JOB_RUN_LOG and
--                          writes failures to APPLOG.ERROR_LOG (same convention as
--                          CORE_BANKING in bank_demo.sql)
--   * candidate FK      -> GL_ENTRY.ACCOUNT_ID is deliberately NOT declared as a FK
--                          (a real GL also books to non-customer accounts), so it is
--                          a known-truth undeclared reference for the FK inference
--
-- Idempotent: re-running drops and rebuilds the batch objects and re-seeds the loan
-- schedule. Beyond the balance and status changes a run legitimately makes, the one
-- thing it rewrites in bank_demo.sql's own tables is LOANS.OPENED_AT: those loans are
-- only weeks old, so no instalment of theirs could ever be late and the arrears step
-- would have nothing to classify. The seeding block below backdates origination to give
-- the book a spread of ages, and builds each schedule from that same date so LOANS and
-- LOAN_SCHEDULE stay consistent with each other.
--
-- Run as SYSTEM, AFTER applog_demo.sql and bank_demo.sql:
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/bank_eod_chain.sql

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET DEFINE OFF
SET SERVEROUTPUT ON SIZE UNLIMITED

-- ------------------------------------------------------------------ preconditions
DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'BANKDEMO';
   IF n = 0 THEN
      raise_application_error(-20900, 'BANKDEMO does not exist - run bank_demo.sql first.');
   END IF;
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'APPLOG';
   IF n = 0 THEN
      raise_application_error(-20901, 'APPLOG does not exist - run applog_demo.sql first.');
   END IF;
END;
/

-- A scheduler chain runs as its owner, so BANKDEMO needs CREATE JOB to own the
-- programs, the chain and the nightly job.
GRANT CREATE JOB TO bankdemo;

-- APPLOG.JOB_RUN_LOG has both STARTED_AT and FINISHED_AT, so a long-running batch opens
-- the row RUNNING and closes it at the end. That close is an UPDATE, which bank_demo.sql
-- did not need to grant (CORE_BANKING only ever inserts finished rows). Scoped to this
-- one table: the append-only rule still holds for ERROR_LOG and AUDIT_TRAIL.
GRANT UPDATE ON applog.job_run_log TO bankdemo;

-- ------------------------------------------------------- drop previous batch objects
-- Dropping the chain with force removes its steps and rules; the job goes first so a
-- run in flight cannot resurrect them.
DECLARE
   PROCEDURE try(p_sql VARCHAR2) IS
   BEGIN
      EXECUTE IMMEDIATE p_sql;
   EXCEPTION
      WHEN OTHERS THEN NULL;   -- object simply was not there yet
   END;
BEGIN
   try('BEGIN DBMS_SCHEDULER.STOP_JOB(''BANKDEMO.EOD_BATCH_JOB'', force => TRUE); END;');
   try('BEGIN DBMS_SCHEDULER.DROP_JOB(''BANKDEMO.EOD_BATCH_JOB'', force => TRUE); END;');
   try('BEGIN DBMS_SCHEDULER.DROP_CHAIN(''BANKDEMO.EOD_CHAIN'', force => TRUE); END;');

   FOR r IN (SELECT owner, program_name FROM dba_scheduler_programs
              WHERE owner = 'BANKDEMO') LOOP
      try('BEGIN DBMS_SCHEDULER.DROP_PROGRAM(''BANKDEMO.' || r.program_name
          || ''', force => TRUE); END;');
   END LOOP;
END;
/

ALTER SESSION SET CURRENT_SCHEMA = BANKDEMO;

-- Child tables first so the FKs come apart cleanly.
DECLARE
   TYPE t_names IS TABLE OF VARCHAR2(30);
   v_tabs t_names := t_names(
      'REG_DAILY_SNAPSHOT', 'RECON_BREAK', 'AML_ALERT', 'LOAN_ARREARS', 'LOAN_SCHEDULE',
      'FEE_CHARGE', 'INTEREST_ACCRUAL', 'GL_ENTRY', 'CLEARING_ITEM', 'EOD_STEP_LOG',
      'EOD_RUN', 'EOD_PARAM');
   v_seqs t_names := t_names(
      'EOD_RUN_SEQ', 'EOD_STEP_SEQ', 'CLEARING_SEQ', 'GL_SEQ', 'ACCRUAL_SEQ', 'FEE_SEQ',
      'SCHEDULE_SEQ', 'ARREARS_SEQ', 'AML_SEQ', 'RECON_SEQ', 'REG_SEQ');
   n NUMBER;
BEGIN
   FOR i IN 1 .. v_tabs.COUNT LOOP
      SELECT COUNT(*) INTO n FROM all_tables
       WHERE owner = 'BANKDEMO' AND table_name = v_tabs(i);
      IF n > 0 THEN
         EXECUTE IMMEDIATE 'DROP TABLE bankdemo.' || v_tabs(i) || ' CASCADE CONSTRAINTS PURGE';
      END IF;
   END LOOP;
   FOR i IN 1 .. v_seqs.COUNT LOOP
      SELECT COUNT(*) INTO n FROM all_sequences
       WHERE sequence_owner = 'BANKDEMO' AND sequence_name = v_seqs(i);
      IF n > 0 THEN
         EXECUTE IMMEDIATE 'DROP SEQUENCE bankdemo.' || v_seqs(i);
      END IF;
   END LOOP;
END;
/

-- ============================================================== batch control tables

-- Batch parameters. A real EOD is driven by a small key/value table like this rather
-- than by hardcoded constants, so ops can retune a rate or a threshold without a release.
CREATE TABLE eod_param (
   param_key    VARCHAR2(30)  NOT NULL,
   param_value  VARCHAR2(100),
   description  VARCHAR2(200),
   updated_at   DATE DEFAULT SYSDATE,
   CONSTRAINT pk_eod_param PRIMARY KEY (param_key)
);

COMMENT ON TABLE eod_param IS
   'Tunable parameters for the end-of-day batch (rates, thresholds, test hooks).';

-- One row per business date the batch has processed. The chain is single-threaded on
-- this table: S01 refuses to open a second run while one is still RUNNING.
CREATE TABLE eod_run (
   run_id          NUMBER(12)   NOT NULL,
   business_date   DATE         NOT NULL,
   attempt_no      NUMBER(3)    DEFAULT 1 NOT NULL,
   started_at      TIMESTAMP    DEFAULT SYSTIMESTAMP,
   finished_at     TIMESTAMP,
   status          VARCHAR2(12),          -- RUNNING / COMPLETED / FAILED
   steps_ok        NUMBER(3)    DEFAULT 0,
   steps_failed    NUMBER(3)    DEFAULT 0,
   rows_processed  NUMBER(10)   DEFAULT 0,
   -- APPLOG lives in another schema, so this cannot be a declared FK.
   job_log_id      NUMBER(12),
   CONSTRAINT pk_eod_run PRIMARY KEY (run_id),
   -- A failed night is retried on the SAME business date, so the day alone is not
   -- unique; the failed attempt is kept for audit rather than overwritten.
   CONSTRAINT uq_eod_run_date UNIQUE (business_date, attempt_no)
);

COMMENT ON TABLE eod_run IS
   'Header row per end-of-day batch run; one accounting day per attempt.';
COMMENT ON COLUMN eod_run.attempt_no IS
   'Retry counter for this business date. A failed night is re-run on the same date.';
COMMENT ON COLUMN eod_run.job_log_id IS
   'Matching RUN_ID in APPLOG.JOB_RUN_LOG (cross-schema, not a declared FK).';

CREATE TABLE eod_step_log (
   step_log_id    NUMBER(12)   NOT NULL,
   run_id         NUMBER(12)   NOT NULL,
   step_name      VARCHAR2(30) NOT NULL,
   started_at     TIMESTAMP,
   finished_at    TIMESTAMP,
   status         VARCHAR2(12),           -- SUCCESS / FAILED
   rows_affected  NUMBER(10),
   message        VARCHAR2(400),
   CONSTRAINT pk_eod_step_log PRIMARY KEY (step_log_id),
   CONSTRAINT fk_step_run FOREIGN KEY (run_id) REFERENCES eod_run (run_id)
);

COMMENT ON TABLE eod_step_log IS
   'Per-step audit of an end-of-day run: timings, row counts and outcome.';

-- ================================================================= processing tables

-- Inbound interbank clearing file. Items arrive addressed by IBAN; the ones we cannot
-- match to an account stay UNMATCHED and are booked to suspense, exactly as in a real
-- clearing intake.
CREATE TABLE clearing_item (
   clearing_id    NUMBER(12)    NOT NULL,
   run_id         NUMBER(12)    NOT NULL,
   business_date  DATE          NOT NULL,
   file_ref       VARCHAR2(40),           -- e.g. SEPA-IN-20260726-001
   scheme         VARCHAR2(12),           -- SEPA_CT / SEPA_DD / CARD
   direction      VARCHAR2(2),            -- CR (money in) / DR (money out)
   account_id     NUMBER(10),             -- NULL when the IBAN matched nothing
   iban_masked    VARCHAR2(34),
   amount         NUMBER(14,2),
   currency       VARCHAR2(3),
   status         VARCHAR2(12),           -- RECEIVED / UNMATCHED / SETTLED / REJECTED
   txn_id         NUMBER(12),
   CONSTRAINT pk_clearing_item PRIMARY KEY (clearing_id),
   CONSTRAINT fk_clr_run  FOREIGN KEY (run_id)     REFERENCES eod_run (run_id),
   CONSTRAINT fk_clr_acct FOREIGN KEY (account_id) REFERENCES accounts (account_id),
   CONSTRAINT fk_clr_txn  FOREIGN KEY (txn_id)     REFERENCES transactions (txn_id)
);

COMMENT ON TABLE clearing_item IS
   'Line items of the inbound interbank clearing file consumed by the nightly batch.';

-- Double-entry general ledger. ACCOUNT_ID is deliberately NOT a declared foreign key:
-- a GL books against customer accounts AND internal ones, so the reference is real but
-- unenforceable. This is the known-truth undeclared FK for this schema.
CREATE TABLE gl_entry (
   entry_id       NUMBER(12)    NOT NULL,
   run_id         NUMBER(12)    NOT NULL,
   business_date  DATE          NOT NULL,
   gl_code        VARCHAR2(8)   NOT NULL,
   dr_cr          VARCHAR2(2)   NOT NULL,   -- DR / CR
   amount         NUMBER(14,2)  NOT NULL,
   currency       VARCHAR2(3),
   account_id     NUMBER(10),               -- undeclared reference to ACCOUNTS
   source_step    VARCHAR2(30),
   narrative      VARCHAR2(200),
   CONSTRAINT pk_gl_entry PRIMARY KEY (entry_id),
   CONSTRAINT fk_gl_run FOREIGN KEY (run_id) REFERENCES eod_run (run_id)
);

COMMENT ON TABLE gl_entry IS
   'Double-entry general ledger postings produced by the end-of-day batch.';
COMMENT ON COLUMN gl_entry.gl_code IS
   '1010 cash, 1200 loans, 1210 interest receivable, 2010 customer deposits, 4010 interest income, 4020 fee income, 5010 interest expense, 9999 suspense.';

CREATE TABLE interest_accrual (
   accrual_id     NUMBER(12)    NOT NULL,
   run_id         NUMBER(12)    NOT NULL,
   business_date  DATE          NOT NULL,
   accrual_type   VARCHAR2(12),             -- LOAN / SAVINGS
   loan_id        NUMBER(10),
   account_id     NUMBER(10),
   basis_amount   NUMBER(14,2),
   rate_pct       NUMBER(6,3),
   day_count      VARCHAR2(8),              -- ACT/365
   accrued_amt    NUMBER(14,4),
   CONSTRAINT pk_interest_accrual PRIMARY KEY (accrual_id),
   CONSTRAINT fk_acr_run  FOREIGN KEY (run_id)     REFERENCES eod_run (run_id),
   CONSTRAINT fk_acr_loan FOREIGN KEY (loan_id)    REFERENCES loans (loan_id),
   CONSTRAINT fk_acr_acct FOREIGN KEY (account_id) REFERENCES accounts (account_id)
);

COMMENT ON TABLE interest_accrual IS
   'Daily interest accrued per loan and per savings account, one row per instrument per day.';

CREATE TABLE fee_charge (
   fee_id         NUMBER(12)    NOT NULL,
   run_id         NUMBER(12)    NOT NULL,
   business_date  DATE          NOT NULL,
   account_id     NUMBER(10)    NOT NULL,
   fee_code       VARCHAR2(16),             -- OVERDRAFT / MAINTENANCE
   amount         NUMBER(12,2),
   waived_yn      VARCHAR2(1)   DEFAULT 'N',
   reason         VARCHAR2(200),
   txn_id         NUMBER(12),
   CONSTRAINT pk_fee_charge PRIMARY KEY (fee_id),
   CONSTRAINT fk_fee_run  FOREIGN KEY (run_id)     REFERENCES eod_run (run_id),
   CONSTRAINT fk_fee_acct FOREIGN KEY (account_id) REFERENCES accounts (account_id),
   CONSTRAINT fk_fee_txn  FOREIGN KEY (txn_id)     REFERENCES transactions (txn_id)
);

COMMENT ON TABLE fee_charge IS
   'Fees the batch charged, including the ones it waived and why.';

-- Amortisation schedule, generated once per loan at install. Delinquency is measured
-- against it: the oldest unpaid instalment sets days-past-due.
CREATE TABLE loan_schedule (
   schedule_id     NUMBER(12)   NOT NULL,
   loan_id         NUMBER(10)   NOT NULL,
   installment_no  NUMBER(4)    NOT NULL,
   due_date        DATE         NOT NULL,
   principal_due   NUMBER(14,2),
   interest_due    NUMBER(14,2),
   paid_amt        NUMBER(14,2) DEFAULT 0,
   status          VARCHAR2(10),            -- PAID / DUE / OVERDUE
   CONSTRAINT pk_loan_schedule PRIMARY KEY (schedule_id),
   CONSTRAINT uq_loan_sched UNIQUE (loan_id, installment_no),
   CONSTRAINT fk_sched_loan FOREIGN KEY (loan_id) REFERENCES loans (loan_id)
);

COMMENT ON TABLE loan_schedule IS
   'Monthly amortisation schedule per loan; unpaid instalments drive days-past-due.';

CREATE TABLE loan_arrears (
   arrears_id      NUMBER(12)   NOT NULL,
   run_id          NUMBER(12)   NOT NULL,
   business_date   DATE         NOT NULL,
   loan_id         NUMBER(10)   NOT NULL,
   days_past_due   NUMBER(5),
   bucket          VARCHAR2(12),            -- CURRENT / DPD1_30 / ... / DPD90_PLUS
   ifrs9_stage     NUMBER(1),               -- 1 performing, 2 under-performing, 3 credit-impaired
   outstanding     NUMBER(14,2),
   provision_amt   NUMBER(14,2),
   CONSTRAINT pk_loan_arrears PRIMARY KEY (arrears_id),
   CONSTRAINT fk_arr_run  FOREIGN KEY (run_id)  REFERENCES eod_run (run_id),
   CONSTRAINT fk_arr_loan FOREIGN KEY (loan_id) REFERENCES loans (loan_id)
);

COMMENT ON TABLE loan_arrears IS
   'Daily arrears position per loan: days-past-due bucket, IFRS 9 stage and provision.';

CREATE TABLE aml_alert (
   alert_id       NUMBER(12)   NOT NULL,
   run_id         NUMBER(12)   NOT NULL,
   business_date  DATE         NOT NULL,
   rule_code      VARCHAR2(12),             -- AML-01 .. AML-04
   severity       VARCHAR2(8),              -- LOW / MEDIUM / HIGH
   account_id     NUMBER(10),
   customer_id    NUMBER(10),
   txn_id         NUMBER(12),
   amount         NUMBER(14,2),
   detail         VARCHAR2(400),
   status         VARCHAR2(12) DEFAULT 'OPEN',
   CONSTRAINT pk_aml_alert PRIMARY KEY (alert_id),
   CONSTRAINT fk_aml_run  FOREIGN KEY (run_id)      REFERENCES eod_run (run_id),
   CONSTRAINT fk_aml_acct FOREIGN KEY (account_id)  REFERENCES accounts (account_id),
   CONSTRAINT fk_aml_cust FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
   CONSTRAINT fk_aml_txn  FOREIGN KEY (txn_id)      REFERENCES transactions (txn_id)
);

COMMENT ON TABLE aml_alert IS
   'Anti-money-laundering screening hits raised by the nightly batch, for analyst review.';

CREATE TABLE recon_break (
   break_id       NUMBER(12)   NOT NULL,
   run_id         NUMBER(12)   NOT NULL,
   business_date  DATE         NOT NULL,
   check_code     VARCHAR2(20),             -- GL_BALANCED / STMT_VS_BALANCE / SUSPENSE_OPEN
   severity       VARCHAR2(10),             -- CRITICAL / WARNING
   expected_val   NUMBER(16,2),
   actual_val     NUMBER(16,2),
   diff_val       NUMBER(16,2),
   detail         VARCHAR2(400),
   CONSTRAINT pk_recon_break PRIMARY KEY (break_id),
   CONSTRAINT fk_rcn_run FOREIGN KEY (run_id) REFERENCES eod_run (run_id)
);

COMMENT ON TABLE recon_break IS
   'Reconciliation differences found at end of day. A CRITICAL break aborts the batch.';

CREATE TABLE reg_daily_snapshot (
   snapshot_id    NUMBER(12)   NOT NULL,
   run_id         NUMBER(12)   NOT NULL,
   business_date  DATE         NOT NULL,
   metric_code    VARCHAR2(30),
   metric_value   NUMBER(18,2),
   currency       VARCHAR2(3),
   CONSTRAINT pk_reg_daily_snapshot PRIMARY KEY (snapshot_id),
   CONSTRAINT uq_reg_metric UNIQUE (run_id, metric_code),
   CONSTRAINT fk_reg_run FOREIGN KEY (run_id) REFERENCES eod_run (run_id)
);

COMMENT ON TABLE reg_daily_snapshot IS
   'Daily prudential metrics extracted at end of day for regulatory reporting.';

CREATE SEQUENCE eod_run_seq   START WITH 1      NOCACHE;
CREATE SEQUENCE eod_step_seq  START WITH 1      NOCACHE;
CREATE SEQUENCE clearing_seq  START WITH 800000 NOCACHE;
CREATE SEQUENCE gl_seq        START WITH 1      NOCACHE;
CREATE SEQUENCE accrual_seq   START WITH 1      NOCACHE;
CREATE SEQUENCE fee_seq       START WITH 1      NOCACHE;
CREATE SEQUENCE schedule_seq  START WITH 1      NOCACHE;
CREATE SEQUENCE arrears_seq   START WITH 1      NOCACHE;
CREATE SEQUENCE aml_seq       START WITH 1      NOCACHE;
CREATE SEQUENCE recon_seq     START WITH 1      NOCACHE;
CREATE SEQUENCE reg_seq       START WITH 1      NOCACHE;

-- ------------------------------------------------------------------- batch parameters
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('SAVINGS_RATE_PCT', '1.750', 'Annual credit interest paid on SAVINGS accounts (%).');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('OVERDRAFT_FEE', '15.00', 'Flat daily fee charged on an account left overdrawn.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('MAINTENANCE_FEE', '4.50', 'Monthly account maintenance fee, charged on month end.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('MAINTENANCE_MIN_BALANCE', '2500', 'Maintenance fee is waived above this balance.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('AML_HIGH_VALUE', '50000', 'Single-transaction amount that triggers rule AML-02.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('AML_STRUCT_THRESHOLD', '10000', 'Reporting threshold that rule AML-01 looks for splitting under.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('AML_DORMANT_DAYS', '90', 'Days of inactivity after which rule AML-03 calls an account dormant.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('FIRST_BUSINESS_DATE_OFFSET', '4', 'Days before today that the very first run books.');
INSERT INTO eod_param (param_key, param_value, description) VALUES
   ('FORCE_FAIL_STEP', NULL, 'Test hook: name a step here and it raises, to exercise the error branch.');
COMMIT;

-- ------------------------------------------------------- seed the amortisation schedule
-- 24 monthly instalments per loan, straight-line principal plus flat-rate interest.
--
-- Each loan is given an age and a number of unpaid months, chosen so that the three
-- IFRS 9 stages are all represented on the very first run rather than only in theory:
--
--   7001  opened 20 months ago, paid up to date  -> stage 1, stays ACTIVE
--   7002  opened 14 months ago, 2 months unpaid  -> stage 2, DELINQUENT
--   7003  opened 22 months ago, 5 months unpaid  -> stage 3, NPL and non-accrual
--
-- The exact bucket a loan lands in drifts as the business date advances night after
-- night, which is the point: the arrears position is recomputed every run.
--
-- LOANS.OPENED_AT is rewritten to the same origination date the schedule is built from,
-- so the two tables agree. See the header note about this being the one place the batch
-- install touches bank_demo.sql's data.
DECLARE
   v_n CONSTANT NUMBER := 24;

   TYPE t_seed IS RECORD (loan_id NUMBER, age_months NUMBER, unpaid_months NUMBER);
   TYPE t_seeds IS TABLE OF t_seed;
   v_seeds t_seeds := t_seeds(t_seed(7001, 20, 0),
                              t_seed(7002, 14, 2),
                              t_seed(7003, 22, 5));

   v_opened    DATE;
   v_principal NUMBER;
   v_interest  NUMBER;
   v_rate      NUMBER;
   v_amount    NUMBER;
   v_cutoff    DATE;
BEGIN
   FOR s IN 1 .. v_seeds.COUNT LOOP
      BEGIN
         SELECT principal, NVL(rate_pct, 0) INTO v_amount, v_rate
           FROM loans WHERE loan_id = v_seeds(s).loan_id;
      EXCEPTION
         WHEN NO_DATA_FOUND THEN CONTINUE;   -- loan not in this copy of the demo data
      END;

      v_opened := ADD_MONTHS(TRUNC(SYSDATE), -1 * v_seeds(s).age_months);
      UPDATE loans SET opened_at = v_opened WHERE loan_id = v_seeds(s).loan_id;

      v_principal := ROUND(v_amount / v_n, 2);
      v_interest  := ROUND(v_amount * (v_rate / 100) / 12, 2);

      FOR i IN 1 .. v_n LOOP
         INSERT INTO loan_schedule
            (schedule_id, loan_id, installment_no, due_date,
             principal_due, interest_due, paid_amt, status)
         VALUES
            (schedule_seq.NEXTVAL, v_seeds(s).loan_id, i, ADD_MONTHS(v_opened, i),
             v_principal, v_interest, 0, 'DUE');
      END LOOP;

      -- Everything older than the unpaid window is settled; what is left inside the
      -- window is the arrears the batch will age tonight.
      v_cutoff := ADD_MONTHS(TRUNC(SYSDATE), -1 * v_seeds(s).unpaid_months);
      UPDATE loan_schedule
         SET paid_amt = principal_due + interest_due,
             status   = 'PAID'
       WHERE loan_id = v_seeds(s).loan_id
         AND due_date <= v_cutoff;
   END LOOP;
   COMMIT;
END;
/

-- ==================================================================== EOD_BATCH package

CREATE OR REPLACE PACKAGE eod_batch AS
   -- Batch error codes (ORA-20101..20106), so a caller can tell why the chain stopped.
   e_run_in_flight  EXCEPTION; PRAGMA EXCEPTION_INIT(e_run_in_flight,  -20101);
   e_no_open_run    EXCEPTION; PRAGMA EXCEPTION_INIT(e_no_open_run,    -20102);
   e_gl_unbalanced  EXCEPTION; PRAGMA EXCEPTION_INIT(e_gl_unbalanced,  -20103);
   e_recon_critical EXCEPTION; PRAGMA EXCEPTION_INIT(e_recon_critical, -20104);
   e_forced_failure EXCEPTION; PRAGMA EXCEPTION_INIT(e_forced_failure, -20105);

   FUNCTION current_run_id  RETURN NUMBER;
   FUNCTION business_date   RETURN DATE;
   FUNCTION param(p_key IN VARCHAR2) RETURN VARCHAR2;
   FUNCTION param_num(p_key IN VARCHAR2, p_default IN NUMBER DEFAULT 0) RETURN NUMBER;
   PROCEDURE set_param(p_key IN VARCHAR2, p_value IN VARCHAR2);

   -- One procedure per chain step, in dependency order.
   PROCEDURE s01_open_business_date;
   PROCEDURE s02_ingest_clearing;
   PROCEDURE s03_reset_card_limits;
   PROCEDURE s04_settle_pending;
   PROCEDURE s05_accrue_interest;
   PROCEDURE s06_apply_fees;
   PROCEDURE s07_aml_screen;
   PROCEDURE s08_delinquency;
   PROCEDURE s09_reconcile;
   PROCEDURE s10_regulatory_extract;
   PROCEDURE s11_close_batch;
   PROCEDURE s99_fail_handler;
END eod_batch;
/

CREATE OR REPLACE PACKAGE BODY eod_batch AS

   -- Each chain step runs in its own session and its own transaction, so nothing can be
   -- carried in package state between steps. The open EOD_RUN row is the only handoff.

   -- ------------------------------------------------------------------- infrastructure

   -- Mirrors CORE_BANKING.LOG_ERROR: autonomous, so the failure survives the rollback of
   -- the business transaction that caused it.
   PROCEDURE log_error(p_severity IN VARCHAR2, p_code IN NUMBER,
                       p_module IN VARCHAR2, p_message IN VARCHAR2) IS
      PRAGMA AUTONOMOUS_TRANSACTION;
   BEGIN
      INSERT INTO applog.error_log
         (error_id, log_time, severity, error_code, module, message, db_user, order_id)
      VALUES
         (applog.error_seq.NEXTVAL, SYSTIMESTAMP, p_severity, p_code,
          SUBSTR(p_module, 1, 80), SUBSTR(p_message, 1, 2000), USER, NULL);
      COMMIT;
   END log_error;

   PROCEDURE log_step(p_step IN VARCHAR2, p_started IN TIMESTAMP, p_status IN VARCHAR2,
                      p_rows IN NUMBER, p_message IN VARCHAR2) IS
      PRAGMA AUTONOMOUS_TRANSACTION;
      v_run NUMBER;
   BEGIN
      SELECT MAX(run_id) INTO v_run FROM eod_run WHERE status = 'RUNNING';
      IF v_run IS NULL THEN
         RETURN;   -- nothing to attach the step to; the error log already has the detail
      END IF;
      INSERT INTO eod_step_log
         (step_log_id, run_id, step_name, started_at, finished_at, status, rows_affected, message)
      VALUES
         (eod_step_seq.NEXTVAL, v_run, p_step, p_started, SYSTIMESTAMP, p_status,
          p_rows, SUBSTR(p_message, 1, 400));

      IF p_status = 'SUCCESS' THEN
         UPDATE eod_run
            SET steps_ok       = steps_ok + 1,
                rows_processed = rows_processed + NVL(p_rows, 0)
          WHERE run_id = v_run;
      ELSE
         UPDATE eod_run SET steps_failed = steps_failed + 1 WHERE run_id = v_run;
      END IF;
      COMMIT;
   END log_step;

   FUNCTION current_run_id RETURN NUMBER IS
      v_run NUMBER;
   BEGIN
      SELECT MAX(run_id) INTO v_run FROM eod_run WHERE status = 'RUNNING';
      IF v_run IS NULL THEN
         raise_application_error(-20102, 'No end-of-day run is open.');
      END IF;
      RETURN v_run;
   END current_run_id;

   FUNCTION business_date RETURN DATE IS
      v_dt DATE;
   BEGIN
      SELECT business_date INTO v_dt FROM eod_run WHERE run_id = current_run_id;
      RETURN v_dt;
   END business_date;

   FUNCTION param(p_key IN VARCHAR2) RETURN VARCHAR2 IS
      v_val VARCHAR2(100);
   BEGIN
      SELECT param_value INTO v_val FROM eod_param WHERE param_key = p_key;
      RETURN v_val;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN RETURN NULL;
   END param;

   FUNCTION param_num(p_key IN VARCHAR2, p_default IN NUMBER DEFAULT 0) RETURN NUMBER IS
   BEGIN
      RETURN NVL(TO_NUMBER(param(p_key)), p_default);
   EXCEPTION
      WHEN VALUE_ERROR THEN RETURN p_default;
   END param_num;

   PROCEDURE set_param(p_key IN VARCHAR2, p_value IN VARCHAR2) IS
   BEGIN
      UPDATE eod_param SET param_value = p_value, updated_at = SYSDATE WHERE param_key = p_key;
      IF SQL%ROWCOUNT = 0 THEN
         INSERT INTO eod_param (param_key, param_value) VALUES (p_key, p_value);
      END IF;
      COMMIT;
   END set_param;

   -- The test hook that lets the error branch be demonstrated on demand.
   PROCEDURE check_force_fail(p_step IN VARCHAR2) IS
   BEGIN
      IF UPPER(NVL(param('FORCE_FAIL_STEP'), '~')) = UPPER(p_step) THEN
         raise_application_error(-20105, 'Forced failure at ' || p_step
                                 || ' (EOD_PARAM.FORCE_FAIL_STEP test hook).');
      END IF;
   END check_force_fail;

   -- One leg of a double entry. Callers post both legs, so the ledger balances by
   -- construction and S09 verifies that it actually did.
   PROCEDURE post_gl(p_gl_code IN VARCHAR2, p_dr_cr IN VARCHAR2, p_amount IN NUMBER,
                     p_account_id IN NUMBER, p_step IN VARCHAR2, p_narrative IN VARCHAR2,
                     p_currency IN VARCHAR2 DEFAULT 'EUR') IS
   BEGIN
      IF NVL(p_amount, 0) = 0 THEN
         RETURN;
      END IF;
      INSERT INTO gl_entry
         (entry_id, run_id, business_date, gl_code, dr_cr, amount, currency,
          account_id, source_step, narrative)
      VALUES
         (gl_seq.NEXTVAL, current_run_id, business_date, p_gl_code, p_dr_cr,
          ROUND(ABS(p_amount), 2), p_currency, p_account_id, p_step,
          SUBSTR(p_narrative, 1, 200));
   END post_gl;

   -- ----------------------------------------------------------------- S01 open the day

   PROCEDURE s01_open_business_date IS
      v_started    TIMESTAMP := SYSTIMESTAMP;
      v_open       NUMBER;
      v_last_id    NUMBER;
      v_last_date  DATE;
      v_last_stat  VARCHAR2(12);
      v_last_att   NUMBER;
      v_date       DATE;
      v_attempt    NUMBER := 1;
      v_run        NUMBER;
      v_joblog     NUMBER;
   BEGIN
      -- A second concurrent run would double-post everything, so refuse outright.
      SELECT COUNT(*) INTO v_open FROM eod_run WHERE status = 'RUNNING';
      IF v_open > 0 THEN
         raise_application_error(-20101,
            'An end-of-day run is already open; close or fail it before starting another.');
      END IF;

      SELECT MAX(run_id) INTO v_last_id FROM eod_run;

      IF v_last_id IS NULL THEN
         -- First ever run: start a few days back so the demo has some history to build.
         v_date    := TRUNC(SYSDATE) - param_num('FIRST_BUSINESS_DATE_OFFSET', 4);
         v_attempt := 1;
      ELSE
         SELECT business_date, status, attempt_no
           INTO v_last_date, v_last_stat, v_last_att
           FROM eod_run WHERE run_id = v_last_id;

         IF v_last_stat = 'FAILED' THEN
            -- Retry the day that failed. Rolling forward instead would close the books
            -- on a date that was never actually processed, and nobody would notice.
            v_date    := v_last_date;
            v_attempt := v_last_att + 1;
         ELSE
            v_date    := v_last_date + 1;
            v_attempt := 1;
         END IF;
      END IF;

      v_run := eod_run_seq.NEXTVAL;

      -- Open the cross-schema batch log first, the way the rest of the app does.
      SELECT applog.job_seq.NEXTVAL INTO v_joblog FROM dual;
      INSERT INTO applog.job_run_log
         (run_id, job_name, started_at, finished_at, status, rows_processed, error_text)
      VALUES
         (v_joblog, 'BANKDEMO.EOD_CHAIN', SYSTIMESTAMP, NULL, 'RUNNING', 0, NULL);

      INSERT INTO eod_run
         (run_id, business_date, attempt_no, started_at, finished_at, status,
          steps_ok, steps_failed, rows_processed, job_log_id)
      VALUES
         (v_run, v_date, v_attempt, v_started, NULL, 'RUNNING', 0, 0, 0, v_joblog);
      COMMIT;

      log_step('S01_OPEN_BUSINESS_DATE', v_started, 'SUCCESS', 1,
               'Business date ' || TO_CHAR(v_date, 'YYYY-MM-DD') || ' opened as run '
               || v_run || CASE WHEN v_attempt > 1
                                THEN ' (retry, attempt ' || v_attempt || ')'
                                ELSE '' END);
      check_force_fail('S01_OPEN_BUSINESS_DATE');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('FATAL', SQLCODE, 'EOD_BATCH.S01_OPEN_BUSINESS_DATE', SQLERRM);
         log_step('S01_OPEN_BUSINESS_DATE', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s01_open_business_date;

   -- --------------------------------------------------- S02 inbound clearing file intake

   PROCEDURE s02_ingest_clearing IS
      v_started   TIMESTAMP := SYSTIMESTAMP;
      v_run       NUMBER := current_run_id;
      v_date      DATE   := business_date;
      v_file      VARCHAR2(40);
      v_count     PLS_INTEGER;
      v_rows      PLS_INTEGER := 0;
      v_unmatched PLS_INTEGER := 0;
      v_acct      NUMBER;
      v_amount    NUMBER;
      v_dir       VARCHAR2(2);
      v_scheme    VARCHAR2(12);
      v_txn       NUMBER;
      TYPE t_ids IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
      v_accts     t_ids;
      v_n_accts   PLS_INTEGER := 0;
      v_existing  PLS_INTEGER;
   BEGIN
      check_force_fail('S02_INGEST_CLEARING');

      -- On a retry of a failed night the file for this date is already in. Loading it a
      -- second time would create a duplicate set of pending items and credit every
      -- customer twice, so intake is keyed on the business date and reports a no-op.
      SELECT COUNT(*) INTO v_existing FROM clearing_item WHERE business_date = v_date;
      IF v_existing > 0 THEN
         log_step('S02_INGEST_CLEARING', v_started, 'SUCCESS', 0,
                  'Clearing file for ' || TO_CHAR(v_date, 'YYYY-MM-DD')
                  || ' already ingested (' || v_existing || ' items); intake skipped.');
         RETURN;
      END IF;

      -- Same business date always produces the same file, so a rerun is reproducible.
      DBMS_RANDOM.SEED(TO_NUMBER(TO_CHAR(v_date, 'YYYYMMDD')));
      v_file := 'SEPA-IN-' || TO_CHAR(v_date, 'YYYYMMDD') || '-001';

      FOR a IN (SELECT account_id FROM accounts WHERE status = 'ACTIVE' ORDER BY account_id) LOOP
         v_n_accts := v_n_accts + 1;
         v_accts(v_n_accts) := a.account_id;
      END LOOP;

      IF v_n_accts = 0 THEN
         log_step('S02_INGEST_CLEARING', v_started, 'SUCCESS', 0,
                  'No active accounts; empty clearing file accepted.');
         RETURN;
      END IF;

      v_count := 8 + TRUNC(DBMS_RANDOM.VALUE(0, 9));   -- 8..16 items in the file

      FOR i IN 1 .. v_count LOOP
         v_dir    := CASE WHEN DBMS_RANDOM.VALUE(0, 1) < 0.55 THEN 'CR' ELSE 'DR' END;
         v_scheme := CASE
                        WHEN v_dir = 'CR' THEN 'SEPA_CT'
                        WHEN DBMS_RANDOM.VALUE(0, 1) < 0.5 THEN 'SEPA_DD'
                        ELSE 'CARD'
                     END;
         v_amount := ROUND(DBMS_RANDOM.VALUE(20, 4200), 2);

         -- Roughly one item in nine carries an IBAN we hold no account for. Those are the
         -- ones that end up in suspense and show on the reconciliation report.
         IF DBMS_RANDOM.VALUE(0, 1) < 0.11 THEN
            v_acct := NULL;
         ELSE
            v_acct := v_accts(1 + TRUNC(DBMS_RANDOM.VALUE(0, v_n_accts)));
         END IF;

         -- Occasionally a large incoming transfer, so AML-02 has something to catch.
         IF v_dir = 'CR' AND DBMS_RANDOM.VALUE(0, 1) < 0.08 THEN
            v_amount := ROUND(DBMS_RANDOM.VALUE(51000, 90000), 2);
         END IF;

         v_txn := NULL;
         IF v_acct IS NOT NULL THEN
            -- Matched items become PENDING transactions for S04 to post.
            v_txn := txn_seq.NEXTVAL;
            INSERT INTO transactions
               (txn_id, account_id, txn_type, amount, balance_after, counterparty_acct,
                status, created_at, note)
            VALUES
               (v_txn, v_acct,
                CASE WHEN v_dir = 'CR' THEN 'CLEARING_CR' ELSE 'CLEARING_DR' END,
                v_amount, NULL, NULL, 'PENDING',
                CAST(v_date AS TIMESTAMP) + NUMTODSINTERVAL(i * 137, 'SECOND'),
                v_scheme || ' item ' || i || ' from ' || v_file);
         ELSE
            v_unmatched := v_unmatched + 1;
         END IF;

         INSERT INTO clearing_item
            (clearing_id, run_id, business_date, file_ref, scheme, direction, account_id,
             iban_masked, amount, currency, status, txn_id)
         VALUES
            (clearing_seq.NEXTVAL, v_run, v_date, v_file, v_scheme, v_dir, v_acct,
             'RO49****************' || LPAD(TRUNC(DBMS_RANDOM.VALUE(0, 10000)), 4, '0'),
             v_amount, 'EUR',
             CASE WHEN v_acct IS NULL THEN 'UNMATCHED' ELSE 'RECEIVED' END,
             v_txn);

         v_rows := v_rows + 1;
      END LOOP;

      COMMIT;
      log_step('S02_INGEST_CLEARING', v_started, 'SUCCESS', v_rows,
               v_file || ': ' || v_rows || ' items, ' || v_unmatched || ' unmatched');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S02_INGEST_CLEARING', SQLERRM);
         log_step('S02_INGEST_CLEARING', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s02_ingest_clearing;

   -- ------------------------------------------------------- S03 roll the card day over

   PROCEDURE s03_reset_card_limits IS
      v_started TIMESTAMP := SYSTIMESTAMP;
      v_date    DATE := business_date;
      v_reset   PLS_INTEGER;
      v_expired PLS_INTEGER;
   BEGIN
      check_force_fail('S03_RESET_CARD_LIMITS');

      -- Cards whose counter still belongs to an earlier day start the new day at zero.
      UPDATE cards
         SET daily_spent      = 0,
             daily_spent_date = v_date
       WHERE NVL(daily_spent_date, v_date - 1) < v_date;
      v_reset := SQL%ROWCOUNT;

      -- A card is dead the day after it expires; block it rather than let S04 authorise on it.
      UPDATE cards
         SET status = 'EXPIRED'
       WHERE status = 'ACTIVE'
         AND expires_on < v_date;
      v_expired := SQL%ROWCOUNT;

      COMMIT;
      log_step('S03_RESET_CARD_LIMITS', v_started, 'SUCCESS', v_reset + v_expired,
               v_reset || ' limit counters reset, ' || v_expired || ' cards expired');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S03_RESET_CARD_LIMITS', SQLERRM);
         log_step('S03_RESET_CARD_LIMITS', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s03_reset_card_limits;

   -- ----------------------------------------------------------- S04 settle the pendings

   PROCEDURE s04_settle_pending IS
      v_started   TIMESTAMP := SYSTIMESTAMP;
      v_date      DATE := business_date;
      v_posted    PLS_INTEGER := 0;
      v_rejected  PLS_INTEGER := 0;
      v_balance   NUMBER;
      v_status    VARCHAR2(12);
      v_signed    NUMBER;
   BEGIN
      check_force_fail('S04_SETTLE_PENDING');

      -- Settle in arrival order and lock the account row: two items on the same account
      -- must not race, and the running balance has to be applied in sequence.
      FOR t IN (SELECT txn_id, account_id, txn_type, amount
                  FROM transactions
                 WHERE status = 'PENDING'
                 ORDER BY txn_id) LOOP
         BEGIN
            SELECT balance, status INTO v_balance, v_status
              FROM accounts
             WHERE account_id = t.account_id
               FOR UPDATE;

            IF v_status <> 'ACTIVE' THEN
               UPDATE transactions
                  SET status = 'REJECTED',
                      note   = SUBSTR('Rejected at settlement: account ' || v_status
                                      || '. ' || note, 1, 200)
                WHERE txn_id = t.txn_id;
               UPDATE clearing_item
                  SET status = 'REJECTED'
                WHERE txn_id = t.txn_id;
               v_rejected := v_rejected + 1;
               log_error('WARN', -20003, 'EOD_BATCH.S04_SETTLE_PENDING',
                         'Settlement rejected for txn ' || t.txn_id || ': account '
                         || t.account_id || ' is ' || v_status);
            ELSE
               v_signed  := CASE WHEN t.txn_type = 'CLEARING_DR' THEN -t.amount
                                 ELSE t.amount END;
               v_balance := v_balance + v_signed;

               UPDATE accounts SET balance = v_balance WHERE account_id = t.account_id;
               UPDATE transactions
                  SET status = 'POSTED', balance_after = v_balance
                WHERE txn_id = t.txn_id;
               UPDATE clearing_item SET status = 'SETTLED' WHERE txn_id = t.txn_id;

               -- Money in: debit our cash, credit what we owe the customer. Money out
               -- is the mirror image.
               IF v_signed > 0 THEN
                  post_gl('1010', 'DR', v_signed, t.account_id, 'S04_SETTLE_PENDING',
                          'Clearing credit settled, txn ' || t.txn_id);
                  post_gl('2010', 'CR', v_signed, t.account_id, 'S04_SETTLE_PENDING',
                          'Customer deposit increased, txn ' || t.txn_id);
               ELSE
                  post_gl('2010', 'DR', v_signed, t.account_id, 'S04_SETTLE_PENDING',
                          'Customer deposit reduced, txn ' || t.txn_id);
                  post_gl('1010', 'CR', v_signed, t.account_id, 'S04_SETTLE_PENDING',
                          'Clearing debit settled, txn ' || t.txn_id);
               END IF;
               v_posted := v_posted + 1;
            END IF;
         EXCEPTION
            WHEN NO_DATA_FOUND THEN
               UPDATE transactions SET status = 'REJECTED' WHERE txn_id = t.txn_id;
               v_rejected := v_rejected + 1;
               log_error('ERROR', -20002, 'EOD_BATCH.S04_SETTLE_PENDING',
                         'Settlement found no account ' || t.account_id
                         || ' for txn ' || t.txn_id);
         END;
      END LOOP;

      -- Unmatched intake never reaches an account, so it parks in suspense until an
      -- operator repairs it. Both legs are booked so the ledger still balances.
      FOR c IN (SELECT clearing_id, direction, amount
                  FROM clearing_item
                 WHERE business_date = v_date
                   AND status = 'UNMATCHED') LOOP
         IF c.direction = 'CR' THEN
            post_gl('1010', 'DR', c.amount, NULL, 'S04_SETTLE_PENDING',
                    'Unmatched credit to suspense, item ' || c.clearing_id);
            post_gl('9999', 'CR', c.amount, NULL, 'S04_SETTLE_PENDING',
                    'Suspense open, item ' || c.clearing_id);
         ELSE
            post_gl('9999', 'DR', c.amount, NULL, 'S04_SETTLE_PENDING',
                    'Suspense open, item ' || c.clearing_id);
            post_gl('1010', 'CR', c.amount, NULL, 'S04_SETTLE_PENDING',
                    'Unmatched debit to suspense, item ' || c.clearing_id);
         END IF;
      END LOOP;

      COMMIT;
      log_step('S04_SETTLE_PENDING', v_started, 'SUCCESS', v_posted + v_rejected,
               v_posted || ' posted, ' || v_rejected || ' rejected');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('FATAL', SQLCODE, 'EOD_BATCH.S04_SETTLE_PENDING', SQLERRM);
         log_step('S04_SETTLE_PENDING', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s04_settle_pending;

   -- --------------------------------------------------------------- S05 interest accrual

   PROCEDURE s05_accrue_interest IS
      v_started    TIMESTAMP := SYSTIMESTAMP;
      v_date       DATE := business_date;
      v_run        NUMBER := current_run_id;
      v_rows       PLS_INTEGER := 0;
      v_daily      NUMBER;
      v_sav_rate   NUMBER := param_num('SAVINGS_RATE_PCT', 1.75);
      v_loan_total NUMBER := 0;
      v_sav_total  NUMBER := 0;
      v_skipped    PLS_INTEGER := 0;
      v_suspended  PLS_INTEGER := 0;
      v_existing   PLS_INTEGER;
   BEGIN
      check_force_fail('S05_ACCRUE_INTEREST');

      -- Interest is owed once per day. On a retry the accrual for this date is already
      -- booked, and running it again would take a second day of income to the ledger.
      SELECT COUNT(*) INTO v_existing FROM interest_accrual WHERE business_date = v_date;
      IF v_existing > 0 THEN
         log_step('S05_ACCRUE_INTEREST', v_started, 'SUCCESS', 0,
                  'Interest for ' || TO_CHAR(v_date, 'YYYY-MM-DD')
                  || ' already accrued (' || v_existing || ' rows); accrual skipped.');
         RETURN;
      END IF;

      -- Loans: one day of interest on the outstanding balance, ACT/365.
      FOR l IN (SELECT loan_id, customer_id, outstanding, rate_pct, status
                  FROM loans
                 WHERE status <> 'CLOSED'
                   AND NVL(outstanding, 0) > 0) LOOP

         -- A loan classified non-performing goes on non-accrual: interest still exists
         -- contractually but the bank stops taking it to income, because it no longer
         -- expects to collect it. The accrual row is kept at zero so the suspension is
         -- visible rather than looking like the loan was simply missed.
         IF l.status = 'NPL' THEN
            INSERT INTO interest_accrual
               (accrual_id, run_id, business_date, accrual_type, loan_id, account_id,
                basis_amount, rate_pct, day_count, accrued_amt)
            VALUES
               (accrual_seq.NEXTVAL, v_run, v_date, 'LOAN', l.loan_id, NULL,
                l.outstanding, l.rate_pct, 'ACT/365', 0);

            v_suspended := v_suspended + 1;
            v_rows      := v_rows + 1;
            CONTINUE;
         END IF;

         -- A loan with no rate cannot be accrued. Skipping it silently would understate
         -- interest income and leave nobody any the wiser, so it is booked at zero and
         -- reported as a data-quality exception instead. One bad row must not stop the
         -- whole bank's close: it goes on the exception report and the day carries on.
         IF l.rate_pct IS NULL OR l.rate_pct <= 0 THEN
            INSERT INTO interest_accrual
               (accrual_id, run_id, business_date, accrual_type, loan_id, account_id,
                basis_amount, rate_pct, day_count, accrued_amt)
            VALUES
               (accrual_seq.NEXTVAL, v_run, v_date, 'LOAN', l.loan_id, NULL,
                l.outstanding, l.rate_pct, 'ACT/365', 0);

            log_error('WARN', -20001, 'EOD_BATCH.S05_ACCRUE_INTEREST',
                      'Loan ' || l.loan_id || ' has no usable interest rate ('
                      || NVL(TO_CHAR(l.rate_pct), 'NULL') || '); accrued 0 on '
                      || TO_CHAR(l.outstanding, 'FM999999990.00')
                      || ' outstanding. Fix LOANS.RATE_PCT.');
            v_skipped := v_skipped + 1;
            v_rows    := v_rows + 1;
            CONTINUE;
         END IF;

         v_daily := ROUND(l.outstanding * (l.rate_pct / 100) / 365, 4);

         INSERT INTO interest_accrual
            (accrual_id, run_id, business_date, accrual_type, loan_id, account_id,
             basis_amount, rate_pct, day_count, accrued_amt)
         VALUES
            (accrual_seq.NEXTVAL, v_run, v_date, 'LOAN', l.loan_id, NULL,
             l.outstanding, l.rate_pct, 'ACT/365', v_daily);

         -- Interest earned but not yet received is an asset, matched by income.
         post_gl('1210', 'DR', v_daily, NULL, 'S05_ACCRUE_INTEREST',
                 'Interest receivable, loan ' || l.loan_id);
         post_gl('4010', 'CR', v_daily, NULL, 'S05_ACCRUE_INTEREST',
                 'Interest income, loan ' || l.loan_id);

         v_loan_total := v_loan_total + NVL(v_daily, 0);
         v_rows := v_rows + 1;
      END LOOP;

      -- Savings: credit interest we owe the customer, same day-count convention.
      FOR a IN (SELECT account_id, balance, currency
                  FROM accounts
                 WHERE account_type = 'SAVINGS'
                   AND status = 'ACTIVE'
                   AND NVL(balance, 0) > 0) LOOP
         v_daily := ROUND(a.balance * (v_sav_rate / 100) / 365, 4);

         INSERT INTO interest_accrual
            (accrual_id, run_id, business_date, accrual_type, loan_id, account_id,
             basis_amount, rate_pct, day_count, accrued_amt)
         VALUES
            (accrual_seq.NEXTVAL, v_run, v_date, 'SAVINGS', NULL, a.account_id,
             a.balance, v_sav_rate, 'ACT/365', v_daily);

         post_gl('5010', 'DR', v_daily, a.account_id, 'S05_ACCRUE_INTEREST',
                 'Interest expense, account ' || a.account_id, NVL(a.currency, 'EUR'));
         post_gl('2010', 'CR', v_daily, a.account_id, 'S05_ACCRUE_INTEREST',
                 'Credit interest owed, account ' || a.account_id, NVL(a.currency, 'EUR'));

         v_sav_total := v_sav_total + NVL(v_daily, 0);
         v_rows := v_rows + 1;
      END LOOP;

      COMMIT;
      log_step('S05_ACCRUE_INTEREST', v_started, 'SUCCESS', v_rows,
               'Loan accrual ' || TO_CHAR(ROUND(v_loan_total, 2), 'FM999999990.00')
               || ', savings accrual ' || TO_CHAR(ROUND(v_sav_total, 2), 'FM999999990.00')
               || CASE WHEN v_suspended > 0
                       THEN ' - ' || v_suspended || ' NPL loan(s) on non-accrual'
                       ELSE '' END
               || CASE WHEN v_skipped > 0
                       THEN ' - ' || v_skipped || ' loan(s) skipped, no rate on file'
                       ELSE '' END);
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S05_ACCRUE_INTEREST', SQLERRM);
         log_step('S05_ACCRUE_INTEREST', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s05_accrue_interest;

   -- ------------------------------------------------------------------- S06 charge fees

   PROCEDURE s06_apply_fees IS
      v_started    TIMESTAMP := SYSTIMESTAMP;
      v_date       DATE := business_date;
      v_run        NUMBER := current_run_id;
      v_od_fee     NUMBER := param_num('OVERDRAFT_FEE', 15);
      v_mt_fee     NUMBER := param_num('MAINTENANCE_FEE', 4.5);
      v_mt_min     NUMBER := param_num('MAINTENANCE_MIN_BALANCE', 2500);
      v_month_end  BOOLEAN := (v_date = LAST_DAY(v_date));
      v_charged    PLS_INTEGER := 0;
      v_waived     PLS_INTEGER := 0;
      v_txn        NUMBER;
      v_new_bal    NUMBER;
      v_existing   PLS_INTEGER;

      -- Booking a fee moves money out of the customer's balance into fee income, and
      -- leaves a transaction behind so the customer can see it on the statement.
      PROCEDURE charge(p_account_id IN NUMBER, p_code IN VARCHAR2, p_amount IN NUMBER,
                       p_reason IN VARCHAR2) IS
      BEGIN
         SELECT balance - p_amount INTO v_new_bal
           FROM accounts WHERE account_id = p_account_id FOR UPDATE;

         v_txn := txn_seq.NEXTVAL;
         INSERT INTO transactions
            (txn_id, account_id, txn_type, amount, balance_after, counterparty_acct,
             status, created_at, note)
         VALUES
            (v_txn, p_account_id, 'FEE', p_amount, v_new_bal, NULL, 'POSTED',
             CAST(v_date AS TIMESTAMP), p_code || ' fee');

         UPDATE accounts SET balance = v_new_bal WHERE account_id = p_account_id;

         INSERT INTO fee_charge
            (fee_id, run_id, business_date, account_id, fee_code, amount,
             waived_yn, reason, txn_id)
         VALUES
            (fee_seq.NEXTVAL, v_run, v_date, p_account_id, p_code, p_amount,
             'N', p_reason, v_txn);

         post_gl('2010', 'DR', p_amount, p_account_id, 'S06_APPLY_FEES',
                 p_code || ' fee charged, account ' || p_account_id);
         post_gl('4020', 'CR', p_amount, p_account_id, 'S06_APPLY_FEES',
                 p_code || ' fee income, account ' || p_account_id);
         v_charged := v_charged + 1;
      END charge;

      PROCEDURE waive(p_account_id IN NUMBER, p_code IN VARCHAR2, p_amount IN NUMBER,
                      p_reason IN VARCHAR2) IS
      BEGIN
         INSERT INTO fee_charge
            (fee_id, run_id, business_date, account_id, fee_code, amount,
             waived_yn, reason, txn_id)
         VALUES
            (fee_seq.NEXTVAL, v_run, v_date, p_account_id, p_code, p_amount,
             'Y', p_reason, NULL);
         v_waived := v_waived + 1;
      END waive;
   BEGIN
      check_force_fail('S06_APPLY_FEES');

      -- A customer must not be charged the same day's fee twice because the batch was
      -- re-run, so fees are keyed on the business date like the other money-moving steps.
      SELECT COUNT(*) INTO v_existing FROM fee_charge WHERE business_date = v_date;
      IF v_existing > 0 THEN
         log_step('S06_APPLY_FEES', v_started, 'SUCCESS', 0,
                  'Fees for ' || TO_CHAR(v_date, 'YYYY-MM-DD')
                  || ' already applied (' || v_existing || ' rows); charging skipped.');
         RETURN;
      END IF;

      -- Overdraft fee: charged every day the account is left below zero.
      FOR a IN (SELECT account_id, balance
                  FROM accounts
                 WHERE status = 'ACTIVE'
                   AND NVL(balance, 0) < 0) LOOP
         charge(a.account_id, 'OVERDRAFT', v_od_fee,
                'Account overdrawn at ' || TO_CHAR(a.balance, 'FM999999990.00'));
      END LOOP;

      -- Maintenance fee: month end only, and waived for balances above the floor.
      IF v_month_end THEN
         FOR a IN (SELECT account_id, balance
                     FROM accounts
                    WHERE status = 'ACTIVE'
                      AND account_type = 'CHECKING') LOOP
            IF NVL(a.balance, 0) >= v_mt_min THEN
               waive(a.account_id, 'MAINTENANCE', v_mt_fee,
                     'Waived: balance at or above ' || TO_CHAR(v_mt_min, 'FM999999990'));
            ELSE
               charge(a.account_id, 'MAINTENANCE', v_mt_fee, 'Monthly account maintenance');
            END IF;
         END LOOP;
      END IF;

      COMMIT;
      log_step('S06_APPLY_FEES', v_started, 'SUCCESS', v_charged + v_waived,
               v_charged || ' fees charged, ' || v_waived || ' waived'
               || CASE WHEN v_month_end THEN ' (month end)' ELSE '' END);
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S06_APPLY_FEES', SQLERRM);
         log_step('S06_APPLY_FEES', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s06_apply_fees;

   -- ---------------------------------------------------------------- S07 AML screening

   PROCEDURE s07_aml_screen IS
      v_started   TIMESTAMP := SYSTIMESTAMP;
      v_date      DATE := business_date;
      v_run       NUMBER := current_run_id;
      v_high      NUMBER := param_num('AML_HIGH_VALUE', 50000);
      v_struct    NUMBER := param_num('AML_STRUCT_THRESHOLD', 10000);
      v_dormant   NUMBER := param_num('AML_DORMANT_DAYS', 90);
      v_alerts    PLS_INTEGER := 0;

      PROCEDURE raise_alert(p_rule IN VARCHAR2, p_sev IN VARCHAR2, p_account IN NUMBER,
                            p_txn IN NUMBER, p_amount IN NUMBER, p_detail IN VARCHAR2) IS
         v_cust NUMBER;
      BEGIN
         BEGIN
            SELECT customer_id INTO v_cust FROM accounts WHERE account_id = p_account;
         EXCEPTION
            WHEN NO_DATA_FOUND THEN v_cust := NULL;
         END;
         INSERT INTO aml_alert
            (alert_id, run_id, business_date, rule_code, severity, account_id,
             customer_id, txn_id, amount, detail, status)
         VALUES
            (aml_seq.NEXTVAL, v_run, v_date, p_rule, p_sev, p_account, v_cust, p_txn,
             p_amount, SUBSTR(p_detail, 1, 400), 'OPEN');
         v_alerts := v_alerts + 1;
      END raise_alert;
   BEGIN
      check_force_fail('S07_AML_SCREEN');

      -- AML-02 high value: any single settled credit at or above the reporting amount.
      FOR t IN (SELECT txn_id, account_id, amount
                  FROM transactions
                 WHERE status = 'POSTED'
                   AND TRUNC(created_at) = v_date
                   AND amount >= v_high) LOOP
         raise_alert('AML-02', 'HIGH', t.account_id, t.txn_id, t.amount,
                     'Single transaction of ' || TO_CHAR(t.amount, 'FM999999990.00')
                     || ' at or above the ' || TO_CHAR(v_high, 'FM999999990')
                     || ' reporting threshold.');
      END LOOP;

      -- AML-01 structuring: several same-day credits each deliberately under the
      -- threshold, adding up to more than it. The classic smurfing pattern.
      FOR s IN (SELECT account_id, COUNT(*) n, SUM(amount) total
                  FROM transactions
                 WHERE status = 'POSTED'
                   AND TRUNC(created_at) = v_date
                   AND amount < v_struct
                   AND txn_type IN ('DEPOSIT', 'CLEARING_CR')
                 GROUP BY account_id
                HAVING COUNT(*) >= 3 AND SUM(amount) >= v_struct) LOOP
         raise_alert('AML-01', 'HIGH', s.account_id, NULL, s.total,
                     s.n || ' same-day credits each below '
                     || TO_CHAR(v_struct, 'FM999999990') || ' totalling '
                     || TO_CHAR(s.total, 'FM999999990.00') || ' - possible structuring.');
      END LOOP;

      -- AML-03 dormant reactivation: an account silent for months that suddenly moves.
      FOR d IN (SELECT t.account_id, MAX(t.txn_id) txn_id, SUM(t.amount) total,
                       (SELECT MAX(TRUNC(p.created_at))
                          FROM transactions p
                         WHERE p.account_id = t.account_id
                           AND TRUNC(p.created_at) < v_date) last_seen
                  FROM transactions t
                 WHERE t.status = 'POSTED'
                   AND TRUNC(t.created_at) = v_date
                 GROUP BY t.account_id) LOOP
         IF d.last_seen IS NOT NULL AND v_date - d.last_seen >= v_dormant THEN
            raise_alert('AML-03', 'MEDIUM', d.account_id, d.txn_id, d.total,
                        'Account dormant since ' || TO_CHAR(d.last_seen, 'YYYY-MM-DD')
                        || ' (' || TO_CHAR(v_date - d.last_seen)
                        || ' days) reactivated today.');
         END IF;
      END LOOP;

      -- AML-04 pass-through: money in and almost all of it straight back out same day.
      FOR p IN (SELECT account_id,
                       SUM(CASE WHEN txn_type IN ('DEPOSIT', 'CLEARING_CR')
                                THEN amount ELSE 0 END) credits,
                       SUM(CASE WHEN txn_type IN ('CARD', 'CLEARING_DR')
                                THEN amount ELSE 0 END) debits
                  FROM transactions
                 WHERE status = 'POSTED'
                   AND TRUNC(created_at) = v_date
                 GROUP BY account_id) LOOP
         IF p.credits > 0 AND p.debits >= 0.8 * p.credits AND p.credits >= v_struct THEN
            raise_alert('AML-04', 'MEDIUM', p.account_id, NULL, p.credits,
                        'Pass-through: ' || TO_CHAR(p.credits, 'FM999999990.00')
                        || ' in, ' || TO_CHAR(p.debits, 'FM999999990.00')
                        || ' out on the same day.');
         END IF;
      END LOOP;

      COMMIT;
      log_step('S07_AML_SCREEN', v_started, 'SUCCESS', v_alerts,
               v_alerts || ' alert(s) raised across rules AML-01..AML-04');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S07_AML_SCREEN', SQLERRM);
         log_step('S07_AML_SCREEN', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s07_aml_screen;

   -- --------------------------------------------------- S08 arrears and IFRS 9 staging

   PROCEDURE s08_delinquency IS
      v_started    TIMESTAMP := SYSTIMESTAMP;
      v_date       DATE := business_date;
      v_run        NUMBER := current_run_id;
      v_rows       PLS_INTEGER := 0;
      v_dpd        NUMBER;
      v_bucket     VARCHAR2(12);
      v_stage      NUMBER;
      v_rate       NUMBER;
      v_prov       NUMBER;
      v_oldest     DATE;
      v_new_status VARCHAR2(12);
   BEGIN
      check_force_fail('S08_DELINQUENCY');

      -- Anything past its due date and still unpaid is overdue as of tonight.
      UPDATE loan_schedule
         SET status = 'OVERDUE'
       WHERE status = 'DUE'
         AND due_date < v_date;

      FOR l IN (SELECT loan_id, outstanding, status FROM loans WHERE status <> 'CLOSED') LOOP
         SELECT MIN(due_date) INTO v_oldest
           FROM loan_schedule
          WHERE loan_id = l.loan_id
            AND status = 'OVERDUE';

         v_dpd := CASE WHEN v_oldest IS NULL THEN 0 ELSE GREATEST(v_date - v_oldest, 0) END;

         -- Standard supervisory buckets; stage 2 is "significant increase in credit
         -- risk", stage 3 is credit-impaired, which is also where a loan becomes an NPL.
         IF v_dpd = 0 THEN
            v_bucket := 'CURRENT';    v_stage := 1; v_rate := 0.01;
         ELSIF v_dpd <= 30 THEN
            v_bucket := 'DPD1_30';    v_stage := 2; v_rate := 0.05;
         ELSIF v_dpd <= 60 THEN
            v_bucket := 'DPD31_60';   v_stage := 2; v_rate := 0.15;
         ELSIF v_dpd <= 90 THEN
            v_bucket := 'DPD61_90';   v_stage := 2; v_rate := 0.30;
         ELSE
            v_bucket := 'DPD90_PLUS'; v_stage := 3; v_rate := 0.60;
         END IF;

         v_prov := ROUND(NVL(l.outstanding, 0) * v_rate, 2);

         INSERT INTO loan_arrears
            (arrears_id, run_id, business_date, loan_id, days_past_due, bucket,
             ifrs9_stage, outstanding, provision_amt)
         VALUES
            (arrears_seq.NEXTVAL, v_run, v_date, l.loan_id, v_dpd, v_bucket,
             v_stage, l.outstanding, v_prov);

         v_new_status := CASE
                            WHEN v_stage = 3 THEN 'NPL'
                            WHEN v_stage = 2 THEN 'DELINQUENT'
                            ELSE 'ACTIVE'
                         END;
         IF l.status <> v_new_status THEN
            UPDATE loans SET status = v_new_status WHERE loan_id = l.loan_id;
            log_error('WARN', -20011, 'EOD_BATCH.S08_DELINQUENCY',
                      'Loan ' || l.loan_id || ' moved ' || l.status || ' -> '
                      || v_new_status || ' at ' || v_dpd || ' days past due');
         END IF;

         v_rows := v_rows + 1;
      END LOOP;

      COMMIT;
      log_step('S08_DELINQUENCY', v_started, 'SUCCESS', v_rows,
               v_rows || ' loan(s) classified into arrears buckets');
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S08_DELINQUENCY', SQLERRM);
         log_step('S08_DELINQUENCY', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s08_delinquency;

   -- --------------------------------------------------------------- S09 reconciliation

   PROCEDURE s09_reconcile IS
      v_started   TIMESTAMP := SYSTIMESTAMP;
      v_date      DATE := business_date;
      v_run       NUMBER := current_run_id;
      v_dr        NUMBER;
      v_cr        NUMBER;
      v_breaks    PLS_INTEGER := 0;
      v_critical  PLS_INTEGER := 0;
      v_suspense  NUMBER;
      v_stmt      NUMBER;

      PROCEDURE add_break(p_code IN VARCHAR2, p_sev IN VARCHAR2, p_expected IN NUMBER,
                          p_actual IN NUMBER, p_detail IN VARCHAR2) IS
      BEGIN
         INSERT INTO recon_break
            (break_id, run_id, business_date, check_code, severity,
             expected_val, actual_val, diff_val, detail)
         VALUES
            (recon_seq.NEXTVAL, v_run, v_date, p_code, p_sev, p_expected, p_actual,
             ROUND(NVL(p_actual, 0) - NVL(p_expected, 0), 2), SUBSTR(p_detail, 1, 400));
         v_breaks := v_breaks + 1;
         IF p_sev = 'CRITICAL' THEN
            v_critical := v_critical + 1;
         END IF;
      END add_break;
   BEGIN
      check_force_fail('S09_RECONCILE');

      -- Check 1 - the ledger must balance. If it does not, the day cannot be closed.
      SELECT NVL(SUM(CASE WHEN dr_cr = 'DR' THEN amount END), 0),
             NVL(SUM(CASE WHEN dr_cr = 'CR' THEN amount END), 0)
        INTO v_dr, v_cr
        FROM gl_entry
       WHERE run_id = v_run;

      IF ROUND(v_dr, 2) <> ROUND(v_cr, 2) THEN
         add_break('GL_BALANCED', 'CRITICAL', v_dr, v_cr,
                   'General ledger out of balance for run ' || v_run || '.');
      END IF;

      -- Check 2 - for every account the batch touched tonight, the balance must equal
      -- what the last posting left behind. A mismatch means something moved a balance
      -- without leaving a transaction. Scoped to tonight on purpose: this control proves
      -- this run's postings, not the history that was already on the book.
      FOR a IN (SELECT a.account_id, a.balance,
                       (SELECT t.balance_after
                          FROM transactions t
                         WHERE t.account_id = a.account_id
                           AND t.status = 'POSTED'
                           AND t.balance_after IS NOT NULL
                           AND TRUNC(t.created_at) = v_date
                         ORDER BY t.txn_id DESC
                         FETCH FIRST 1 ROWS ONLY) last_after
                  FROM accounts a
                 WHERE a.status = 'ACTIVE') LOOP
         v_stmt := a.last_after;
         IF v_stmt IS NOT NULL AND ROUND(v_stmt, 2) <> ROUND(a.balance, 2) THEN
            add_break('STMT_VS_BALANCE', 'CRITICAL', v_stmt, a.balance,
                      'Account ' || a.account_id
                      || ' balance disagrees with its last posted transaction.');
         END IF;
      END LOOP;

      -- Check 3 - suspense should be empty by morning. Anything left is a warning for
      -- the operations team, not a reason to stop the batch.
      SELECT NVL(SUM(CASE WHEN dr_cr = 'DR' THEN amount ELSE -amount END), 0)
        INTO v_suspense
        FROM gl_entry
       WHERE run_id = v_run
         AND gl_code = '9999';

      IF ROUND(v_suspense, 2) <> 0 THEN
         add_break('SUSPENSE_OPEN', 'WARNING', 0, v_suspense,
                   'Suspense account 9999 carries a net '
                   || TO_CHAR(ROUND(v_suspense, 2), 'FM999999990.00')
                   || ' from unmatched clearing items awaiting repair.');
      END IF;

      COMMIT;

      IF v_critical > 0 THEN
         log_step('S09_RECONCILE', v_started, 'FAILED', v_breaks,
                  v_critical || ' critical break(s) of ' || v_breaks || ' total');
         raise_application_error(-20104,
            'Reconciliation failed with ' || v_critical
            || ' critical break(s); see BANKDEMO.RECON_BREAK for run ' || v_run || '.');
      END IF;

      log_step('S09_RECONCILE', v_started, 'SUCCESS', v_breaks,
               'Ledger balanced at ' || TO_CHAR(ROUND(v_dr, 2), 'FM999999990.00')
               || '; ' || v_breaks || ' warning break(s)');
   EXCEPTION
      WHEN OTHERS THEN
         log_error('FATAL', SQLCODE, 'EOD_BATCH.S09_RECONCILE', SQLERRM);
         IF SQLCODE <> -20104 THEN
            ROLLBACK;
            log_step('S09_RECONCILE', v_started, 'FAILED', 0, SQLERRM);
         END IF;
         RAISE;
   END s09_reconcile;

   -- --------------------------------------------------------- S10 regulatory extract

   PROCEDURE s10_regulatory_extract IS
      v_started TIMESTAMP := SYSTIMESTAMP;
      v_date    DATE := business_date;
      v_run     NUMBER := current_run_id;
      v_rows    PLS_INTEGER := 0;
      v_n       NUMBER;

      PROCEDURE metric(p_code IN VARCHAR2, p_value IN NUMBER,
                       p_ccy IN VARCHAR2 DEFAULT 'EUR') IS
      BEGIN
         INSERT INTO reg_daily_snapshot
            (snapshot_id, run_id, business_date, metric_code, metric_value, currency)
         VALUES
            (reg_seq.NEXTVAL, v_run, v_date, p_code, ROUND(NVL(p_value, 0), 2), p_ccy);
         v_rows := v_rows + 1;
      END metric;
   BEGIN
      check_force_fail('S10_REGULATORY_EXTRACT');

      SELECT NVL(SUM(balance), 0) INTO v_n FROM accounts WHERE status = 'ACTIVE';
      metric('CUSTOMER_DEPOSITS_TOTAL', v_n);

      SELECT NVL(SUM(outstanding), 0) INTO v_n FROM loans WHERE status <> 'CLOSED';
      metric('LOAN_BOOK_GROSS', v_n);

      SELECT NVL(SUM(outstanding), 0) INTO v_n FROM loans WHERE status = 'NPL';
      metric('NPL_EXPOSURE', v_n);

      SELECT NVL(SUM(provision_amt), 0) INTO v_n FROM loan_arrears WHERE run_id = v_run;
      metric('IFRS9_PROVISION_STOCK', v_n);

      SELECT NVL(SUM(accrued_amt), 0) INTO v_n
        FROM interest_accrual WHERE run_id = v_run AND accrual_type = 'LOAN';
      metric('INTEREST_ACCRUED_TODAY', v_n);

      SELECT NVL(SUM(amount), 0) INTO v_n
        FROM fee_charge WHERE run_id = v_run AND waived_yn = 'N';
      metric('FEE_INCOME_TODAY', v_n);

      SELECT COUNT(*) INTO v_n FROM aml_alert WHERE run_id = v_run;
      metric('AML_ALERTS_OPEN', v_n, NULL);

      SELECT COUNT(*) INTO v_n
        FROM clearing_item WHERE run_id = v_run AND status = 'UNMATCHED';
      metric('CLEARING_UNMATCHED', v_n, NULL);

      SELECT COUNT(*) INTO v_n FROM transactions WHERE TRUNC(created_at) = v_date;
      metric('TXN_VOLUME_TODAY', v_n, NULL);

      COMMIT;
      log_step('S10_REGULATORY_EXTRACT', v_started, 'SUCCESS', v_rows,
               v_rows || ' prudential metrics extracted for '
               || TO_CHAR(v_date, 'YYYY-MM-DD'));
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('ERROR', SQLCODE, 'EOD_BATCH.S10_REGULATORY_EXTRACT', SQLERRM);
         log_step('S10_REGULATORY_EXTRACT', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s10_regulatory_extract;

   -- ------------------------------------------------------------------ S11 close the day

   PROCEDURE s11_close_batch IS
      v_started TIMESTAMP := SYSTIMESTAMP;
      v_run     NUMBER := current_run_id;
      v_date    DATE   := business_date;
      v_rows    NUMBER;
      v_joblog  NUMBER;
   BEGIN
      check_force_fail('S11_CLOSE_BATCH');

      -- Count this step in before the header is frozen, so the totals include it.
      log_step('S11_CLOSE_BATCH', v_started, 'SUCCESS', 0,
               'Closing business date ' || TO_CHAR(v_date, 'YYYY-MM-DD'));

      SELECT rows_processed, job_log_id INTO v_rows, v_joblog
        FROM eod_run WHERE run_id = v_run;

      UPDATE eod_run
         SET status      = 'COMPLETED',
             finished_at = SYSTIMESTAMP
       WHERE run_id = v_run;

      UPDATE applog.job_run_log
         SET finished_at    = SYSTIMESTAMP,
             status         = 'SUCCESS',
             rows_processed = v_rows
       WHERE run_id = v_joblog;

      COMMIT;
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('FATAL', SQLCODE, 'EOD_BATCH.S11_CLOSE_BATCH', SQLERRM);
         log_step('S11_CLOSE_BATCH', v_started, 'FAILED', 0, SQLERRM);
         RAISE;
   END s11_close_batch;

   -- --------------------------------------------------------------- S99 the error branch

   -- Reached only when some step failed. It must not raise: its job is to leave the run
   -- in a clean FAILED state so the next night's S01 can open a new one.
   PROCEDURE s99_fail_handler IS
      v_run     NUMBER;
      v_joblog  NUMBER;
      v_failed  VARCHAR2(400);
   BEGIN
      SELECT MAX(run_id) INTO v_run FROM eod_run WHERE status = 'RUNNING';
      IF v_run IS NULL THEN
         log_error('FATAL', -20102, 'EOD_BATCH.S99_FAIL_HANDLER',
                   'Chain entered the error branch with no open run to fail.');
         RETURN;
      END IF;

      SELECT LISTAGG(step_name, ', ') WITHIN GROUP (ORDER BY step_log_id)
        INTO v_failed
        FROM eod_step_log
       WHERE run_id = v_run AND status = 'FAILED';

      SELECT job_log_id INTO v_joblog FROM eod_run WHERE run_id = v_run;

      UPDATE eod_run
         SET status = 'FAILED', finished_at = SYSTIMESTAMP
       WHERE run_id = v_run;

      UPDATE applog.job_run_log
         SET finished_at = SYSTIMESTAMP,
             status      = 'FAILED',
             error_text  = SUBSTR('EOD chain aborted. Failed step(s): '
                                  || NVL(v_failed, 'unknown'), 1, 2000)
       WHERE run_id = v_joblog;

      COMMIT;

      log_error('FATAL', -20106, 'EOD_BATCH.S99_FAIL_HANDLER',
                'End-of-day run ' || v_run || ' marked FAILED. Failed step(s): '
                || NVL(v_failed, 'unknown'));
   EXCEPTION
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('FATAL', SQLCODE, 'EOD_BATCH.S99_FAIL_HANDLER', SQLERRM);
   END s99_fail_handler;

END eod_batch;
/

SHOW ERRORS PACKAGE BODY eod_batch

-- ================================================================ scheduler programs
-- One program per step. Chain steps can only point at programs, not at raw PL/SQL.
DECLARE
   TYPE t_step IS RECORD (name VARCHAR2(30), proc VARCHAR2(60), descr VARCHAR2(200));
   TYPE t_steps IS TABLE OF t_step;
   v_steps t_steps := t_steps(
      t_step('S01_OPEN_BUSINESS_DATE',  'eod_batch.s01_open_business_date',
             'Cut-off: open the next accounting day and the batch log entry.'),
      t_step('S02_INGEST_CLEARING',     'eod_batch.s02_ingest_clearing',
             'Read the inbound interbank clearing file into pending transactions.'),
      t_step('S03_RESET_CARD_LIMITS',   'eod_batch.s03_reset_card_limits',
             'Roll card daily-spend counters to the new day and expire due cards.'),
      t_step('S04_SETTLE_PENDING',      'eod_batch.s04_settle_pending',
             'Post pending transactions to balances and write the double-entry ledger.'),
      t_step('S05_ACCRUE_INTEREST',     'eod_batch.s05_accrue_interest',
             'Accrue one day of loan interest and savings credit interest, ACT/365.'),
      t_step('S06_APPLY_FEES',          'eod_batch.s06_apply_fees',
             'Charge overdraft fees daily and maintenance fees at month end.'),
      t_step('S07_AML_SCREEN',          'eod_batch.s07_aml_screen',
             'Screen the day for structuring, high value, dormancy and pass-through.'),
      t_step('S08_DELINQUENCY',         'eod_batch.s08_delinquency',
             'Age unpaid instalments into arrears buckets and IFRS 9 stages.'),
      t_step('S09_RECONCILE',           'eod_batch.s09_reconcile',
             'Prove the ledger balances and the balances match the statements.'),
      t_step('S10_REGULATORY_EXTRACT',  'eod_batch.s10_regulatory_extract',
             'Extract the daily prudential metrics for regulatory reporting.'),
      t_step('S11_CLOSE_BATCH',         'eod_batch.s11_close_batch',
             'Close the accounting day and mark the batch log successful.'),
      t_step('S99_FAIL_HANDLER',        'eod_batch.s99_fail_handler',
             'Error branch: mark the run failed and record why.'));
BEGIN
   FOR i IN 1 .. v_steps.COUNT LOOP
      DBMS_SCHEDULER.CREATE_PROGRAM(
         program_name   => 'BANKDEMO.PRG_' || v_steps(i).name,
         program_type   => 'STORED_PROCEDURE',
         program_action => 'BANKDEMO.' || v_steps(i).proc,
         number_of_arguments => 0,
         enabled        => TRUE,
         comments       => v_steps(i).descr);
   END LOOP;
END;
/

-- ==================================================================== the chain itself
BEGIN
   DBMS_SCHEDULER.CREATE_CHAIN(
      chain_name   => 'BANKDEMO.EOD_CHAIN',
      rule_set_name => NULL,
      evaluation_interval => NULL,
      comments     => 'Nightly end-of-day core banking batch: cut-off, clearing intake, '
                   || 'settlement, accrual, fees, AML screening, arrears, reconciliation, '
                   || 'regulatory extract and close.');
END;
/

DECLARE
   TYPE t_names IS TABLE OF VARCHAR2(30);
   v_steps t_names := t_names(
      'S01_OPEN_BUSINESS_DATE', 'S02_INGEST_CLEARING', 'S03_RESET_CARD_LIMITS',
      'S04_SETTLE_PENDING', 'S05_ACCRUE_INTEREST', 'S06_APPLY_FEES', 'S07_AML_SCREEN',
      'S08_DELINQUENCY', 'S09_RECONCILE', 'S10_REGULATORY_EXTRACT', 'S11_CLOSE_BATCH',
      'S99_FAIL_HANDLER');
   v_fail_cond VARCHAR2(2000);
BEGIN
   FOR i IN 1 .. v_steps.COUNT LOOP
      DBMS_SCHEDULER.DEFINE_CHAIN_STEP(
         chain_name => 'BANKDEMO.EOD_CHAIN',
         step_name  => v_steps(i),
         program_name => 'BANKDEMO.PRG_' || v_steps(i));
   END LOOP;

   -- --- the happy path ---------------------------------------------------------
   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'TRUE',
      action    => 'START S01_OPEN_BUSINESS_DATE',
      rule_name => 'R00_START',
      comments  => 'Every run begins at the cut-off step.');

   -- Clearing intake and the card day-roll are independent, so they run side by side.
   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S01_OPEN_BUSINESS_DATE SUCCEEDED',
      action    => 'START S02_INGEST_CLEARING, S03_RESET_CARD_LIMITS',
      rule_name => 'R01_FANOUT_INTAKE',
      comments  => 'Once the day is open, take in the clearing file and roll the cards.');

   -- Settlement needs the file loaded AND the limits rolled: an AND join.
   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S02_INGEST_CLEARING SUCCEEDED AND S03_RESET_CARD_LIMITS SUCCEEDED',
      action    => 'START S04_SETTLE_PENDING',
      rule_name => 'R02_JOIN_SETTLE',
      comments  => 'Settle only after both intake steps are done.');

   -- Accrual, fees and screening all read settled balances but not each other.
   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S04_SETTLE_PENDING SUCCEEDED',
      action    => 'START S05_ACCRUE_INTEREST, S06_APPLY_FEES, S07_AML_SCREEN',
      rule_name => 'R03_FANOUT_VALUATION',
      comments  => 'Three independent branches over the settled book.');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S05_ACCRUE_INTEREST SUCCEEDED',
      action    => 'START S08_DELINQUENCY',
      rule_name => 'R04_ARREARS',
      comments  => 'Arrears are aged after the loan book has accrued.');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S06_APPLY_FEES SUCCEEDED AND S07_AML_SCREEN SUCCEEDED '
                || 'AND S08_DELINQUENCY SUCCEEDED',
      action    => 'START S09_RECONCILE',
      rule_name => 'R05_JOIN_RECONCILE',
      comments  => 'Reconcile only when every posting branch has finished.');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S09_RECONCILE SUCCEEDED',
      action    => 'START S10_REGULATORY_EXTRACT',
      rule_name => 'R06_EXTRACT',
      comments  => 'Report only off a ledger that has been proved.');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S10_REGULATORY_EXTRACT SUCCEEDED',
      action    => 'START S11_CLOSE_BATCH',
      rule_name => 'R07_CLOSE');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S11_CLOSE_BATCH SUCCEEDED',
      action    => 'END 0',
      rule_name => 'R08_END_OK',
      comments  => 'Clean close: the chain job ends successfully.');

   -- --- the error branch -------------------------------------------------------
   -- Any failing step diverts to the handler. Built as an OR list over every step so
   -- adding a step to the array above is the only edit needed.
   FOR i IN 1 .. v_steps.COUNT - 1 LOOP          -- everything except S99 itself
      v_fail_cond := v_fail_cond
                     || CASE WHEN i > 1 THEN ' OR ' END
                     || v_steps(i) || ' FAILED';
   END LOOP;

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => v_fail_cond,
      action    => 'START S99_FAIL_HANDLER',
      rule_name => 'R90_ON_FAILURE',
      comments  => 'Any failed step diverts the chain to the error handler.');

   DBMS_SCHEDULER.DEFINE_CHAIN_RULE('BANKDEMO.EOD_CHAIN',
      condition => 'S99_FAIL_HANDLER COMPLETED',
      action    => 'END 1',
      rule_name => 'R91_END_FAILED',
      comments  => 'The chain job ends with a non-zero code so monitoring sees it.');

   DBMS_SCHEDULER.ENABLE('BANKDEMO.EOD_CHAIN');
END;
/

-- ======================================================================= nightly job
-- Runs after the cut-off, once a day. Restartable so a failed night can be resumed
-- from the step that broke rather than from the top.
BEGIN
   DBMS_SCHEDULER.CREATE_JOB(
      job_name        => 'BANKDEMO.EOD_BATCH_JOB',
      job_type        => 'CHAIN',
      job_action      => 'BANKDEMO.EOD_CHAIN',
      start_date      => TRUNC(SYSDATE) + 1 + 23/24 + 5/1440,   -- tomorrow 23:05
      repeat_interval => 'FREQ=DAILY; BYHOUR=23; BYMINUTE=5; BYSECOND=0',
      enabled         => TRUE,
      auto_drop       => FALSE,
      comments        => 'Nightly end-of-day core banking batch for BANKDEMO.');

   DBMS_SCHEDULER.SET_ATTRIBUTE('BANKDEMO.EOD_BATCH_JOB', 'restartable',   TRUE);
   DBMS_SCHEDULER.SET_ATTRIBUTE('BANKDEMO.EOD_BATCH_JOB', 'max_run_duration',
                                INTERVAL '30' MINUTE);
   DBMS_SCHEDULER.SET_ATTRIBUTE('BANKDEMO.EOD_BATCH_JOB', 'raise_events',
                                DBMS_SCHEDULER.JOB_FAILED + DBMS_SCHEDULER.JOB_SUCCEEDED);
END;
/

-- Stats, so the optimiser and Blossa's introspector both see real row counts.
BEGIN
   DBMS_STATS.GATHER_SCHEMA_STATS(ownname => 'BANKDEMO', cascade => TRUE);
END;
/

-- ---------------------------------------------------------------------------- summary
SET SERVEROUTPUT ON
DECLARE
   v_steps NUMBER;
   v_rules NUMBER;
   v_progs NUMBER;
   v_tabs  NUMBER;
BEGIN
   SELECT COUNT(*) INTO v_steps FROM dba_scheduler_chain_steps
    WHERE owner = 'BANKDEMO' AND chain_name = 'EOD_CHAIN';
   SELECT COUNT(*) INTO v_rules FROM dba_scheduler_chain_rules
    WHERE owner = 'BANKDEMO' AND chain_name = 'EOD_CHAIN';
   SELECT COUNT(*) INTO v_progs FROM dba_scheduler_programs WHERE owner = 'BANKDEMO';
   SELECT COUNT(*) INTO v_tabs  FROM dba_tables WHERE owner = 'BANKDEMO';

   DBMS_OUTPUT.PUT_LINE('BANKDEMO.EOD_CHAIN installed.');
   DBMS_OUTPUT.PUT_LINE('  chain steps : ' || v_steps);
   DBMS_OUTPUT.PUT_LINE('  chain rules : ' || v_rules);
   DBMS_OUTPUT.PUT_LINE('  programs    : ' || v_progs);
   DBMS_OUTPUT.PUT_LINE('  tables      : ' || v_tabs || ' (5 from bank_demo.sql + 12 batch)');
   DBMS_OUTPUT.PUT_LINE('  job         : BANKDEMO.EOD_BATCH_JOB, daily at 23:05');
   DBMS_OUTPUT.PUT_LINE('');
   DBMS_OUTPUT.PUT_LINE('Run one night on demand:');
   DBMS_OUTPUT.PUT_LINE('  EXEC DBMS_SCHEDULER.RUN_JOB(''BANKDEMO.EOD_BATCH_JOB'', FALSE);');
   DBMS_OUTPUT.PUT_LINE('Exercise the error branch:');
   DBMS_OUTPUT.PUT_LINE('  EXEC BANKDEMO.EOD_BATCH.SET_PARAM(''FORCE_FAIL_STEP'', ''S06_APPLY_FEES'');');
END;
/

EXIT
