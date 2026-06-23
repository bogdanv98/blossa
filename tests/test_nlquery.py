"""Pure NL→SQL logic for `blossa ask`: context, parsing, the read-only guard, the row cap."""

import pytest

from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.nlquery import (
    UnsafeQueryError,
    build_ask_prompt,
    build_schema_context,
    parse_ask_response,
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
