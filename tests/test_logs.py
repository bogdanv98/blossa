"""Deterministic log-table detection: recognise error/audit/job logs and tag their columns."""

from blossa.logs import classify_table, detect_log_tables
from blossa.models import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    LogKind,
    LogRole,
    SchemaInfo,
    TableInfo,
)


def _col(name, data_type="VARCHAR2", length=None):
    return ColumnInfo(name=name, data_type=data_type, data_length=length)


def _pk(*cols):
    return ConstraintInfo(name="pk", type=ConstraintType.PRIMARY_KEY, columns=list(cols))


def _fk(cols, ref_table, ref_cols):
    return ConstraintInfo(
        name="fk", type=ConstraintType.FOREIGN_KEY, columns=list(cols),
        referenced_table=ref_table, referenced_columns=list(ref_cols),
    )


def _error_log():
    return TableInfo(
        name="ERROR_LOG",
        owner="APPLOG",
        columns=[
            _col("ERROR_ID", "NUMBER"),
            _col("LOG_TIME", "TIMESTAMP"),
            _col("SEVERITY", "VARCHAR2", 10),
            _col("ERROR_CODE", "NUMBER"),
            _col("MODULE", "VARCHAR2", 80),
            _col("MESSAGE", "VARCHAR2", 2000),
            _col("DB_USER", "VARCHAR2", 30),
            _col("ORDER_ID", "NUMBER"),
        ],
        constraints=[_pk("ERROR_ID"), _fk(["ORDER_ID"], "APP_ORDERS", ["ORDER_ID"])],
    )


def _audit_trail():
    return TableInfo(
        name="AUDIT_TRAIL",
        owner="APPLOG",
        columns=[
            _col("AUDIT_ID", "NUMBER"),
            _col("EVENT_TIME", "TIMESTAMP"),
            _col("ACTION", "VARCHAR2", 10),
            _col("OBJECT_NAME", "VARCHAR2", 40),
            _col("ROW_PK", "VARCHAR2", 40),
            _col("CHANGED_BY", "VARCHAR2", 30),
            _col("DETAILS", "CLOB"),
        ],
        constraints=[_pk("AUDIT_ID")],
    )


def _job_run_log():
    return TableInfo(
        name="JOB_RUN_LOG",
        owner="APPLOG",
        columns=[
            _col("RUN_ID", "NUMBER"),
            _col("JOB_NAME", "VARCHAR2", 60),
            _col("STARTED_AT", "TIMESTAMP"),
            _col("FINISHED_AT", "TIMESTAMP"),
            _col("STATUS", "VARCHAR2", 12),
            _col("ROWS_PROCESSED", "NUMBER"),
            _col("ERROR_TEXT", "VARCHAR2", 2000),
        ],
        constraints=[_pk("RUN_ID")],
    )


def _app_orders():
    return TableInfo(
        name="APP_ORDERS",
        owner="APPLOG",
        columns=[
            _col("ORDER_ID", "NUMBER"),
            _col("CUSTOMER", "VARCHAR2", 60),
            _col("AMOUNT", "NUMBER"),
            _col("STATUS", "VARCHAR2", 20),
        ],
        constraints=[_pk("ORDER_ID")],
    )


def _roles(log_table):
    return {lc.role: lc.column for lc in log_table.columns}


def test_error_log_detected_with_roles():
    lt = classify_table(_error_log())
    assert lt is not None
    assert lt.kind == LogKind.ERROR
    assert lt.confidence.value == "high"  # loggy name + timestamp + message
    roles = _roles(lt)
    assert roles[LogRole.EVENT_TIME] == "LOG_TIME"
    assert roles[LogRole.MESSAGE] == "MESSAGE"
    assert roles[LogRole.SEVERITY] == "SEVERITY"
    assert roles[LogRole.SOURCE] == "MODULE"
    assert roles[LogRole.ACTOR] == "DB_USER"
    assert roles[LogRole.CODE] == "ERROR_CODE"
    assert roles[LogRole.BUSINESS_REF] == "ORDER_ID"  # the FK to the business table


def test_audit_trail_detected_via_clob_message_and_kind():
    lt = classify_table(_audit_trail())
    assert lt is not None and lt.kind == LogKind.AUDIT
    roles = _roles(lt)
    assert roles[LogRole.EVENT_TIME] == "EVENT_TIME"
    assert roles[LogRole.MESSAGE] == "DETAILS"  # CLOB read as the message column
    assert roles[LogRole.ACTOR] == "CHANGED_BY"  # _BY suffix
    assert roles[LogRole.SOURCE] in {"ACTION", "OBJECT_NAME"}


def test_job_run_log_detected_as_job_kind():
    lt = classify_table(_job_run_log())
    assert lt is not None and lt.kind == LogKind.JOB
    roles = _roles(lt)
    assert lt.column_for(LogRole.MESSAGE) == "ERROR_TEXT"
    assert roles[LogRole.SEVERITY] == "STATUS"
    assert roles[LogRole.SOURCE] == "JOB_NAME"


def test_business_table_is_not_a_log():
    # APP_ORDERS has a STATUS column (severity-shaped) but no timestamp + message → not a log.
    assert classify_table(_app_orders()) is None


def test_detect_over_schema_finds_only_the_logs():
    schema = SchemaInfo(
        name="APPLOG",
        tables=[_app_orders(), _error_log(), _audit_trail(), _job_run_log()],
    )
    found = {lt.table for lt in detect_log_tables(schema)}
    assert found == {"ERROR_LOG", "AUDIT_TRAIL", "JOB_RUN_LOG"}


def test_markdown_renders_application_logs_section():
    from datetime import UTC, datetime

    from blossa.models import ScanMetadata, ScanReport
    from blossa.render import render_markdown

    schema = SchemaInfo(name="APPLOG", tables=[_error_log()])
    report = ScanReport(
        metadata=ScanMetadata(
            blossa_version="0", schema_name="APPLOG",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            llm_provider="heuristic", table_count=1,
        ),
        schema_info=schema,
        log_tables=detect_log_tables(schema),
    )
    md = render_markdown(report)
    assert "## Application logs" in md
    assert "ERROR_LOG" in md
    assert "event_time → `LOG_TIME`" in md  # role → column mapping is rendered


def test_shape_only_log_without_loggy_name():
    # No log-ish name, but a timestamp + a wide free-text column → still recognised (medium conf).
    t = TableInfo(
        name="REQUEST_OUTCOMES",
        owner="APP",
        columns=[
            _col("ID", "NUMBER"),
            _col("CREATED_AT", "TIMESTAMP"),
            _col("DETAIL", "VARCHAR2", 1000),
        ],
        constraints=[_pk("ID")],
    )
    lt = classify_table(t)
    assert lt is not None
    assert lt.confidence.value == "medium"  # shape matched, name did not
