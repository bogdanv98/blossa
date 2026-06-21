-- cross_schema_demo.sql ---------------------------------------------------------
-- A synthetic schema (EXTSALES) whose only point is to hold CROSS-SCHEMA foreign keys
-- into HR, so we can measure multi-schema FK rediscovery (Blossa scanning {HR, EXTSALES}
-- together). HR must already be installed.
--
--   EXTSALES.SALES_CONTACTS.location_id -> HR.LOCATIONS(location_id)   (exact-name match)
--   EXTSALES.SALES_CONTACTS.rep_id      -> HR.EMPLOYEES(employee_id)   (suffix "_ID" + data)
--
-- Run as SYSTEM (needs to grant REFERENCES on HR objects):
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/cross_schema_demo.sql

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET SERVEROUTPUT ON

DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'EXTSALES';
   IF n > 0 THEN
      EXECUTE IMMEDIATE 'DROP USER EXTSALES CASCADE';
   END IF;
END;
/

CREATE USER extsales IDENTIFIED BY "oracle"
               DEFAULT TABLESPACE USERS
               QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE TO extsales;

-- EXTSALES needs REFERENCES on the HR tables it points at (and SELECT for value overlap).
GRANT REFERENCES, SELECT ON hr.employees TO extsales;
GRANT REFERENCES, SELECT ON hr.locations TO extsales;

ALTER SESSION SET CURRENT_SCHEMA=EXTSALES;

CREATE TABLE sales_contacts (
   contact_id    NUMBER(8)  NOT NULL,
   contact_name  VARCHAR2(40),
   rep_id        NUMBER(6),
   location_id   NUMBER(4),
   CONSTRAINT pk_sales_contacts PRIMARY KEY (contact_id),
   CONSTRAINT fk_sc_rep      FOREIGN KEY (rep_id)      REFERENCES hr.employees (employee_id),
   CONSTRAINT fk_sc_location FOREIGN KEY (location_id) REFERENCES hr.locations (location_id)
);
COMMENT ON TABLE  sales_contacts              IS 'External sales contacts, tied to an HR rep and office.';
COMMENT ON COLUMN sales_contacts.contact_id   IS 'Surrogate key of the contact.';
COMMENT ON COLUMN sales_contacts.contact_name IS 'Display name of the contact.';
COMMENT ON COLUMN sales_contacts.rep_id       IS 'HR employee who owns this contact.';
COMMENT ON COLUMN sales_contacts.location_id  IS 'HR location of the contact''s office.';

-- rep_id / location_id values must exist in HR (FKs are validated on insert).
INSERT INTO sales_contacts VALUES (1, 'Acme',    100, 1000);
INSERT INTO sales_contacts VALUES (2, 'Globex',  101, 1100);
INSERT INTO sales_contacts VALUES (3, 'Initech', 102, 1200);
COMMIT;

SET HEADING ON
SELECT 'sales_contacts' AS "Table", COUNT(1) AS "rows" FROM sales_contacts;

exit
