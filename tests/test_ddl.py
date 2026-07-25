"""DDL retrieval: the DBMS_METADATA plumbing and the offline CREATE TABLE reconstruction."""

from datetime import datetime

import pytest

from blossa.ddl import (
    GET_DDL_SQL,
    metadata_type,
    offline_ddl,
    split_qualified,
    synthesize_table_ddl,
    validate_identifier,
)
from blossa.demo import build_demo_schema
from blossa.models import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    IndexInfo,
    ProgramKind,
    ProgramUnit,
    ScanMetadata,
    ScanReport,
    TableInfo,
)


def _report() -> ScanReport:
    schema = build_demo_schema()
    return ScanReport(
        metadata=ScanMetadata(
            blossa_version="test",
            schema_name=schema.name,
            generated_at=datetime(2026, 7, 25, 12, 0),
            llm_provider="heuristic",
        ),
        schema_info=schema,
    )


def _table() -> TableInfo:
    return TableInfo(
        name="ORDERS",
        owner="HR",
        comment="Customer orders.",
        columns=[
            ColumnInfo(name="ORDER_ID", data_type="NUMBER", data_precision=12, data_scale=0,
                       nullable=False),
            ColumnInfo(name="CUST_ID", data_type="NUMBER", data_precision=10, data_scale=0,
                       nullable=False, comment="Who ordered."),
            ColumnInfo(name="STATUS", data_type="VARCHAR2", data_length=20, nullable=True,
                       data_default="'NEW'"),
        ],
        constraints=[
            ConstraintInfo(name="PK_ORDERS", type=ConstraintType.PRIMARY_KEY,
                           columns=["ORDER_ID"]),
            ConstraintInfo(name="FK_ORDERS_CUST", type=ConstraintType.FOREIGN_KEY,
                           columns=["CUST_ID"], referenced_table="CUSTOMERS",
                           referenced_columns=["CUST_ID"]),
            ConstraintInfo(name="CK_STATUS", type=ConstraintType.CHECK,
                           search_condition="STATUS IN ('NEW','SENT')"),
            # Oracle records every NOT NULL as a check constraint; it is already on the column.
            ConstraintInfo(name="SYS_C007", type=ConstraintType.CHECK,
                           search_condition='"ORDER_ID" IS NOT NULL'),
        ],
        indexes=[
            IndexInfo(name="PK_ORDERS", unique=True, columns=["ORDER_ID"]),
            IndexInfo(name="IX_ORDERS_CUST", unique=False, columns=["CUST_ID"]),
        ],
    )


def test_synthesized_ddl_has_columns_keys_and_comments():
    ddl = synthesize_table_ddl(_table())
    assert "CREATE TABLE HR.ORDERS (" in ddl
    assert "ORDER_ID  NUMBER(12,0) NOT NULL" in ddl
    assert "STATUS    VARCHAR2(20) DEFAULT 'NEW'" in ddl
    assert "CONSTRAINT PK_ORDERS PRIMARY KEY (ORDER_ID)" in ddl
    assert "CONSTRAINT FK_ORDERS_CUST FOREIGN KEY (CUST_ID) REFERENCES CUSTOMERS (CUST_ID)" in ddl
    assert "CONSTRAINT CK_STATUS CHECK (STATUS IN ('NEW','SENT'))" in ddl
    assert "COMMENT ON TABLE HR.ORDERS IS 'Customer orders.';" in ddl
    assert "COMMENT ON COLUMN HR.ORDERS.CUST_ID IS 'Who ordered.';" in ddl
    # It says what it is, so nobody mistakes a reconstruction for Oracle's own text.
    assert ddl.startswith("-- Reconstructed by Blossa")


def test_synthesized_ddl_skips_noise_constraints_and_backing_indexes():
    ddl = synthesize_table_ddl(_table())
    assert "SYS_C007" not in ddl  # the NOT NULL check is rendered on the column line instead
    assert "CREATE INDEX IX_ORDERS_CUST ON HR.ORDERS (CUST_ID);" in ddl
    assert "CREATE UNIQUE INDEX PK_ORDERS" not in ddl  # the PK constraint already creates it


def test_comment_quotes_are_escaped():
    table = _table()
    table.comment = "O'Brien's orders"
    assert "IS 'O''Brien''s orders';" in synthesize_table_ddl(table)


@pytest.mark.parametrize(
    ("object_type", "expected"),
    [("TABLE", "TABLE"), ("materialized view", "MATERIALIZED_VIEW"), ("SEQUENCE", "SEQUENCE")],
)
def test_metadata_type_maps_catalog_types(object_type, expected):
    assert metadata_type(object_type) == expected


def test_metadata_type_is_none_for_unknown_kinds():
    assert metadata_type("DATABASE LINK") is None
    assert metadata_type("") is None


def test_get_ddl_sql_binds_every_identifier():
    # Nothing is concatenated into the statement, so an object name can't carry SQL into it.
    assert ":otype" in GET_DDL_SQL and ":name" in GET_DDL_SQL and ":owner" in GET_DDL_SQL


def test_split_qualified_handles_both_name_forms():
    assert split_qualified("HR.EMPLOYEES") == ("HR", "EMPLOYEES")
    assert split_qualified("employees") == (None, "EMPLOYEES")


def test_validate_identifier_rejects_injection():
    assert validate_identifier("hr") == "HR"
    with pytest.raises(ValueError, match="Invalid"):
        validate_identifier("EMP; DROP TABLE X")


def test_offline_ddl_returns_captured_program_source():
    report = _report()
    report.schema_info.program_units.append(
        ProgramUnit(name="EMP_V", owner="BLOSSA_DEMO", kind=ProgramKind.VIEW,
                    source="SELECT * FROM CUSTOMERS")
    )
    assert offline_ddl(report, "BLOSSA_DEMO", "EMP_V", "VIEW") == "SELECT * FROM CUSTOMERS"
    assert offline_ddl(report, "BLOSSA_DEMO", "NO_SUCH", "VIEW") == ""


def test_offline_ddl_synthesizes_tables_from_the_map():
    report = _report()
    name = report.schema_info.tables[0].name
    assert "CREATE TABLE" in offline_ddl(report, None, name, "TABLE")
