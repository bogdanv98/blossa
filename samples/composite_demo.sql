-- composite_demo.sql -----------------------------------------------------------
-- A small SYNTHETIC schema whose only point is to contain a genuine COMPOSITE
-- foreign key, because none of Oracle's sample schemas (HR/OE/CO/SH) have one and
-- we need a known-truth case to evaluate composite-FK rediscovery against.
--
-- ITEM_RETURNS(order_id, item_no) -> ORDER_ITEMS(order_id, item_no)  is the FK we
-- hide (via legacy_ify.sql) and expect Blossa to re-infer by EXACT column name.
--
-- RETURN_LINES(source_order_id, return_item_no) -> ORDER_ITEMS(order_id, item_no)  is a
-- second composite FK whose child columns are ROLE-NAMED (no exact-name overlap), so only
-- the suffix+type+tuple-overlap pass can rediscover it.
--
-- Run as a privileged user (SYSTEM):
--   sqlplus -s system/oracle@//localhost:1521/XEPDB1 @/tmp/composite_demo.sql
-- Password is fixed to "oracle" to match samples/cfk.yml.

WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET SERVEROUTPUT ON

DECLARE
   n NUMBER;
BEGIN
   SELECT COUNT(*) INTO n FROM all_users WHERE username = 'CFKDEMO';
   IF n > 0 THEN
      EXECUTE IMMEDIATE 'DROP USER CFKDEMO CASCADE';
   END IF;
END;
/

CREATE USER cfkdemo IDENTIFIED BY "oracle"
               DEFAULT TABLESPACE USERS
               QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE TO cfkdemo;
ALTER SESSION SET CURRENT_SCHEMA=CFKDEMO;

-- Parent: composite primary key (order_id, item_no).
CREATE TABLE order_items (
   order_id   NUMBER(8)  NOT NULL,
   item_no    NUMBER(4)  NOT NULL,
   product    VARCHAR2(40),
   CONSTRAINT pk_order_items PRIMARY KEY (order_id, item_no)
);
COMMENT ON TABLE  order_items          IS 'One line per product on a sales order.';
COMMENT ON COLUMN order_items.order_id IS 'Order this line belongs to.';
COMMENT ON COLUMN order_items.item_no  IS 'Line number within the order.';
COMMENT ON COLUMN order_items.product  IS 'Product name on this line.';

-- Child: composite FK (order_id, item_no) -> order_items.
CREATE TABLE item_returns (
   return_id   NUMBER(10) NOT NULL,
   order_id    NUMBER(8)  NOT NULL,
   item_no     NUMBER(4)  NOT NULL,
   return_qty  NUMBER(4),
   CONSTRAINT pk_item_returns PRIMARY KEY (return_id),
   CONSTRAINT fk_item_returns FOREIGN KEY (order_id, item_no)
      REFERENCES order_items (order_id, item_no)
);
COMMENT ON TABLE  item_returns            IS 'Returns logged against specific order lines.';
COMMENT ON COLUMN item_returns.return_id  IS 'Surrogate key of the return.';
COMMENT ON COLUMN item_returns.order_id   IS 'Order of the returned line.';
COMMENT ON COLUMN item_returns.item_no    IS 'Line number of the returned line.';
COMMENT ON COLUMN item_returns.return_qty IS 'Quantity returned.';

-- Second child: composite FK with ROLE-NAMED columns (no exact-name overlap with the parent key).
-- (source_order_id, return_item_no) -> order_items (order_id, item_no).
CREATE TABLE return_lines (
   return_id        NUMBER(10) NOT NULL,
   source_order_id  NUMBER(8)  NOT NULL,
   return_item_no   NUMBER(4)  NOT NULL,
   return_qty       NUMBER(4),
   CONSTRAINT pk_return_lines PRIMARY KEY (return_id),
   CONSTRAINT fk_return_lines FOREIGN KEY (source_order_id, return_item_no)
      REFERENCES order_items (order_id, item_no)
);
COMMENT ON TABLE  return_lines                 IS 'Returns keyed by role-named order-line columns.';
COMMENT ON COLUMN return_lines.return_id        IS 'Surrogate key of the return line.';
COMMENT ON COLUMN return_lines.source_order_id  IS 'Order of the returned line.';
COMMENT ON COLUMN return_lines.return_item_no   IS 'Line number of the returned line.';
COMMENT ON COLUMN return_lines.return_qty       IS 'Quantity returned.';

INSERT INTO order_items VALUES (1001, 1, 'Widget');
INSERT INTO order_items VALUES (1001, 2, 'Gadget');
INSERT INTO order_items VALUES (1002, 1, 'Sprocket');
INSERT INTO order_items VALUES (1002, 2, 'Cog');
INSERT INTO order_items VALUES (1003, 1, 'Bolt');

INSERT INTO item_returns VALUES (1, 1001, 1, 1);
INSERT INTO item_returns VALUES (2, 1001, 2, 1);
INSERT INTO item_returns VALUES (3, 1002, 1, 2);

INSERT INTO return_lines VALUES (1, 1001, 1, 1);
INSERT INTO return_lines VALUES (2, 1002, 2, 1);
INSERT INTO return_lines VALUES (3, 1003, 1, 1);
COMMIT;

SET HEADING ON
SELECT 'order_items' AS "Table", COUNT(1) AS "rows" FROM order_items
UNION ALL
SELECT 'item_returns', COUNT(1) FROM item_returns
UNION ALL
SELECT 'return_lines', COUNT(1) FROM return_lines;

exit
