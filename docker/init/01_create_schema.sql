-- Synthetic BLOSSA_DEMO schema -------------------------------------------------
-- A small, deliberately under-documented "orders" domain with the kinds of legacy
-- quirks Blossa is meant to surface:
--   * tables and columns with NO comments (and a couple WITH comments, for contrast)
--   * one UNDECLARED foreign key (ORDER_ITEMS.PROD_ID -> PRODUCTS.PROD_ID)
--   * orphan rows behind that undeclared FK
--   * a type inconsistency (CUST_ID is NUMBER in most tables, VARCHAR2 in CUST_NOTES)
--   * mixed naming conventions (*_DT vs *_DATE, STATUS vs STATUS_CD)
--   * PII-looking columns (NAME, EMAIL, PHONE) to exercise masking / PII-safety
--
-- Runs once at container creation, connected as BLOSSA_DEMO into XEPDB1.

-- 1. Lookup of order statuses (cryptic legacy codes). Intentionally NO comments.
CREATE TABLE STATUS_REF (
    STATUS_CD   NUMBER(2)      NOT NULL,
    DESCR       VARCHAR2(40)   NOT NULL,
    CONSTRAINT PK_STATUS_REF PRIMARY KEY (STATUS_CD)
);

-- 2. Customers. This table and a couple of its columns ARE documented.
CREATE TABLE CUSTOMERS (
    CUST_ID     NUMBER(10)     NOT NULL,
    NAME        VARCHAR2(120)  NOT NULL,
    EMAIL       VARCHAR2(120),
    PHONE       VARCHAR2(30),
    CREATED_DT  DATE           DEFAULT SYSDATE NOT NULL,
    STATUS_CD   NUMBER(2)      DEFAULT 1 NOT NULL,
    CONSTRAINT PK_CUSTOMERS PRIMARY KEY (CUST_ID)
);

-- 3. Products. Undocumented. CAT_CD is a low-cardinality category code.
CREATE TABLE PRODUCTS (
    PROD_ID     NUMBER(10)     NOT NULL,
    SKU         VARCHAR2(20)   NOT NULL,
    DESCR       VARCHAR2(200),
    PRICE       NUMBER(10,2),
    CAT_CD      VARCHAR2(4),
    CONSTRAINT PK_PRODUCTS PRIMARY KEY (PROD_ID),
    CONSTRAINT UQ_PRODUCTS_SKU UNIQUE (SKU)
);

-- 4. Orders. CUST_ID is a DECLARED FK. STATUS_CD points at STATUS_REF but is NOT declared.
CREATE TABLE ORDERS (
    ORDER_ID    NUMBER(12)     NOT NULL,
    CUST_ID     NUMBER(10)     NOT NULL,
    ORDER_DATE  DATE           DEFAULT SYSDATE NOT NULL,   -- note: *_DATE, not *_DT
    TOTAL_AMT   NUMBER(12,2),
    STATUS_CD   NUMBER(2)      DEFAULT 1 NOT NULL,
    CONSTRAINT PK_ORDERS PRIMARY KEY (ORDER_ID),
    CONSTRAINT FK_ORDERS_CUST FOREIGN KEY (CUST_ID) REFERENCES CUSTOMERS (CUST_ID)
);

-- 5. Order line items. ORDER_ID is a DECLARED FK.
--    PROD_ID references PRODUCTS but is intentionally NOT declared as a foreign key.
CREATE TABLE ORDER_ITEMS (
    ITEM_ID     NUMBER(12)     NOT NULL,
    ORDER_ID    NUMBER(12)     NOT NULL,
    PROD_ID     NUMBER(10)     NOT NULL,
    QTY         NUMBER(6)      DEFAULT 1 NOT NULL,
    UNIT_PRICE  NUMBER(10,2),
    CONSTRAINT PK_ORDER_ITEMS PRIMARY KEY (ITEM_ID),
    CONSTRAINT FK_ITEMS_ORDER FOREIGN KEY (ORDER_ID) REFERENCES ORDERS (ORDER_ID)
);

-- 6. Legacy free-text notes. CUST_ID here is VARCHAR2 (type inconsistency) and is an
--    undeclared reference back to CUSTOMERS. Cryptic, undocumented.
CREATE TABLE CUST_NOTES (
    NOTE_ID     NUMBER(12)     NOT NULL,
    CUST_ID     VARCHAR2(20)   NOT NULL,
    NOTE_TXT    VARCHAR2(400),
    CONSTRAINT PK_CUST_NOTES PRIMARY KEY (NOTE_ID)
);

-- Comments: deliberately partial. CUSTOMERS is documented; the rest mostly are not.
COMMENT ON TABLE CUSTOMERS IS 'Master record of customers placing orders.';
COMMENT ON COLUMN CUSTOMERS.CUST_ID IS 'Surrogate primary key for a customer.';
COMMENT ON COLUMN CUSTOMERS.EMAIL IS 'Primary contact email address.';
COMMENT ON COLUMN ORDERS.TOTAL_AMT IS 'Order gross total in account currency.';

-- Indexes (helps the introspector show non-PK access paths).
CREATE INDEX IX_ORDERS_CUST ON ORDERS (CUST_ID);
CREATE INDEX IX_ITEMS_ORDER ON ORDER_ITEMS (ORDER_ID);
CREATE INDEX IX_ITEMS_PROD ON ORDER_ITEMS (PROD_ID);

-- --- Seed data ---------------------------------------------------------------

INSERT INTO STATUS_REF (STATUS_CD, DESCR) VALUES (1, 'NEW');
INSERT INTO STATUS_REF (STATUS_CD, DESCR) VALUES (2, 'PAID');
INSERT INTO STATUS_REF (STATUS_CD, DESCR) VALUES (3, 'SHIPPED');
INSERT INTO STATUS_REF (STATUS_CD, DESCR) VALUES (7, 'CANCELLED');

INSERT INTO CUSTOMERS (CUST_ID, NAME, EMAIL, PHONE, STATUS_CD) VALUES (1001, 'Acme Trading SRL', 'orders@acme.example', '+40 21 555 0101', 1);
INSERT INTO CUSTOMERS (CUST_ID, NAME, EMAIL, PHONE, STATUS_CD) VALUES (1002, 'Beta Logistics GmbH', 'beta.ops@beta.example', '+49 30 555 0202', 1);
INSERT INTO CUSTOMERS (CUST_ID, NAME, EMAIL, PHONE, STATUS_CD) VALUES (1003, 'Carmen Ionescu', 'carmen.i@mail.example', '+40 745 555 303', 2);
INSERT INTO CUSTOMERS (CUST_ID, NAME, EMAIL, PHONE, STATUS_CD) VALUES (1004, 'Delta Retail PLC', 'ap@delta.example', NULL, 1);

INSERT INTO PRODUCTS (PROD_ID, SKU, DESCR, PRICE, CAT_CD) VALUES (50, 'SKU-ABX-001', 'Widget, standard', 19.99, 'WID');
INSERT INTO PRODUCTS (PROD_ID, SKU, DESCR, PRICE, CAT_CD) VALUES (51, 'SKU-ABX-002', 'Widget, premium', 39.99, 'WID');
INSERT INTO PRODUCTS (PROD_ID, SKU, DESCR, PRICE, CAT_CD) VALUES (52, 'SKU-GZM-010', 'Gizmo, blue', 9.50, 'GIZ');
INSERT INTO PRODUCTS (PROD_ID, SKU, DESCR, PRICE, CAT_CD) VALUES (53, 'SKU-GZM-011', 'Gizmo, red', 9.50, 'GIZ');

INSERT INTO ORDERS (ORDER_ID, CUST_ID, ORDER_DATE, TOTAL_AMT, STATUS_CD) VALUES (900001, 1001, DATE '2023-01-15', 59.98, 2);
INSERT INTO ORDERS (ORDER_ID, CUST_ID, ORDER_DATE, TOTAL_AMT, STATUS_CD) VALUES (900002, 1002, DATE '2023-02-03', 19.99, 1);
INSERT INTO ORDERS (ORDER_ID, CUST_ID, ORDER_DATE, TOTAL_AMT, STATUS_CD) VALUES (900003, 1003, DATE '2023-02-20', 19.00, 3);

INSERT INTO ORDER_ITEMS (ITEM_ID, ORDER_ID, PROD_ID, QTY, UNIT_PRICE) VALUES (1, 900001, 50, 2, 19.99);
INSERT INTO ORDER_ITEMS (ITEM_ID, ORDER_ID, PROD_ID, QTY, UNIT_PRICE) VALUES (2, 900001, 51, 1, 39.99);
INSERT INTO ORDER_ITEMS (ITEM_ID, ORDER_ID, PROD_ID, QTY, UNIT_PRICE) VALUES (3, 900002, 50, 1, 19.99);
INSERT INTO ORDER_ITEMS (ITEM_ID, ORDER_ID, PROD_ID, QTY, UNIT_PRICE) VALUES (4, 900003, 52, 2, 9.50);
-- Orphan: PROD_ID 99 does not exist in PRODUCTS (undeclared FK has no DB enforcement).
INSERT INTO ORDER_ITEMS (ITEM_ID, ORDER_ID, PROD_ID, QTY, UNIT_PRICE) VALUES (5, 900003, 99, 1, 0.00);

INSERT INTO CUST_NOTES (NOTE_ID, CUST_ID, NOTE_TXT) VALUES (1, '1001', 'Net-30 terms agreed.');
INSERT INTO CUST_NOTES (NOTE_ID, CUST_ID, NOTE_TXT) VALUES (2, '1003', 'Prefers email contact.');

COMMIT;

-- Gather stats so ALL_TABLES.NUM_ROWS is populated for the introspector.
BEGIN
    DBMS_STATS.GATHER_SCHEMA_STATS(ownname => USER);
END;
/
