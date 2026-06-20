-- legacy_ify.sql --------------------------------------------------------------
-- Turn a documented schema into a "legacy" one: drop all FOREIGN KEY constraints
-- and remove all table/column comments. This simulates the undocumented estates
-- Blossa is built to reverse-engineer, while you keep the original (captured with
-- `blossa ground-truth`) to evaluate against.
--
-- Run connected AS THE SCHEMA OWNER you want to break, e.g.:
--   docker exec -i blossa-oracle sqlplus HR/oracle@//localhost:1521/XEPDB1 @/tmp/legacy_ify.sql
--
-- DO NOT run this against anything you care about — it is destructive (by design,
-- on throwaway sample data only).

SET SERVEROUTPUT ON
DECLARE
    n_fk   PLS_INTEGER := 0;
    n_tcom PLS_INTEGER := 0;
    n_ccom PLS_INTEGER := 0;
BEGIN
    -- 1) Drop foreign keys (so Blossa has to re-infer relationships).
    FOR c IN (SELECT table_name, constraint_name
                FROM user_constraints
               WHERE constraint_type = 'R') LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE "' || c.table_name ||
                          '" DROP CONSTRAINT "' || c.constraint_name || '"';
        n_fk := n_fk + 1;
    END LOOP;

    -- 2) Remove table comments.
    FOR t IN (SELECT table_name FROM user_tables) LOOP
        EXECUTE IMMEDIATE 'COMMENT ON TABLE "' || t.table_name || '" IS ' || q'['']';
        n_tcom := n_tcom + 1;
    END LOOP;

    -- 3) Remove column comments.
    FOR c IN (SELECT table_name, column_name FROM user_tab_columns) LOOP
        EXECUTE IMMEDIATE 'COMMENT ON COLUMN "' || c.table_name || '"."' ||
                          c.column_name || '" IS ' || q'['']';
        n_ccom := n_ccom + 1;
    END LOOP;

    DBMS_OUTPUT.PUT_LINE('Dropped ' || n_fk || ' foreign keys.');
    DBMS_OUTPUT.PUT_LINE('Cleared comments on ' || n_tcom || ' tables, ' ||
                         n_ccom || ' columns.');
END;
/
