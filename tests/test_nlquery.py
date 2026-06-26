"""Pure NL→SQL logic for `blossa ask`: context, parsing, the read-only guard, the row cap."""

import pytest

from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.models import ConfidenceLevel, LogColumn, LogKind, LogRole, LogTable
from blossa.nlquery import (
    _MAX_HISTORY,
    Turn,
    UnsafeQueryError,
    build_ask_prompt,
    build_schema_context,
    parse_ask_response,
    privilege_hint,
    validate_read_only_select,
    with_row_limit,
)
from blossa.pipeline import run_scan_over_schema


def _demo_report():
    settings = Settings()
    settings.llm.provider = "heuristic"
    return run_scan_over_schema(
        build_demo_schema(), settings, HeuristicProvider(), db=None, owner=None
    )


# --------------------------------------------------------- read-only guard


def test_validate_accepts_select_and_with():
    assert validate_read_only_select("SELECT * FROM customers") == "SELECT * FROM customers"
    cte = validate_read_only_select(" with x as (select 1 from dual) select * from x ")
    assert cte.lower().startswith("with")


def test_validate_strips_trailing_semicolon():
    assert validate_read_only_select("SELECT 1 FROM dual;") == "SELECT 1 FROM dual"


@pytest.mark.parametrize(
    "bad",
    [
        "UPDATE customers SET x = 1",
        "DELETE FROM customers",
        "DROP TABLE customers",
        "INSERT INTO customers VALUES (1)",
        "MERGE INTO t USING s ON (1=1)",
        "TRUNCATE TABLE t",
        "BEGIN NULL; END;",
        "SELECT 1 FROM dual; DROP TABLE t",  # second statement
        "GRANT SELECT ON t TO u",
    ],
)
def test_validate_rejects_non_readonly(bad):
    with pytest.raises(UnsafeQueryError):
        validate_read_only_select(bad)


def test_validate_rejects_empty():
    with pytest.raises(UnsafeQueryError):
        validate_read_only_select("   ")


# --------------------------------------------------------- row cap


def test_with_row_limit_wraps_and_caps():
    out = with_row_limit("SELECT * FROM t", 50)
    assert "SELECT * FROM (" in out and "ROWNUM <= 50" in out


def test_with_row_limit_floors_to_one():
    assert "ROWNUM <= 1" in with_row_limit("SELECT 1 FROM dual", 0)


# --------------------------------------------------------- response parsing


def test_parse_plain_json():
    r = parse_ask_response(
        '{"sql":"SELECT 1 FROM dual","explanation":"one","assumptions":["a"],"confidence":"high"}'
    )
    assert r.answerable and r.sql == "SELECT 1 FROM dual"
    assert r.confidence.value == "high" and r.assumptions == ["a"]


def test_parse_strips_markdown_fence():
    r = parse_ask_response('```json\n{"sql":"SELECT 2 FROM dual","confidence":"medium"}\n```')
    assert r.sql == "SELECT 2 FROM dual"


def test_parse_unanswerable_when_sql_empty():
    r = parse_ask_response('{"sql":"","explanation":"not in this schema"}')
    assert not r.answerable and "not in this schema" in r.explanation


def test_parse_garbage_is_unanswerable():
    assert not parse_ask_response("sorry, I can't help with that").answerable


# --------------------------------------------------------- schema context / prompt


def test_context_has_tables_columns_meanings_and_relationships():
    ctx = build_schema_context(_demo_report())
    names = {t["name"] for t in ctx["tables"]}
    assert "CUSTOMERS" in names
    customers = next(t for t in ctx["tables"] if t["name"] == "CUSTOMERS")
    email = next(c for c in customers["columns"] if c["name"] == "EMAIL")
    assert email["means"]  # the heuristic gave EMAIL a meaning, carried into the context
    assert ctx["relationships"]  # the demo schema has foreign keys


def test_prompt_includes_question_and_output_contract():
    prompt = build_ask_prompt("how many customers are there?", _demo_report())
    assert "how many customers are there?" in prompt
    assert '"sql"' in prompt and '"confidence"' in prompt


def test_prompt_catalog_views_follow_scope():
    report = _demo_report()
    scoped = build_ask_prompt("how many schemas?", report, use_dba=False)
    full = build_ask_prompt("how many schemas?", report, use_dba=True)
    # Scoped exposes the ALL_* views (Oracle limits them to granted objects); not the DBA_* ones.
    assert "ALL_TABLES" in scoped and "DBA_USERS" not in scoped
    # Full exposes the whole-database DBA_* views.
    assert "DBA_USERS" in full and "DBA_TABLES" in full


def test_prompt_distinguishes_counting_tables_from_schemas():
    # Regression: "how many tables" once produced COUNT(DISTINCT OWNER) (i.e. schemas). The catalog
    # reference must spell out the table-count pattern and warn off DISTINCT OWNER, in both scopes.
    report = _demo_report()
    scoped = build_ask_prompt("how many tables?", report, use_dba=False)
    full = build_ask_prompt("how many tables?", report, use_dba=True)
    assert "COUNT(*) FROM ALL_TABLES" in scoped
    assert "COUNT(*) FROM DBA_TABLES" in full
    for prompt in (scoped, full):
        assert "DISTINCT OWNER" in prompt  # the schema-count pattern is still offered
        assert "count schemas, not tables" in prompt  # and explicitly contrasted


def test_full_catalog_excludes_operational_accounts():
    # Regression: ORACLE_MAINTAINED='N' alone leaks Oracle operational accounts (PDBADMIN, OS-auth
    # OPS$ logins) into the "how many schemas" answer. They must be filtered out in full mode.
    full = build_ask_prompt("how many schemas?", _demo_report(), use_dba=True)
    assert "OPS$%" in full and "PDBADMIN" in full


def test_prompt_counts_views_with_a_dedicated_view():
    # Regression: "how many views" once counted ALL objects via DBA_OBJECTS (no OBJECT_TYPE filter).
    # The reference must offer the dedicated views catalog and warn that *_OBJECTS spans every kind.
    scoped = build_ask_prompt("how many views?", _demo_report(), use_dba=False)
    full = build_ask_prompt("how many views?", _demo_report(), use_dba=True)
    assert "ALL_VIEWS" in scoped and "DBA_VIEWS" in full
    for prompt in (scoped, full):
        assert "OBJECT_TYPE" in prompt  # filtering by kind is spelled out


def test_prompt_covers_other_object_kinds_and_a_safety_net():
    # Regression: "how many chains" returned "unclear". The reference should point at the dedicated
    # dictionary views (e.g. scheduler chains) and tell the model to ask rather than guess.
    scoped = build_ask_prompt("how many chains?", _demo_report(), use_dba=False)
    full = build_ask_prompt("how many chains?", _demo_report(), use_dba=True)
    assert "ALL_SCHEDULER_CHAINS" in scoped and "DBA_SCHEDULER_CHAINS" in full
    for prompt in (scoped, full):
        assert "ask the user to clarify rather than guessing" in prompt


# --------------------------------------------------------- log-aware ask


def _report_with_log_table():
    report = _demo_report()
    report.log_tables.append(
        LogTable(
            table="ERROR_LOG",
            kind=LogKind.ERROR,
            confidence=ConfidenceLevel.HIGH,
            columns=[
                LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
                LogColumn(column="SEVERITY", role=LogRole.SEVERITY),
                LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
                LogColumn(column="ORDER_ID", role=LogRole.BUSINESS_REF),
            ],
        )
    )
    return report


def test_context_exposes_log_tables_with_roles():
    ctx = build_schema_context(_report_with_log_table())
    assert ctx["log_tables"], "log tables should be in the model-facing context"
    lt = ctx["log_tables"][0]
    assert lt["name"] == "ERROR_LOG" and lt["kind"] == "error"
    roles = {c["name"]: c["role"] for c in lt["columns"]}
    assert roles["LOG_TIME"] == "event_time" and roles["MESSAGE"] == "message"


def test_prompt_carries_log_tables_and_guidance():
    prompt = build_ask_prompt("what are the most common errors?", _report_with_log_table())
    assert "ERROR_LOG" in prompt and "event_time" in prompt
    # The system prompt steers error/log questions toward the log tables.
    from blossa.nlquery import ASK_SYSTEM_PROMPT

    assert "log_tables" in ASK_SYSTEM_PROMPT
    assert "ERRORS" in ASK_SYSTEM_PROMPT and "event_time" in ASK_SYSTEM_PROMPT
    # "errors" must filter severity, not just date — INFO/WARN excluded unless asked for.
    assert "'FATAL'" in ASK_SYSTEM_PROMPT and "INFO" in ASK_SYSTEM_PROMPT
    assert "date/time filter alone is wrong" in ASK_SYSTEM_PROMPT.replace("\n", " ")


# --------------------------------------------------- error-severity safety net


def _ask(sql):
    from blossa.nlquery import AskResult

    return AskResult(sql=sql, confidence=ConfidenceLevel.HIGH)


def test_error_filter_injected_when_missing_no_where():
    from blossa.nlquery import enforce_error_severity_filter

    report = _report_with_log_table()  # ERROR_LOG has a SEVERITY column
    out = enforce_error_severity_filter(
        "what errors happened today?",
        _ask("SELECT * FROM ERROR_LOG WHERE TRUNC(LOG_TIME) = TRUNC(SYSDATE)"),
        report,
    )
    assert "UPPER(SEVERITY) IN" in out.sql and "'FATAL'" in out.sql
    assert "AND UPPER(SEVERITY)" in out.sql  # appended to the existing WHERE
    assert any("error severities" in a for a in out.assumptions)


def test_error_filter_added_as_where_when_none_and_before_order_by():
    from blossa.nlquery import enforce_error_severity_filter

    out = enforce_error_severity_filter(
        "show me errors", _ask("SELECT MESSAGE FROM ERROR_LOG ORDER BY LOG_TIME DESC"),
        _report_with_log_table(),
    )
    # WHERE inserted before ORDER BY, not after it.
    assert out.sql.index("WHERE") < out.sql.index("ORDER BY")
    assert "UPPER(SEVERITY) IN" in out.sql


def test_error_filter_not_applied_when_already_filtered():
    from blossa.nlquery import enforce_error_severity_filter

    sql = "SELECT * FROM ERROR_LOG WHERE SEVERITY = 'FATAL'"
    out = enforce_error_severity_filter("errors today", _ask(sql), _report_with_log_table())
    assert out.sql == sql  # untouched
    assert out.assumptions == []


def test_error_filter_skipped_for_non_error_question():
    from blossa.nlquery import enforce_error_severity_filter

    sql = "SELECT * FROM ERROR_LOG WHERE TRUNC(LOG_TIME) = TRUNC(SYSDATE)"
    out = enforce_error_severity_filter("show all log entries today", _ask(sql),
                                        _report_with_log_table())
    assert out.sql == sql  # "all ... entries" override → no filter


def test_error_filter_warns_on_complex_sql_without_rewriting():
    from blossa.nlquery import enforce_error_severity_filter

    # A GROUP BY with no severity filter is still rewritten (simple single SELECT)...
    simple = enforce_error_severity_filter(
        "which module had the most errors?",
        _ask("SELECT MODULE, COUNT(*) FROM ERROR_LOG GROUP BY MODULE"),
        _report_with_log_table(),
    )
    assert "WHERE UPPER(SEVERITY) IN" in simple.sql
    assert simple.sql.index("WHERE") < simple.sql.index("GROUP BY")

    # ...but a UNION is too complex to touch: warn instead of rewrite.
    union_sql = "SELECT MESSAGE FROM ERROR_LOG UNION SELECT MESSAGE FROM ERROR_LOG"
    complex_out = enforce_error_severity_filter("errors", _ask(union_sql), _report_with_log_table())
    assert complex_out.sql == union_sql
    assert any("INFO/WARN" in a for a in complex_out.assumptions)


# --------------------------------------------------------- multi-turn refine


def test_prompt_without_history_has_no_conversation_block():
    prompt = build_ask_prompt("how many customers?", _demo_report())
    assert "Conversation so far" not in prompt


def test_prompt_includes_prior_questions_and_sql():
    history = [Turn(question="how many customers?", sql="SELECT COUNT(*) FROM CUSTOMERS")]
    prompt = build_ask_prompt("now break it down by country", _demo_report(), history=history)
    assert "Conversation so far" in prompt
    assert "how many customers?" in prompt
    assert "SELECT COUNT(*) FROM CUSTOMERS" in prompt  # the model can build on its last query
    assert "now break it down by country" in prompt  # the new (follow-up) question


def test_plain_language_turn_is_marked_without_sql():
    history = [Turn(question="what does the order trigger do?", sql="")]
    prompt = build_ask_prompt("and the customer one?", _demo_report(), history=history)
    assert "answered in plain language" in prompt


def test_history_is_capped_to_recent_turns():
    history = [Turn(question=f"q{i}", sql=f"SELECT {i} FROM dual") for i in range(_MAX_HISTORY + 5)]
    prompt = build_ask_prompt("latest", _demo_report(), history=history)
    # The oldest turns are dropped; only the most recent _MAX_HISTORY survive.
    assert "q0" not in prompt and "SELECT 0 FROM dual" not in prompt
    assert f"q{_MAX_HISTORY + 4}" in prompt


def test_system_prompt_describes_followups():
    from blossa.nlquery import ASK_SYSTEM_PROMPT

    assert "FOLLOW-UP" in ASK_SYSTEM_PROMPT
    assert "most recent SQL" in ASK_SYSTEM_PROMPT


# --------------------------------------------------------- catalog privilege hint


_DENIED = "ORA-00942: table or view does not exist"


def test_privilege_hint_fires_on_denied_dba_query():
    hint = privilege_hint("SELECT COUNT(*) FROM DBA_USERS", _DENIED)
    assert hint and "SELECT_CATALOG_ROLE" in hint


def test_privilege_hint_silent_for_all_views_and_other_errors():
    assert privilege_hint("SELECT * FROM ALL_TABLES", _DENIED) is None
    assert privilege_hint("SELECT * FROM DBA_USERS", "ORA-12170: connection timeout") is None
