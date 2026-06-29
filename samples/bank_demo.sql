-- bank_demo.sql -----------------------------------------------------------------
-- A SYNTHETIC but realistic core-banking application schema (BANKDEMO) whose whole
-- point is to be a live, RUNNING system that records its own failures in the shared
-- application log (APPLOG.ERROR_LOG) the way a real app does: every business rule
-- that fails is caught and written out through an AUTONOMOUS-TRANSACTION logger, so
-- the error survives even when the business transaction itself rolls back.
--
-- It exercises several Blossa features at once:
--   * program logic  -> the CORE_BANKING package (functions + procedures) for the Logic tab
--   * cross-schema   -> BANKDEMO writes into APPLOG's log tables (separate app vs log schema)
--   * logs / spikes  -> running bank_workload.sql fills APPLOG.ERROR_LOG with a real time series
--
-- Tables: CUSTOMERS, ACCOUNTS, CARDS, TRANSACTIONS, LOANS.
-- Package CORE_BANKING:
--   functions  get_balance, is_account_active, monthly_interest, card_daily_remaining
--   procedures open_account, deposit, withdraw, transfer_funds, charge_card,
--              apply_monthly_interest (batch -> JOB_RUN_LOG), close_account
--
-- Run as SYSTEM, AFTER applog_demo.sql (the log tables must already exist):
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/bank_demo.sql

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET DEFINE OFF
SET SERVEROUTPUT ON

-- ---------------------------------------------------------------- (re)create user
DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'BANKDEMO';
   IF n > 0 THEN
      EXECUTE IMMEDIATE 'DROP USER BANKDEMO CASCADE';
   END IF;
END;
/

CREATE USER bankdemo IDENTIFIED BY "oracle"
   DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE, CREATE SEQUENCE TO bankdemo;

-- Sequences for the shared log tables live in APPLOG; BANKDEMO references them by grant.
DECLARE
   PROCEDURE ensure_seq(p_name VARCHAR2, p_start NUMBER) IS
      n NUMBER;
   BEGIN
      SELECT COUNT(*) INTO n FROM all_sequences
       WHERE sequence_owner = 'APPLOG' AND sequence_name = p_name;
      IF n = 0 THEN
         EXECUTE IMMEDIATE 'CREATE SEQUENCE applog.' || p_name
                           || ' START WITH ' || p_start || ' NOCACHE';
      END IF;
   END;
BEGIN
   ensure_seq('ERROR_SEQ', 100000);
   ensure_seq('JOB_SEQ',   100000);
   ensure_seq('AUDIT_SEQ', 100000);
END;
/

-- Direct grants (PL/SQL ignores role privileges) so the package can log cross-schema.
GRANT INSERT ON applog.error_log   TO bankdemo;
GRANT INSERT ON applog.job_run_log TO bankdemo;
GRANT INSERT ON applog.audit_trail TO bankdemo;
-- DELETE + SELECT are granted ONLY so the demo workload (bank_workload.sql) can reset the rows it
-- owns (a WHERE-clause DELETE needs SELECT too) and print a summary. A real application would have
-- neither on the central log tables — it would only ever INSERT.
GRANT DELETE, SELECT ON applog.error_log   TO bankdemo;
GRANT DELETE, SELECT ON applog.job_run_log TO bankdemo;
GRANT DELETE, SELECT ON applog.audit_trail TO bankdemo;
GRANT SELECT ON applog.error_seq   TO bankdemo;
GRANT SELECT ON applog.job_seq     TO bankdemo;
GRANT SELECT ON applog.audit_seq   TO bankdemo;

ALTER SESSION SET CURRENT_SCHEMA = BANKDEMO;

-- ---------------------------------------------------------------- tables
CREATE TABLE customers (
   customer_id  NUMBER(10)  NOT NULL,
   full_name    VARCHAR2(80),
   email        VARCHAR2(120),
   status       VARCHAR2(12) DEFAULT 'ACTIVE',   -- ACTIVE / DORMANT / CLOSED
   created_at   DATE         DEFAULT SYSDATE,
   CONSTRAINT pk_customers PRIMARY KEY (customer_id)
);

CREATE TABLE accounts (
   account_id   NUMBER(12)  NOT NULL,
   customer_id  NUMBER(10)  NOT NULL,
   account_type VARCHAR2(12),                     -- CHECKING / SAVINGS
   balance      NUMBER(14,2) DEFAULT 0,
   currency     VARCHAR2(3)  DEFAULT 'EUR',
   status       VARCHAR2(12) DEFAULT 'ACTIVE',    -- ACTIVE / FROZEN / CLOSED
   daily_limit  NUMBER(12,2) DEFAULT 5000,
   opened_at    DATE         DEFAULT SYSDATE,
   CONSTRAINT pk_accounts PRIMARY KEY (account_id),
   CONSTRAINT fk_acct_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE cards (
   card_id          NUMBER(12) NOT NULL,
   account_id       NUMBER(12) NOT NULL,
   card_last4       VARCHAR2(4),
   status           VARCHAR2(12) DEFAULT 'ACTIVE',  -- ACTIVE / BLOCKED / EXPIRED
   expires_on       DATE,
   daily_spent      NUMBER(12,2) DEFAULT 0,
   daily_spent_date DATE,
   CONSTRAINT pk_cards PRIMARY KEY (card_id),
   CONSTRAINT fk_card_account FOREIGN KEY (account_id) REFERENCES accounts (account_id)
);

CREATE TABLE transactions (
   txn_id              NUMBER(14) NOT NULL,
   account_id          NUMBER(12) NOT NULL,
   txn_type            VARCHAR2(16),                -- DEPOSIT / WITHDRAWAL / TRANSFER / CARD
   amount              NUMBER(14,2),
   balance_after       NUMBER(14,2),
   counterparty_acct   NUMBER(12),
   status              VARCHAR2(12),                -- POSTED / REVERSED
   created_at          TIMESTAMP DEFAULT SYSTIMESTAMP,
   note                VARCHAR2(200),
   CONSTRAINT pk_transactions PRIMARY KEY (txn_id),
   CONSTRAINT fk_txn_account FOREIGN KEY (account_id) REFERENCES accounts (account_id)
);

CREATE TABLE loans (
   loan_id      NUMBER(12) NOT NULL,
   customer_id  NUMBER(10) NOT NULL,
   principal    NUMBER(14,2),
   rate_pct     NUMBER(6,3),                        -- annual %, NULL on a bad row (forces a batch error)
   outstanding  NUMBER(14,2),
   status       VARCHAR2(12) DEFAULT 'ACTIVE',
   opened_at    DATE DEFAULT SYSDATE,
   CONSTRAINT pk_loans PRIMARY KEY (loan_id),
   CONSTRAINT fk_loan_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE SEQUENCE account_seq START WITH 90000 NOCACHE;
CREATE SEQUENCE txn_seq     START WITH 700000 NOCACHE;

-- ---------------------------------------------------------------- package spec
CREATE OR REPLACE PACKAGE core_banking AS
   -- Application error codes (ORA-20001..20012), exposed so callers can catch a specific failure.
   e_invalid_amount     EXCEPTION; PRAGMA EXCEPTION_INIT(e_invalid_amount,     -20001);
   e_account_not_found  EXCEPTION; PRAGMA EXCEPTION_INIT(e_account_not_found,  -20002);
   e_account_frozen     EXCEPTION; PRAGMA EXCEPTION_INIT(e_account_frozen,     -20003);
   e_account_closed     EXCEPTION; PRAGMA EXCEPTION_INIT(e_account_closed,     -20004);
   e_insufficient_funds EXCEPTION; PRAGMA EXCEPTION_INIT(e_insufficient_funds, -20005);
   e_card_not_found     EXCEPTION; PRAGMA EXCEPTION_INIT(e_card_not_found,     -20006);
   e_card_expired       EXCEPTION; PRAGMA EXCEPTION_INIT(e_card_expired,       -20007);
   e_card_blocked       EXCEPTION; PRAGMA EXCEPTION_INIT(e_card_blocked,       -20008);
   e_daily_limit        EXCEPTION; PRAGMA EXCEPTION_INIT(e_daily_limit,        -20009);
   e_gateway_timeout    EXCEPTION; PRAGMA EXCEPTION_INIT(e_gateway_timeout,    -20010);

   -- Simulation hooks: used ONLY by the test workload to backdate log timestamps so a multi-hour
   -- history (and a spike) can be replayed. Real callers never set the clock -> SYSTIMESTAMP is used.
   PROCEDURE set_sim_clock(p_ts IN TIMESTAMP);
   PROCEDURE clear_sim_clock;

   FUNCTION get_balance(p_account_id IN NUMBER) RETURN NUMBER;
   FUNCTION is_account_active(p_account_id IN NUMBER) RETURN VARCHAR2;
   FUNCTION monthly_interest(p_principal IN NUMBER, p_rate_pct IN NUMBER) RETURN NUMBER;
   FUNCTION card_daily_remaining(p_card_id IN NUMBER) RETURN NUMBER;

   PROCEDURE open_account(p_customer_id IN NUMBER, p_type IN VARCHAR2,
                          p_initial IN NUMBER, p_account_id OUT NUMBER);
   PROCEDURE deposit(p_account_id IN NUMBER, p_amount IN NUMBER, p_note IN VARCHAR2 DEFAULT NULL);
   PROCEDURE withdraw(p_account_id IN NUMBER, p_amount IN NUMBER, p_note IN VARCHAR2 DEFAULT NULL);
   PROCEDURE transfer_funds(p_from IN NUMBER, p_to IN NUMBER, p_amount IN NUMBER);
   PROCEDURE charge_card(p_card_id IN NUMBER, p_amount IN NUMBER, p_merchant IN VARCHAR2,
                         p_force_gateway_fail IN BOOLEAN DEFAULT FALSE);
   PROCEDURE apply_monthly_interest;
   PROCEDURE close_account(p_account_id IN NUMBER);
END core_banking;
/

-- ---------------------------------------------------------------- package body
CREATE OR REPLACE PACKAGE BODY core_banking AS

   g_sim_clock TIMESTAMP := NULL;   -- NULL => use the real clock

   -- The autonomous error logger: writes to the SHARED log schema and commits independently, so a
   -- failed-and-rolled-back business transaction still leaves an audit trail of WHY it failed.
   -- p_ref names the affected entity (account/card/loan) for readability at the call sites; it is
   -- NOT stored in ERROR_LOG.ORDER_ID, whose FK is scoped to APP_ORDERS — the id instead appears in
   -- the message text, so order_id stays NULL for banking errors.
   PROCEDURE log_error(p_severity IN VARCHAR2, p_code IN NUMBER, p_module IN VARCHAR2,
                       p_message IN VARCHAR2, p_ref IN NUMBER DEFAULT NULL) IS
      PRAGMA AUTONOMOUS_TRANSACTION;
   BEGIN
      INSERT INTO applog.error_log
         (error_id, log_time, severity, error_code, module, message, db_user, order_id)
      VALUES
         (applog.error_seq.NEXTVAL, NVL(g_sim_clock, SYSTIMESTAMP), p_severity, p_code,
          p_module, SUBSTR(p_message, 1, 2000), USER, NULL);
      COMMIT;
   END log_error;

   PROCEDURE record_txn(p_account_id IN NUMBER, p_type IN VARCHAR2, p_amount IN NUMBER,
                        p_balance_after IN NUMBER, p_counterparty IN NUMBER,
                        p_status IN VARCHAR2, p_note IN VARCHAR2) IS
   BEGIN
      INSERT INTO transactions
         (txn_id, account_id, txn_type, amount, balance_after, counterparty_acct, status,
          created_at, note)
      VALUES
         (txn_seq.NEXTVAL, p_account_id, p_type, p_amount, p_balance_after, p_counterparty,
          p_status, NVL(g_sim_clock, SYSTIMESTAMP), p_note);
   END record_txn;

   PROCEDURE set_sim_clock(p_ts IN TIMESTAMP) IS BEGIN g_sim_clock := p_ts; END;
   PROCEDURE clear_sim_clock IS BEGIN g_sim_clock := NULL; END;

   FUNCTION money(p_n IN NUMBER) RETURN VARCHAR2 IS
   BEGIN
      RETURN TO_CHAR(p_n, 'FM999999990.00');
   END money;

   -- ----------------------------------------------------------- functions
   FUNCTION get_balance(p_account_id IN NUMBER) RETURN NUMBER IS
      v_balance NUMBER;
   BEGIN
      SELECT balance INTO v_balance FROM accounts WHERE account_id = p_account_id;
      RETURN v_balance;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN
         log_error('ERROR', -20002, 'CORE_BANKING.GET_BALANCE',
                   'Balance requested for unknown account ' || p_account_id, p_account_id);
         RAISE e_account_not_found;
   END get_balance;

   FUNCTION is_account_active(p_account_id IN NUMBER) RETURN VARCHAR2 IS
      v_status VARCHAR2(12);
   BEGIN
      SELECT status INTO v_status FROM accounts WHERE account_id = p_account_id;
      RETURN CASE WHEN v_status = 'ACTIVE' THEN 'Y' ELSE 'N' END;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN RETURN 'N';
   END is_account_active;

   FUNCTION monthly_interest(p_principal IN NUMBER, p_rate_pct IN NUMBER) RETURN NUMBER IS
   BEGIN
      IF p_principal IS NULL OR p_rate_pct IS NULL THEN
         RAISE_APPLICATION_ERROR(-20012, 'principal/rate missing for interest calc');
      END IF;
      RETURN ROUND(p_principal * (p_rate_pct / 100) / 12, 2);
   END monthly_interest;

   FUNCTION card_daily_remaining(p_card_id IN NUMBER) RETURN NUMBER IS
      v_limit NUMBER; v_spent NUMBER; v_spent_date DATE;
   BEGIN
      SELECT a.daily_limit, c.daily_spent, c.daily_spent_date
        INTO v_limit, v_spent, v_spent_date
        FROM cards c JOIN accounts a ON a.account_id = c.account_id
       WHERE c.card_id = p_card_id;
      IF v_spent_date IS NULL OR v_spent_date < TRUNC(SYSDATE) THEN
         v_spent := 0;                 -- a new day resets the running total
      END IF;
      RETURN v_limit - v_spent;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN RAISE e_card_not_found;
   END card_daily_remaining;

   -- ----------------------------------------------------------- procedures
   PROCEDURE open_account(p_customer_id IN NUMBER, p_type IN VARCHAR2,
                          p_initial IN NUMBER, p_account_id OUT NUMBER) IS
      v_details VARCHAR2(200);
   BEGIN
      IF NVL(p_initial, 0) < 0 THEN RAISE e_invalid_amount; END IF;
      p_account_id := account_seq.NEXTVAL;
      v_details := 'opened ' || p_type || ' with ' || money(p_initial);
      INSERT INTO accounts (account_id, customer_id, account_type, balance)
         VALUES (p_account_id, p_customer_id, p_type, NVL(p_initial, 0));
      INSERT INTO applog.audit_trail (audit_id, event_time, action, object_name, row_pk, changed_by,
                                      details)
         VALUES (applog.audit_seq.NEXTVAL, NVL(g_sim_clock, SYSTIMESTAMP), 'INSERT', 'ACCOUNTS',
                 TO_CHAR(p_account_id), USER, v_details);
      COMMIT;
   EXCEPTION
      WHEN e_invalid_amount THEN
         ROLLBACK;
         log_error('WARN', -20001, 'CORE_BANKING.OPEN_ACCOUNT',
                   'Rejected negative opening balance ' || money(p_initial)
                   || ' for customer ' || p_customer_id, p_customer_id);
         RAISE;
      WHEN OTHERS THEN
         ROLLBACK;
         log_error('FATAL', SQLCODE, 'CORE_BANKING.OPEN_ACCOUNT',
                   'Open account failed for customer ' || p_customer_id || ': '
                   || SUBSTR(SQLERRM, 1, 300), p_customer_id);
         RAISE;
   END open_account;

   PROCEDURE deposit(p_account_id IN NUMBER, p_amount IN NUMBER, p_note IN VARCHAR2 DEFAULT NULL) IS
      v_balance NUMBER; v_status VARCHAR2(12);
   BEGIN
      IF NVL(p_amount, 0) <= 0 THEN RAISE e_invalid_amount; END IF;
      SELECT balance, status INTO v_balance, v_status
        FROM accounts WHERE account_id = p_account_id FOR UPDATE;
      IF v_status = 'CLOSED' THEN RAISE e_account_closed; END IF;
      IF v_status = 'FROZEN' THEN RAISE e_account_frozen; END IF;
      v_balance := v_balance + p_amount;
      UPDATE accounts SET balance = v_balance WHERE account_id = p_account_id;
      record_txn(p_account_id, 'DEPOSIT', p_amount, v_balance, NULL, 'POSTED', p_note);
      COMMIT;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN
         ROLLBACK;
         log_error('ERROR', -20002, 'CORE_BANKING.DEPOSIT',
                   'Deposit to unknown account ' || p_account_id, p_account_id);
         RAISE e_account_not_found;
      WHEN e_invalid_amount THEN
         ROLLBACK;
         log_error('WARN', -20001, 'CORE_BANKING.DEPOSIT',
                   'Rejected non-positive deposit ' || money(p_amount) || ' on account '
                   || p_account_id, p_account_id);
         RAISE;
      WHEN e_account_closed THEN
         ROLLBACK;
         log_error('ERROR', -20004, 'CORE_BANKING.DEPOSIT',
                   'Deposit to CLOSED account ' || p_account_id || ' rejected', p_account_id);
         RAISE;
      WHEN e_account_frozen THEN
         ROLLBACK;
         log_error('ERROR', -20003, 'CORE_BANKING.DEPOSIT',
                   'Deposit blocked: account ' || p_account_id || ' is FROZEN', p_account_id);
         RAISE;
   END deposit;

   PROCEDURE withdraw(p_account_id IN NUMBER, p_amount IN NUMBER, p_note IN VARCHAR2 DEFAULT NULL) IS
      v_balance NUMBER; v_status VARCHAR2(12);
   BEGIN
      IF NVL(p_amount, 0) <= 0 THEN RAISE e_invalid_amount; END IF;
      SELECT balance, status INTO v_balance, v_status
        FROM accounts WHERE account_id = p_account_id FOR UPDATE;
      IF v_status = 'CLOSED' THEN RAISE e_account_closed; END IF;
      IF v_status = 'FROZEN' THEN RAISE e_account_frozen; END IF;
      IF v_balance < p_amount THEN RAISE e_insufficient_funds; END IF;
      v_balance := v_balance - p_amount;
      UPDATE accounts SET balance = v_balance WHERE account_id = p_account_id;
      record_txn(p_account_id, 'WITHDRAWAL', p_amount, v_balance, NULL, 'POSTED', p_note);
      COMMIT;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN
         ROLLBACK;
         log_error('ERROR', -20002, 'CORE_BANKING.WITHDRAW',
                   'Withdrawal from unknown account ' || p_account_id, p_account_id);
         RAISE e_account_not_found;
      WHEN e_invalid_amount THEN
         ROLLBACK;
         log_error('WARN', -20001, 'CORE_BANKING.WITHDRAW',
                   'Rejected non-positive withdrawal ' || money(p_amount) || ' on account '
                   || p_account_id, p_account_id);
         RAISE;
      WHEN e_account_frozen THEN
         ROLLBACK;
         log_error('ERROR', -20003, 'CORE_BANKING.WITHDRAW',
                   'Withdrawal blocked: account ' || p_account_id || ' is FROZEN', p_account_id);
         RAISE;
      WHEN e_account_closed THEN
         ROLLBACK;
         log_error('ERROR', -20004, 'CORE_BANKING.WITHDRAW',
                   'Withdrawal from CLOSED account ' || p_account_id, p_account_id);
         RAISE;
      WHEN e_insufficient_funds THEN
         ROLLBACK;
         log_error('ERROR', -20005, 'CORE_BANKING.WITHDRAW',
                   'Insufficient funds on account ' || p_account_id || ': balance '
                   || money(v_balance) || ' < requested ' || money(p_amount), p_account_id);
         RAISE;
   END withdraw;

   PROCEDURE transfer_funds(p_from IN NUMBER, p_to IN NUMBER, p_amount IN NUMBER) IS
      v_from_balance NUMBER; v_from_status VARCHAR2(12);
      v_to_balance   NUMBER; v_to_status   VARCHAR2(12);
   BEGIN
      IF NVL(p_amount, 0) <= 0 THEN RAISE e_invalid_amount; END IF;
      -- Lock both rows (lowest id first to avoid deadlock) then validate.
      SELECT balance, status INTO v_from_balance, v_from_status
        FROM accounts WHERE account_id = p_from FOR UPDATE;
      BEGIN
         SELECT balance, status INTO v_to_balance, v_to_status
           FROM accounts WHERE account_id = p_to FOR UPDATE;
      EXCEPTION
         WHEN NO_DATA_FOUND THEN RAISE e_account_not_found;
      END;
      IF v_from_status <> 'ACTIVE' THEN RAISE e_account_frozen; END IF;
      IF v_to_status = 'CLOSED' THEN RAISE e_account_closed; END IF;
      IF v_from_balance < p_amount THEN RAISE e_insufficient_funds; END IF;

      v_from_balance := v_from_balance - p_amount;
      v_to_balance   := v_to_balance + p_amount;
      UPDATE accounts SET balance = v_from_balance WHERE account_id = p_from;
      UPDATE accounts SET balance = v_to_balance   WHERE account_id = p_to;
      record_txn(p_from, 'TRANSFER', p_amount, v_from_balance, p_to, 'POSTED', 'transfer out');
      record_txn(p_to,   'TRANSFER', p_amount, v_to_balance,   p_from, 'POSTED', 'transfer in');
      COMMIT;
   EXCEPTION
      WHEN e_invalid_amount THEN
         ROLLBACK;
         log_error('WARN', -20001, 'CORE_BANKING.TRANSFER_FUNDS',
                   'Rejected non-positive transfer ' || money(p_amount) || ' from ' || p_from
                   || ' to ' || p_to, p_from);
         RAISE;
      WHEN e_account_not_found THEN
         ROLLBACK;
         log_error('ERROR', -20002, 'CORE_BANKING.TRANSFER_FUNDS',
                   'Transfer failed: beneficiary account ' || p_to || ' not found', p_from);
         RAISE;
      WHEN e_account_frozen THEN
         ROLLBACK;
         log_error('ERROR', -20003, 'CORE_BANKING.TRANSFER_FUNDS',
                   'Transfer from non-active account ' || p_from || ' blocked', p_from);
         RAISE;
      WHEN e_account_closed THEN
         ROLLBACK;
         log_error('ERROR', -20004, 'CORE_BANKING.TRANSFER_FUNDS',
                   'Transfer into CLOSED account ' || p_to || ' rejected', p_from);
         RAISE;
      WHEN e_insufficient_funds THEN
         ROLLBACK;
         log_error('ERROR', -20005, 'CORE_BANKING.TRANSFER_FUNDS',
                   'Transfer of ' || money(p_amount) || ' from account ' || p_from
                   || ' declined: balance ' || money(v_from_balance + p_amount), p_from);
         RAISE;
   END transfer_funds;

   PROCEDURE charge_card(p_card_id IN NUMBER, p_amount IN NUMBER, p_merchant IN VARCHAR2,
                         p_force_gateway_fail IN BOOLEAN DEFAULT FALSE) IS
      v_account_id NUMBER; v_last4 VARCHAR2(4); v_card_status VARCHAR2(12);
      v_expires DATE; v_balance NUMBER; v_remaining NUMBER;
   BEGIN
      IF NVL(p_amount, 0) <= 0 THEN RAISE e_invalid_amount; END IF;
      SELECT c.account_id, c.card_last4, c.status, c.expires_on, a.balance
        INTO v_account_id, v_last4, v_card_status, v_expires, v_balance
        FROM cards c JOIN accounts a ON a.account_id = c.account_id
       WHERE c.card_id = p_card_id FOR UPDATE OF c.daily_spent;

      IF v_expires < TRUNC(SYSDATE) THEN RAISE e_card_expired; END IF;
      IF v_card_status = 'BLOCKED' THEN RAISE e_card_blocked; END IF;

      v_remaining := card_daily_remaining(p_card_id);
      IF p_amount > v_remaining THEN RAISE e_daily_limit; END IF;

      -- The external payment gateway: in the incident window the workload forces this to time out.
      IF p_force_gateway_fail THEN RAISE e_gateway_timeout; END IF;

      IF v_balance < p_amount THEN RAISE e_insufficient_funds; END IF;

      UPDATE accounts SET balance = balance - p_amount WHERE account_id = v_account_id;
      UPDATE cards
         SET daily_spent = CASE WHEN daily_spent_date = TRUNC(SYSDATE) THEN daily_spent ELSE 0 END
                           + p_amount,
             daily_spent_date = TRUNC(SYSDATE)
       WHERE card_id = p_card_id;
      record_txn(v_account_id, 'CARD', p_amount, v_balance - p_amount, NULL, 'POSTED',
                 'card ending ' || v_last4 || ' at ' || p_merchant);
      COMMIT;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN
         ROLLBACK;
         log_error('ERROR', -20006, 'CORE_BANKING.CHARGE_CARD',
                   'Charge on unknown card ' || p_card_id, p_card_id);
         RAISE e_card_not_found;
      WHEN e_invalid_amount THEN
         ROLLBACK;
         log_error('WARN', -20001, 'CORE_BANKING.CHARGE_CARD',
                   'Rejected non-positive card charge ' || money(p_amount) || ' at ' || p_merchant,
                   p_card_id);
         RAISE;
      WHEN e_card_expired THEN
         ROLLBACK;
         log_error('ERROR', -20007, 'CORE_BANKING.CHARGE_CARD',
                   'Card ending ' || v_last4 || ' expired on '
                   || TO_CHAR(v_expires, 'YYYY-MM-DD') || ', charge at ' || p_merchant
                   || ' declined', p_card_id);
         RAISE;
      WHEN e_card_blocked THEN
         ROLLBACK;
         log_error('ERROR', -20008, 'CORE_BANKING.CHARGE_CARD',
                   'Card ending ' || v_last4 || ' is BLOCKED; charge at ' || p_merchant
                   || ' declined', p_card_id);
         RAISE;
      WHEN e_daily_limit THEN
         ROLLBACK;
         log_error('ERROR', -20009, 'CORE_BANKING.CHARGE_CARD',
                   'Daily limit exceeded on card ending ' || v_last4 || ': '
                   || money(p_amount) || ' over remaining ' || money(v_remaining), p_card_id);
         RAISE;
      WHEN e_gateway_timeout THEN
         ROLLBACK;
         log_error('ERROR', -20010, 'CORE_BANKING.CHARGE_CARD',
                   'Payment gateway timeout after 30s charging card ending ' || v_last4
                   || ' at ' || p_merchant, p_card_id);
         RAISE;
      WHEN e_insufficient_funds THEN
         ROLLBACK;
         log_error('ERROR', -20005, 'CORE_BANKING.CHARGE_CARD',
                   'Card ending ' || v_last4 || ' declined at ' || p_merchant
                   || ': balance ' || money(v_balance) || ' < ' || money(p_amount), p_card_id);
         RAISE;
   END charge_card;

   PROCEDURE apply_monthly_interest IS
      v_started TIMESTAMP := NVL(g_sim_clock, SYSTIMESTAMP);
      v_count   NUMBER := 0;
      v_failed  NUMBER := 0;
      v_int     NUMBER;
   BEGIN
      FOR rec IN (SELECT loan_id, principal, rate_pct FROM loans WHERE status = 'ACTIVE') LOOP
         BEGIN
            v_int := monthly_interest(rec.principal, rec.rate_pct);
            UPDATE loans SET outstanding = NVL(outstanding, principal) + v_int
             WHERE loan_id = rec.loan_id;
            v_count := v_count + 1;
         EXCEPTION
            WHEN OTHERS THEN
               v_failed := v_failed + 1;
               log_error('ERROR', SQLCODE, 'CORE_BANKING.APPLY_MONTHLY_INTEREST',
                         'Loan ' || rec.loan_id || ' interest run failed: '
                         || SUBSTR(SQLERRM, 1, 200), rec.loan_id);
         END;
      END LOOP;
      COMMIT;
      INSERT INTO applog.job_run_log
         (run_id, job_name, started_at, finished_at, status, rows_processed, error_text)
      VALUES
         (applog.job_seq.NEXTVAL, 'MONTHLY_INTEREST', v_started, NVL(g_sim_clock, SYSTIMESTAMP),
          CASE WHEN v_failed > 0 THEN 'FAILED' ELSE 'SUCCESS' END, v_count,
          CASE WHEN v_failed > 0 THEN v_failed || ' loan(s) failed' END);
      COMMIT;
   END apply_monthly_interest;

   PROCEDURE close_account(p_account_id IN NUMBER) IS
      v_balance NUMBER;
   BEGIN
      SELECT balance INTO v_balance FROM accounts WHERE account_id = p_account_id FOR UPDATE;
      IF v_balance <> 0 THEN
         log_error('WARN', -20005, 'CORE_BANKING.CLOSE_ACCOUNT',
                   'Closing account ' || p_account_id || ' with non-zero balance '
                   || money(v_balance), p_account_id);
      END IF;
      UPDATE accounts SET status = 'CLOSED' WHERE account_id = p_account_id;
      INSERT INTO applog.audit_trail (audit_id, event_time, action, object_name, row_pk, changed_by,
                                      details)
         VALUES (applog.audit_seq.NEXTVAL, NVL(g_sim_clock, SYSTIMESTAMP), 'UPDATE', 'ACCOUNTS',
                 TO_CHAR(p_account_id), USER, 'status -> CLOSED');
      COMMIT;
   EXCEPTION
      WHEN NO_DATA_FOUND THEN
         log_error('ERROR', -20002, 'CORE_BANKING.CLOSE_ACCOUNT',
                   'Close requested for unknown account ' || p_account_id, p_account_id);
         RAISE e_account_not_found;
   END close_account;

END core_banking;
/

SHOW ERRORS PACKAGE BODY core_banking

-- ---------------------------------------------------------------- seed data
INSERT INTO customers (customer_id, full_name, email, status) VALUES
   (1001, 'Acme Holdings SRL', 'finance@acme-corp.example', 'ACTIVE');
INSERT INTO customers (customer_id, full_name, email, status) VALUES
   (1002, 'Maria Ionescu', 'maria.ionescu@example.com', 'ACTIVE');
INSERT INTO customers (customer_id, full_name, email, status) VALUES
   (1003, 'Globex Trading', 'ap@globex.example', 'ACTIVE');
INSERT INTO customers (customer_id, full_name, email, status) VALUES
   (1004, 'Ion Popescu', 'ion.popescu@example.com', 'DORMANT');

INSERT INTO accounts (account_id, customer_id, account_type, balance, status, daily_limit) VALUES
   (80001, 1001, 'CHECKING', 125000.00, 'ACTIVE', 20000);
INSERT INTO accounts (account_id, customer_id, account_type, balance, status, daily_limit) VALUES
   (80002, 1002, 'CHECKING',    540.50, 'ACTIVE', 2000);
INSERT INTO accounts (account_id, customer_id, account_type, balance, status, daily_limit) VALUES
   (80003, 1002, 'SAVINGS',   18250.00, 'ACTIVE', 5000);
INSERT INTO accounts (account_id, customer_id, account_type, balance, status, daily_limit) VALUES
   (80004, 1003, 'CHECKING',   7600.00, 'FROZEN', 10000);
INSERT INTO accounts (account_id, customer_id, account_type, balance, status, daily_limit) VALUES
   (80005, 1004, 'CHECKING',     12.00, 'ACTIVE', 1000);

INSERT INTO cards (card_id, account_id, card_last4, status, expires_on, daily_spent, daily_spent_date)
   VALUES (9001, 80001, '4417', 'ACTIVE',  ADD_MONTHS(TRUNC(SYSDATE), 18), 0, NULL);
INSERT INTO cards (card_id, account_id, card_last4, status, expires_on, daily_spent, daily_spent_date)
   VALUES (9002, 80002, '5560', 'ACTIVE',  ADD_MONTHS(TRUNC(SYSDATE), 6),  0, NULL);
INSERT INTO cards (card_id, account_id, card_last4, status, expires_on, daily_spent, daily_spent_date)
   VALUES (9003, 80003, '8821', 'BLOCKED', ADD_MONTHS(TRUNC(SYSDATE), 9),  0, NULL);
-- card 9004 is already past its expiry date (negative ADD_MONTHS), so charges on it must fail.
INSERT INTO cards (card_id, account_id, card_last4, status, expires_on, daily_spent, daily_spent_date)
   VALUES (9004, 80005, '3094', 'ACTIVE',  ADD_MONTHS(TRUNC(SYSDATE), -2), 0, NULL);

INSERT INTO loans (loan_id, customer_id, principal, rate_pct, outstanding, status) VALUES
   (7001, 1001, 50000.00, 6.500, 50000.00, 'ACTIVE');
INSERT INTO loans (loan_id, customer_id, principal, rate_pct, outstanding, status) VALUES
   (7002, 1002,  8000.00, 9.250,  8000.00, 'ACTIVE');
-- loan 7003 has a NULL rate_pct, which makes the monthly-interest batch raise on this row.
INSERT INTO loans (loan_id, customer_id, principal, rate_pct, outstanding, status) VALUES
   (7003, 1003, 12000.00, NULL, 12000.00, 'ACTIVE');
COMMIT;

PROMPT BANKDEMO installed: customers/accounts/cards/loans + CORE_BANKING package.
exit
