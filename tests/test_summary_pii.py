"""The summary handed to the LLM must never contain raw row values — only PII-safe artifacts."""

import json

from blossa.checks import run_checks
from blossa.demo import build_demo_schema
from blossa.llm.base import build_user_prompt
from blossa.summary import build_summaries

# Raw values present in the demo data that must NOT leak into a summary/prompt.
_RAW_LEAKS = [
    "Acme Trading SRL",
    "carmen.i@mail.example",
    "orders@acme.example",
    "+40 21 555 0101",
    "Net-30 terms agreed.",
    "SKU-ABX-001",
]


def test_summaries_contain_no_raw_values():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    summaries = build_summaries(schema, relationships)
    blob = json.dumps([s.model_dump() for s in summaries], default=str)
    for leak in _RAW_LEAKS:
        assert leak not in blob, f"Raw value leaked into summary: {leak!r}"


def test_prompt_contains_no_raw_values():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    summaries = build_summaries(schema, relationships)
    for summary in summaries:
        prompt = build_user_prompt(summary)
        for leak in _RAW_LEAKS:
            assert leak not in prompt, f"Raw value leaked into LLM prompt: {leak!r}"


def test_summary_keeps_key_roles_and_references():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    summaries = {s.name: s for s in build_summaries(schema, relationships)}
    orders = summaries["ORDERS"]
    cust_col = next(c for c in orders.columns if c.name == "CUST_ID")
    assert cust_col.key_role.value == "foreign_key"
    assert cust_col.references == "CUSTOMERS.CUST_ID"
