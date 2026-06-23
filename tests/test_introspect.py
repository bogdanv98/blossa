"""Introspection assembly logic, exercised with a fake QueryExecutor (no Oracle needed)."""

from blossa.db.introspect import introspect_schema, list_non_system_schemas


class FakeDB:
    """Returns canned ALL_* rows based on which view the SQL references."""

    def query(self, sql: str, binds=None):
        s = sql.upper()
        if "ALL_TABLES" in s:
            return [
                {"TABLE_NAME": "CUSTOMERS", "NUM_ROWS": 4},
                {"TABLE_NAME": "ORDERS", "NUM_ROWS": 3},
            ]
        if "ALL_TAB_COMMENTS" in s:
            return [{"TABLE_NAME": "CUSTOMERS", "COMMENTS": "Customer master."}]
        if "ALL_COL_COMMENTS" in s:
            return [{"TABLE_NAME": "CUSTOMERS", "COLUMN_NAME": "CUST_ID", "COMMENTS": "PK."}]
        if "ALL_TAB_COLUMNS" in s:
            return [
                {"TABLE_NAME": "CUSTOMERS", "COLUMN_NAME": "CUST_ID", "COLUMN_ID": 1,
                 "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "DATA_PRECISION": 10,
                 "DATA_SCALE": 0, "NULLABLE": "N", "DATA_DEFAULT": None},
                {"TABLE_NAME": "CUSTOMERS", "COLUMN_NAME": "NAME", "COLUMN_ID": 2,
                 "DATA_TYPE": "VARCHAR2", "DATA_LENGTH": 120, "DATA_PRECISION": None,
                 "DATA_SCALE": None, "NULLABLE": "N", "DATA_DEFAULT": None},
                {"TABLE_NAME": "ORDERS", "COLUMN_NAME": "ORDER_ID", "COLUMN_ID": 1,
                 "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "DATA_PRECISION": 12,
                 "DATA_SCALE": 0, "NULLABLE": "N", "DATA_DEFAULT": None},
                {"TABLE_NAME": "ORDERS", "COLUMN_NAME": "CUST_ID", "COLUMN_ID": 2,
                 "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22, "DATA_PRECISION": 10,
                 "DATA_SCALE": 0, "NULLABLE": "N", "DATA_DEFAULT": None},
            ]
        if "ALL_CONSTRAINTS" in s:
            return [
                {"CONSTRAINT_NAME": "PK_CUSTOMERS", "TABLE_NAME": "CUSTOMERS",
                 "CONSTRAINT_TYPE": "P", "STATUS": "ENABLED", "SEARCH_CONDITION": None,
                 "R_OWNER": None, "R_CONSTRAINT_NAME": None},
                {"CONSTRAINT_NAME": "PK_ORDERS", "TABLE_NAME": "ORDERS",
                 "CONSTRAINT_TYPE": "P", "STATUS": "ENABLED", "SEARCH_CONDITION": None,
                 "R_OWNER": None, "R_CONSTRAINT_NAME": None},
                {"CONSTRAINT_NAME": "FK_ORDERS_CUST", "TABLE_NAME": "ORDERS",
                 "CONSTRAINT_TYPE": "R", "STATUS": "ENABLED", "SEARCH_CONDITION": None,
                 "R_OWNER": "BLOSSA_DEMO", "R_CONSTRAINT_NAME": "PK_CUSTOMERS"},
            ]
        if "ALL_CONS_COLUMNS" in s:
            return [
                {"CONSTRAINT_NAME": "PK_CUSTOMERS", "TABLE_NAME": "CUSTOMERS",
                 "COLUMN_NAME": "CUST_ID", "POSITION": 1},
                {"CONSTRAINT_NAME": "PK_ORDERS", "TABLE_NAME": "ORDERS",
                 "COLUMN_NAME": "ORDER_ID", "POSITION": 1},
                {"CONSTRAINT_NAME": "FK_ORDERS_CUST", "TABLE_NAME": "ORDERS",
                 "COLUMN_NAME": "CUST_ID", "POSITION": 1},
            ]
        if "ALL_INDEXES" in s:
            return [{"INDEX_NAME": "IX_ORDERS_CUST", "TABLE_NAME": "ORDERS",
                     "UNIQUENESS": "NONUNIQUE"}]
        if "ALL_IND_COLUMNS" in s:
            return [{"INDEX_NAME": "IX_ORDERS_CUST", "TABLE_NAME": "ORDERS",
                     "COLUMN_NAME": "CUST_ID", "COLUMN_POSITION": 1}]
        return []


def test_introspect_assembles_tables_columns_and_fk():
    schema = introspect_schema(FakeDB(), "BLOSSA_DEMO")
    assert {t.name for t in schema.tables} == {"CUSTOMERS", "ORDERS"}

    customers = schema.table("CUSTOMERS")
    assert customers.comment == "Customer master."
    assert customers.num_rows == 4
    assert customers.primary_key.columns == ["CUST_ID"]
    assert customers.column("CUST_ID").comment == "PK."
    assert customers.column("CUST_ID").type_signature == "NUMBER(10,0)"
    assert customers.column("NAME").type_signature == "VARCHAR2(120)"

    orders = schema.table("ORDERS")
    fks = orders.foreign_keys
    assert len(fks) == 1
    assert fks[0].columns == ["CUST_ID"]
    assert fks[0].referenced_table == "CUSTOMERS"
    assert fks[0].referenced_columns == ["CUST_ID"]
    assert orders.indexes[0].columns == ["CUST_ID"]


# --------------------------------------------------------- "*" non-system schema discovery


class _MaintainedDB:
    """A 12.2+ database: ALL_USERS exposes ORACLE_MAINTAINED, so the flag query succeeds."""

    def query(self, sql: str, binds=None):
        if "ORACLE_MAINTAINED" in sql.upper():
            return [{"OWNER": "CFKDEMO"}, {"OWNER": "EXTSALES"}, {"OWNER": "HR"}]
        raise AssertionError("blocklist fallback should not run when the flag query works")


class _LegacyDB:
    """A pre-12.2 database: querying ORACLE_MAINTAINED raises, forcing the blocklist fallback."""

    def query(self, sql: str, binds=None):
        if "ORACLE_MAINTAINED" in sql.upper():
            raise RuntimeError("ORA-00904: invalid identifier")
        # The fallback's ALL_TABLES query: SYS is in the blocklist, so it must not appear here.
        return [{"OWNER": "HR"}, {"OWNER": "CFKDEMO"}]


def test_non_system_schemas_prefers_oracle_maintained_flag():
    assert list_non_system_schemas(_MaintainedDB()) == ["CFKDEMO", "EXTSALES", "HR"]


def test_non_system_schemas_falls_back_to_blocklist_pre_12_2():
    assert list_non_system_schemas(_LegacyDB()) == ["HR", "CFKDEMO"]


def test_blocklist_fallback_query_excludes_system_owners():
    """The fallback SQL must carry the blocklist so Oracle-maintained owners can't slip through."""
    captured = {}

    class _Capture:
        def query(self, sql: str, binds=None):
            if "ORACLE_MAINTAINED" in sql.upper():
                raise RuntimeError("no such column")
            captured["sql"] = sql
            return []

    list_non_system_schemas(_Capture())
    assert "'SYS'" in captured["sql"] and "'SYSTEM'" in captured["sql"]
    assert "APEX_%" in captured["sql"]
