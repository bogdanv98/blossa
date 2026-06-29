-- bank_workload.sql -------------------------------------------------------------
-- Drive the CORE_BANKING package like a live system so it fills APPLOG.ERROR_LOG with
-- a REAL error time series (every row here is the genuine output of a failed business
-- rule, logged by the package's own exception handlers — none of it is hand-written).
--
-- Shape of the traffic:
--   * ~48 hours of low background failures: roughly one declined operation per hour,
--     rotating through the realistic ways a bank op fails (insufficient funds, frozen
--     account, expired/blocked card, daily-limit, unknown beneficiary).
--   * ONE incident hour (~6h ago): the payment gateway goes down and ~20 card charges
--     time out in that single hour -> a clear spike from CORE_BANKING.CHARGE_CARD.
--   * the monthly-interest batch runs once (a JOB_RUN_LOG row + one failed loan).
--   * a little successful traffic at "now" so the ledger isn't all failures.
--
-- The package's set_sim_clock hook backdates the log timestamps so the history can be
-- replayed in one run. Idempotent: clears the rows it owns first, so re-run any time.
--
-- Run as BANKDEMO (so the logged db_user is the app account), AFTER bank_demo.sql:
--   sqlplus -s bankdemo/oracle@//localhost:1521/XEPDB1 @/tmp/bank_workload.sql

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET SERVEROUTPUT ON

-- Reset anything from a previous run (my synthetic filler at >=1000 and prior bank runs at >=100000).
DELETE FROM applog.error_log   WHERE error_id >= 1000;
DELETE FROM applog.job_run_log WHERE run_id   >= 100000;
DELETE FROM applog.audit_trail WHERE audit_id >= 100000;
COMMIT;

DECLARE
   v_when TIMESTAMP;

   -- Run one operation and swallow the (expected) app exception, so the loop keeps going.
   -- (Variables must be declared before nested subprograms in PL/SQL, hence v_when above.)
   PROCEDURE quietly_withdraw(p_acct NUMBER, p_amt NUMBER) IS
   BEGIN bankdemo.core_banking.withdraw(p_acct, p_amt); EXCEPTION WHEN OTHERS THEN NULL; END;
   PROCEDURE quietly_transfer(p_from NUMBER, p_to NUMBER, p_amt NUMBER) IS
   BEGIN bankdemo.core_banking.transfer_funds(p_from, p_to, p_amt); EXCEPTION WHEN OTHERS THEN NULL; END;
   PROCEDURE quietly_charge(p_card NUMBER, p_amt NUMBER, p_merchant VARCHAR2,
                            p_fail BOOLEAN DEFAULT FALSE) IS
   BEGIN bankdemo.core_banking.charge_card(p_card, p_amt, p_merchant, p_fail);
   EXCEPTION WHEN OTHERS THEN NULL; END;
BEGIN
   -- ---- 48 hours of steady background traffic (newest = 1h ago) -------------------
   FOR h IN REVERSE 1 .. 48 LOOP
      v_when := SYSTIMESTAMP - NUMTODSINTERVAL(h, 'HOUR');
      bankdemo.core_banking.set_sim_clock(v_when);

      -- A couple of operations that SUCCEED (no error logged), to keep the ledger realistic.
      bankdemo.core_banking.deposit(80001, 100 + MOD(h, 7) * 10, 'salary run');
      quietly_charge(9001, 40 + MOD(h, 5) * 5, 'GROCERY-' || MOD(h, 3));

      -- One declined operation per hour, rotating the failure mode (each logs exactly one row).
      CASE MOD(h, 6)
         WHEN 0 THEN quietly_withdraw(80005, 500);                      -- insufficient funds
         WHEN 1 THEN quietly_withdraw(80004, 100);                      -- account frozen
         WHEN 2 THEN quietly_charge(9004, 50, 'ONLINE-SHOP');           -- card expired
         WHEN 3 THEN quietly_charge(9003, 50, 'ONLINE-SHOP');           -- card blocked
         WHEN 4 THEN quietly_charge(9002, 9000, 'ELECTRONICS');         -- daily limit exceeded
         WHEN 5 THEN quietly_transfer(80001, 80099, 100);              -- beneficiary not found
      END CASE;

      -- Run the monthly-interest batch once, partway through the window (JOB row + 1 failed loan).
      IF h = 30 THEN
         bankdemo.core_banking.apply_monthly_interest;
      END IF;
   END LOOP;

   -- ---- THE INCIDENT: payment gateway down for one hour, ~6h ago --------------------
   v_when := SYSTIMESTAMP - NUMTODSINTERVAL(6, 'HOUR');
   bankdemo.core_banking.set_sim_clock(v_when);
   FOR k IN 1 .. 22 LOOP
      -- Real, active card + funded account: the ONLY reason these fail is the gateway timeout.
      quietly_charge(9001, 25 + MOD(k, 9) * 3, 'ACME-STORE', p_fail => TRUE);
   END LOOP;

   -- ---- a little live traffic at the real "now" (clock cleared) ---------------------
   bankdemo.core_banking.clear_sim_clock;
   bankdemo.core_banking.deposit(80002, 75, 'refund');
   quietly_withdraw(80005, 999);          -- one fresh insufficient-funds decline, logged at now
   quietly_charge(9002, 30, 'CAFE');

   COMMIT;
END;
/

SET HEADING ON
SET FEEDBACK ON
-- Confirmation: how many errors the run produced and over how many hourly buckets (the gateway
-- incident shows up as one bucket far above the rest). Inspect the spike with:
--   blossa logs --spikes        (or the "Show spikes" button in the web Logs tab)
SELECT COUNT(*) AS errors_logged, COUNT(DISTINCT TO_CHAR(log_time,'YYYY-MM-DD HH24')) AS hours, MAX(module) keep_any FROM applog.error_log WHERE error_id >= 100000;

exit
