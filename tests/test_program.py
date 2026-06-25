"""Program-unit capture + LLM explanation: prompt, parsing, the pass, and ask integration."""

from blossa.llm.base import LLMProvider
from blossa.models import (
    ConfidenceLevel,
    ProgramKind,
    ProgramSemantics,
    ProgramUnit,
    ScanMetadata,
    ScanReport,
    SchemaInfo,
)
from blossa.nlquery import build_ask_prompt, build_schema_context
from blossa.program import (
    build_program_prompt,
    parse_program_response,
    run_program_pass,
    trim_source,
)


class _FakeProvider(LLMProvider):
    """Returns a canned response; records the prompts it was given."""

    name = "fake"

    def __init__(self, response: str, *, fail: bool = False):
        self._response = response
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    def analyze(self, summary):  # pragma: no cover - not used here
        raise NotImplementedError

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self._fail:
            raise RuntimeError("model down")
        return self._response


_PROC = ProgramUnit(
    name="GIVE_RAISE",
    owner="HR",
    kind=ProgramKind.PROCEDURE,
    source="PROCEDURE give_raise(p_id NUMBER) IS BEGIN UPDATE employees SET salary = salary*1.1 "
    "WHERE employee_id = p_id; END;",
)

_GOOD_JSON = (
    '{"summary": "Raises an employee\'s salary by 10%.", '
    '"tables_used": ["EMPLOYEES"], "confidence": "high", '
    '"evidence": ["UPDATE employees SET salary"]}'
)


# --------------------------------------------------------------- source trimming


def test_trim_source_passes_short_source_through():
    assert trim_source("SELECT 1") == "SELECT 1"


def test_trim_source_truncates_and_flags_long_source():
    trimmed = trim_source("X" * 20000)
    assert len(trimmed) < 20000
    assert "truncated" in trimmed


# --------------------------------------------------------------- prompt building


def test_program_prompt_includes_source_kind_and_known_tables():
    prompt = build_program_prompt(_PROC, known_tables=["EMPLOYEES", "DEPARTMENTS"])
    assert "GIVE_RAISE" in prompt
    assert "PROCEDURE" in prompt
    assert "UPDATE employees" in prompt  # the actual source is shown
    assert "EMPLOYEES, DEPARTMENTS" in prompt  # known-table hint
    assert '"summary"' in prompt  # output contract


# --------------------------------------------------------------- response parsing


def test_parse_program_response_reads_all_fields():
    sem = parse_program_response(_PROC, _GOOD_JSON)
    assert sem.name == "GIVE_RAISE"
    assert sem.kind == ProgramKind.PROCEDURE
    assert "10%" in sem.summary
    assert sem.tables_used == ["EMPLOYEES"]
    assert sem.confidence == ConfidenceLevel.HIGH


def test_parse_program_response_falls_back_on_garbage():
    sem = parse_program_response(_PROC, "not json at all")
    assert sem.confidence == ConfidenceLevel.LOW
    assert "GIVE_RAISE".lower() in sem.summary.lower() or "procedure" in sem.summary.lower()


# --------------------------------------------------------------- the pass


def test_run_program_pass_explains_each_unit():
    prov = _FakeProvider(_GOOD_JSON)
    results = run_program_pass(prov, [_PROC], known_tables=["EMPLOYEES"])
    assert len(results) == 1 and results[0].tables_used == ["EMPLOYEES"]
    assert len(prov.calls) == 1


def test_run_program_pass_degrades_one_failure_to_low_confidence():
    prov = _FakeProvider("", fail=True)
    results = run_program_pass(prov, [_PROC])
    assert len(results) == 1
    assert results[0].confidence == ConfidenceLevel.LOW
    assert "model down" in results[0].evidence[0]


# --------------------------------------------------------------- ask integration


def _report_with_program() -> ScanReport:
    meta = ScanMetadata(
        blossa_version="0",
        schema_name="HR",
        generated_at="2026-06-25T00:00:00Z",
        llm_provider="fake",
    )
    return ScanReport(
        metadata=meta,
        schema_info=SchemaInfo(name="HR"),
        program_semantics=[
            ProgramSemantics(
                name="GIVE_RAISE",
                owner="HR",
                kind=ProgramKind.PROCEDURE,
                summary="Raises an employee's salary by 10%.",
                tables_used=["EMPLOYEES"],
                confidence=ConfidenceLevel.HIGH,
            )
        ],
    )


def test_schema_context_carries_program_summaries():
    ctx = build_schema_context(_report_with_program())
    assert ctx["programs"]
    prog = ctx["programs"][0]
    assert prog["kind"] == "PROCEDURE" and "10%" in prog["does"]


def test_ask_prompt_instructs_plain_language_logic_answers():
    prompt = build_ask_prompt("what does GIVE_RAISE do?", _report_with_program())
    assert "GIVE_RAISE" in prompt  # the summary is in the context
    # The system prompt (separate constant) tells the model to answer logic questions with sql="".
    from blossa.nlquery import ASK_SYSTEM_PROMPT

    assert "plain language" in ASK_SYSTEM_PROMPT.lower()
    assert "programs" in ASK_SYSTEM_PROMPT.lower()
