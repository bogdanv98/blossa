"""Log-content analysis: redaction, deterministic SQL builders, the local-provider gate, parsing."""

from blossa.logsense import (
    LOCAL_PROVIDERS,
    build_root_cause_prompt,
    choose_log_table,
    is_local_provider,
    parse_root_cause_response,
    recent_entries_sql,
    redact_entries,
    redact_text,
    severity_breakdown_sql,
    source_breakdown_sql,
)
from blossa.models import ConfidenceLevel, LogColumn, LogKind, LogRole, LogTable


def _error_log(owner="APPLOG"):
    return LogTable(
        table="ERROR_LOG",
        owner=owner,
        kind=LogKind.ERROR,
        confidence=ConfidenceLevel.HIGH,
        columns=[
            LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
            LogColumn(column="SEVERITY", role=LogRole.SEVERITY),
            LogColumn(column="MODULE", role=LogRole.SOURCE),
            LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
        ],
    )


# --------------------------------------------------------------- redaction


def test_redact_text_masks_email_keeps_short_codes():
    out = redact_text("ORA-00600 while posting invoice for jane.doe@initech.com")
    assert "jane.doe@initech.com" not in out
    assert "ORA-00600" in out  # short diagnostic codes are preserved


def test_redact_text_masks_long_numbers_keeps_card_tail():
    out = redact_text("charged card 4111 1111 1111 1111 (ending 4321)")
    assert "4111 1111 1111 1111" not in out  # full number redacted
    assert "4321" in out  # last-4 tail is not, on its own, identifying


def test_redact_entries_only_touches_message_column():
    rows = [{"SEVERITY": "ERROR", "MESSAGE": "fail for a@b.com", "MODULE": "X"}]
    out = redact_entries(rows, "MESSAGE")
    assert out[0]["SEVERITY"] == "ERROR" and out[0]["MODULE"] == "X"
    assert "a@b.com" not in out[0]["MESSAGE"]


# --------------------------------------------------------------- SQL builders


def test_severity_breakdown_sql_uses_severity_column():
    sql = severity_breakdown_sql(_error_log())
    assert sql is not None
    assert "SEVERITY" in sql and "COUNT(*)" in sql and "GROUP BY SEVERITY" in sql
    assert "APPLOG.ERROR_LOG" in sql


def test_source_breakdown_filters_to_errors_by_default():
    sql = source_breakdown_sql(_error_log())
    assert "GROUP BY MODULE" in sql
    assert "UPPER(SEVERITY) IN" in sql  # only-errors filter applied
    assert "'FATAL'" in sql


def test_recent_entries_sql_orders_by_time_and_caps():
    sql = recent_entries_sql(_error_log(), limit=25)
    assert "ORDER BY LOG_TIME DESC" in sql
    assert "FETCH FIRST 25 ROWS ONLY" in sql
    assert "SUBSTR(MESSAGE, 1, 2000)" in sql  # message bounded, never the whole CLOB


def test_breakdowns_return_none_without_the_needed_column():
    plain = LogTable(
        table="EVENT_LOG",
        columns=[LogColumn(column="WHEN_TS", role=LogRole.EVENT_TIME),
                 LogColumn(column="BODY", role=LogRole.MESSAGE)],
    )
    assert severity_breakdown_sql(plain) is None  # no severity column
    assert source_breakdown_sql(plain) is None  # no source column
    # recent still works off the event-time + message it does have
    assert "FETCH FIRST" in recent_entries_sql(plain)


# --------------------------------------------------------------- local gate


class _Provider:
    def __init__(self, name):
        self.name = name
        self.model = "m"

    def generate(self, system, user):  # pragma: no cover - not called here
        return "{}"


def test_local_provider_gate():
    assert is_local_provider(_Provider("ollama")) is True
    assert is_local_provider(_Provider("openai_compatible")) is False
    assert "ollama" in LOCAL_PROVIDERS


# --------------------------------------------------------------- choose / parse


def test_choose_prefers_error_log_when_multiple():
    audit = LogTable(table="AUDIT_TRAIL", kind=LogKind.AUDIT, confidence=ConfidenceLevel.HIGH)
    err = _error_log()
    assert choose_log_table([audit, err], None).table == "ERROR_LOG"
    assert choose_log_table([audit, err], "AUDIT_TRAIL").table == "AUDIT_TRAIL"
    assert choose_log_table([audit, err], "NOPE") is None
    assert choose_log_table([], None) is None


def test_prompt_contains_entries_and_parse_clusters():
    entries = [{"SEVERITY": "ERROR", "MESSAGE": "gateway timeout"}]
    prompt = build_root_cause_prompt("ERROR_LOG", entries)
    assert "ERROR_LOG" in prompt and "gateway timeout" in prompt

    rc = parse_root_cause_response(
        "ERROR_LOG",
        '{"clusters":[{"cause":"Payment gateway timeout","count":3,"severity":"ERROR",'
        '"suggested_action":"Add retry/backoff","example":"gateway timeout"}]}',
        sample_size=5,
    )
    assert rc.sample_size == 5 and len(rc.clusters) == 1
    c = rc.clusters[0]
    assert c.cause == "Payment gateway timeout" and c.count == 3
    assert c.suggested_action == "Add retry/backoff"


def test_parse_handles_garbage():
    rc = parse_root_cause_response("ERROR_LOG", "sorry no json", sample_size=2)
    assert rc.clusters == [] and rc.note
