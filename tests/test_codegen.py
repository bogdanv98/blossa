# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Code generation: intent detection, grounding, the destructive-statement review, parsing.

The safety property under test throughout: Blossa writes code, it never runs it.
"""

import json
from datetime import datetime

import pytest

from blossa.codegen import (
    build_codegen_prompt,
    is_code_request,
    parse_codegen_response,
    reference_sources,
    review_generated_code,
    unknown_column_references,
)
from blossa.demo import build_demo_schema
from blossa.models import (
    ConfidenceLevel,
    ProgramKind,
    ProgramUnit,
    ScanMetadata,
    ScanReport,
)

_PACKAGE_SOURCE = """PACKAGE core_banking AS
   FUNCTION get_balance(p_account_id IN NUMBER) RETURN NUMBER;
END core_banking;PACKAGE BODY core_banking AS
   FUNCTION get_balance(p_account_id IN NUMBER) RETURN NUMBER IS
   BEGIN
      RETURN 0;  -- house style: every routine logs through log_error
   END get_balance;
END core_banking;"""


def _report() -> ScanReport:
    schema = build_demo_schema()
    schema.program_units.append(
        ProgramUnit(name="CORE_BANKING", owner="BANKDEMO", kind=ProgramKind.PACKAGE,
                    source=_PACKAGE_SOURCE)
    )
    return ScanReport(
        metadata=ScanMetadata(
            blossa_version="test",
            schema_name=schema.name,
            generated_at=datetime(2026, 7, 25, 12, 0),
            llm_provider="ollama",
        ),
        schema_info=schema,
    )


# ------------------------------------------------------------- intent detection


@pytest.mark.parametrize(
    "question",
    [
        "write me a procedure like get_balance but only for active accounts",
        "crezi ca ai putea sa creezi o procedura ca cea get_balance doar ca sa aduca balanta",
        "generate a view joining orders and customers",
        "scrie-mi un trigger care logheaza modificarile",
        "give me the code for an index on ORDERS.CUST_ID",
    ],
)
def test_recognises_a_request_to_build_something(question):
    assert is_code_request(question)


@pytest.mark.parametrize(
    "question",
    [
        "what does the get_balance procedure do?",  # an object, but no action
        "exista vreo procedura get_balance in db?",
        "how many orders per customer?",
        "care sunt ultimele 5 erori?",
    ],
)
def test_questions_are_not_code_requests(question):
    assert not is_code_request(question)


# ------------------------------------------------------------------- grounding


def test_prompt_carries_the_source_of_the_referenced_program():
    # "a procedure like get_balance" is only answerable if the model can read get_balance —
    # and a packaged routine resolves to the package, which is where the source lives.
    prompt = build_codegen_prompt("write a procedure like get_balance", _report())
    assert "house style: every routine logs through log_error" in prompt
    assert "PACKAGE BANKDEMO.CORE_BANKING" in prompt


def test_prompt_includes_the_map_and_the_request():
    prompt = build_codegen_prompt("write a view over CUSTOMERS", _report())
    assert "CUSTOMERS" in prompt
    assert "What to build:" in prompt
    assert '"code"' in prompt  # the output contract


def test_prompt_can_be_asked_for_romanian():
    assert "Romanian" in build_codegen_prompt("scrie o procedura", _report(), language="ro")


def test_reference_sources_follows_a_name_from_the_conversation():
    # "one like that one" — the routine was named in an earlier turn, not in this question.
    refs = reference_sources("acum fa una doar pentru conturi active", _report(),
                             "ce face get_balance?")
    assert refs and "core_banking" in refs[0].lower()


def test_reference_sources_is_empty_when_nothing_is_named():
    assert reference_sources("write a view over CUSTOMERS", _report()) == []


# --------------------------------------------------------- destructive review


def test_review_flags_destructive_statements():
    assert review_generated_code("DROP TABLE ORDERS;")
    assert review_generated_code("TRUNCATE TABLE ORDERS;")
    assert review_generated_code("DELETE FROM ORDERS;")
    assert review_generated_code("GRANT SELECT ON ORDERS TO PUBLIC;")


def test_review_passes_ordinary_code():
    code = (
        "CREATE OR REPLACE PROCEDURE active_balance(p_id IN NUMBER) IS\n"
        "BEGIN\n  DELETE FROM TMP_CALC WHERE RUN_ID = p_id;\nEND;"
    )
    assert review_generated_code(code) == []  # the DELETE is scoped by a WHERE


def test_review_flags_a_bare_package_routine_body():
    # Asked to extend a package, a model writes the routine as it appears inside one — which is
    # not runnable on its own. The user should learn that from us, not from ORA-00900.
    notes = review_generated_code(
        "FUNCTION get_active_balance(p_id IN NUMBER) RETURN NUMBER IS\nBEGIN\n"
        "  RETURN 0;\nEND get_active_balance;"
    )
    assert any("INSIDE a package" in n for n in notes)


def test_review_does_not_flag_a_standalone_create():
    code = "CREATE OR REPLACE FUNCTION f RETURN NUMBER IS BEGIN RETURN 0; END;"
    assert review_generated_code(code) == []


def test_review_ignores_a_comment_mentioning_drop():
    code = "-- does not DROP TABLE anything\nCREATE VIEW V AS SELECT 1 X FROM DUAL"
    assert review_generated_code(code) == []


# ---------------------------------------------------------------- parsing


def test_parse_reads_the_proposal_and_attaches_warnings():
    raw = (
        '{"code": "DROP TABLE ORDERS;", "object_type": "table", "object_name": "ORDERS",'
        ' "explanation": "removes it", "assumptions": ["you meant this"], "confidence": "high"}'
    )
    proposal = parse_codegen_response(raw)
    assert proposal.code == "DROP TABLE ORDERS;"
    assert proposal.object_type == "TABLE"  # normalised
    assert proposal.confidence == ConfidenceLevel.HIGH
    assert proposal.assumptions == ["you meant this"]
    assert proposal.warnings, "a destructive statement must be flagged for the reviewer"


def test_invented_columns_are_flagged_against_the_map():
    # The real failure seen live: asked for "active accounts", the model wrote `a.ACTIVE = 'Y'`
    # while the schema keeps the state in a different column. The map can settle that.
    report = _report()
    table = report.schema_info.tables[0]
    real_column = table.columns[0].name
    code = (
        f"CREATE VIEW V AS SELECT t.{real_column} FROM {table.name} t WHERE t.ACTIVE = 'Y'"
    )
    assert unknown_column_references(code, report) == ["ACTIVE"]
    proposal = parse_codegen_response(json.dumps({"code": code, "confidence": "high"}), report)
    assert any("ACTIVE" in w for w in proposal.warnings)


def test_a_package_call_is_not_mistaken_for_a_column():
    # core_banking.get_balance has the same alias.name shape as a column reference.
    code = "BEGIN v := core_banking.get_balance(1); END;"
    assert unknown_column_references(code, _report()) == []


def test_no_map_means_no_identifier_check():
    code = "CREATE VIEW V AS SELECT t.NOPE FROM X t"
    assert parse_codegen_response(json.dumps({"code": code})).warnings == []


def test_parse_survives_garbage():
    proposal = parse_codegen_response("sorry, I can't")
    assert not proposal.answerable
    assert proposal.confidence == ConfidenceLevel.LOW
