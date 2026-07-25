# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Write Oracle code (PL/SQL and DDL) against the scanned schema.

`ask` answers questions about the database; this answers "build me something for it" — a new
procedure modelled on an existing one, a view over the right join, an index for a query. The map
is what makes the output usable rather than generic: it carries the real table and column names,
the relationships, and the SOURCE of the stored programs, so generated code can follow the
conventions already in the schema and compile against it.

Two boundaries hold here:

- **Blossa never executes what it writes.** The output is text, shown with an explanation for a
  human to review and run in their own tool. The connection stays READ ONLY at the Oracle
  transaction level, so nothing here can change the database even by mistake.
- **No raw rows reach the model.** The prompt carries the semantic map and program source — DDL
  and metadata — exactly like the scan's own passes. Query results are never involved.

A deterministic pass flags destructive statements in the result, because "review this before you
run it" is only useful advice if we point at the part that deserves the attention.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from .llm.base import language_instruction
from .models import ConfidenceLevel, ProgramKind, ScanReport
from .program import trim_source

# What the user is asking Blossa to build. Both halves must be present — an ACTION and a DATABASE
# OBJECT — so "what does the get_balance procedure do?" (an object, no action) stays a question.
_BUILD_VERBS = (
    "create", "write", "generate", "build", "implement", "add a", "add an", "give me the code",
    "code for", "ddl for", "script for", "refactor", "rewrite",
    "creeaz", "creez", "creaz", "scrie", "genereaz", "fa-mi", "fă-mi", "fami", "construie",
    "implementeaz", "adauga", "adaugă", "vreau o", "vreau un", "da-mi codul", "dă-mi codul",
    "codul pentru", "ddl-ul pentru", "modifica", "modifică", "rescrie",
)
_OBJECT_NOUNS = (
    "procedure", "function", "package", "trigger", "view", "index", "table", "sequence",
    "synonym", "constraint", "column", "materialized view", "ddl", "pl/sql", "plsql", "script",
    "procedur", "functie", "funcție", "funct", "pachet", "declansator", "declanșator", "vedere",
    "tabel", "tabela", "tabelă", "secventa", "secvență", "constrangere", "constrângere",
    "coloana", "coloană", "cod ", "codul",
)


def is_code_request(question: str) -> bool:
    """True when the user is asking for code to be written rather than for an answer."""
    q = (question or "").lower()
    return any(v in q for v in _BUILD_VERBS) and any(n in q for n in _OBJECT_NOUNS)


class CodeProposal(BaseModel):
    """Generated code, plus everything a reviewer needs before running it."""

    code: str = ""
    object_type: str = ""  # PROCEDURE / VIEW / INDEX / ...
    object_name: str = ""
    explanation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    # Deterministic review notes (destructive statements, etc.) — not the model's opinion.
    warnings: list[str] = Field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.code.strip())


CODEGEN_SYSTEM_PROMPT = (
    "You are a senior Oracle developer writing code for an existing application schema. You are "
    "given a semantic map of that schema (tables, columns with their business meaning, "
    "relationships, stored programs) and, when relevant, the SOURCE of the programs the request "
    "refers to. Write the code the user asked for.\n\n"
    "Rules:\n"
    "- Use ONLY tables, columns and programs that appear in the map, with their EXACT names. "
    "Never invent an object. If something needed is missing, say so in \"assumptions\" and write "
    "the closest correct version.\n"
    "- Follow the conventions visible in the provided source (naming, error handling, logging, "
    "parameter style) so the result fits the codebase it joins.\n"
    "- Prefer CREATE OR REPLACE for programs and views. NEVER write DROP, TRUNCATE, or a DELETE/"
    "UPDATE without a WHERE clause. Do not modify existing objects the user did not mention.\n"
    "- The code will NOT be run by this tool: a human reviews it first. So it must be complete "
    "and runnable as-is, and anything you assumed must be listed in \"assumptions\".\n"
    "- Give a confidence: 'high', 'medium' or 'low'.\n"
    "- Respond with STRICT JSON only — no prose, no markdown fences."
)

_CODE_OUTPUT_CONTRACT = (
    'Respond with JSON of exactly this shape:\n'
    '{\n'
    '  "code": "<the complete Oracle code, ready to run>",\n'
    '  "object_type": "<PROCEDURE|FUNCTION|PACKAGE|VIEW|TRIGGER|INDEX|TABLE|...>",\n'
    '  "object_name": "<the object this creates>",\n'
    '  "explanation": "<what it does and how it works>",\n'
    '  "assumptions": ["<anything you had to assume>", ...],\n'
    '  "confidence": "high|medium|low"\n'
    '}'
)


def reference_sources(question: str, report: ScanReport, extra_text: str = "") -> list[str]:
    """The source of every program the request names — the grounding that makes output fit in.

    "a procedure like get_balance" is only answerable if the model can see get_balance. Packaged
    routines resolve to their package, since that is where the source lives.
    """
    from .program import declared_subprograms  # local: keeps the module import graph flat

    tokens = {t.upper() for t in re.findall(r"[A-Za-z][A-Za-z0-9_$#]*", f"{question} {extra_text}")}
    blocks: list[str] = []
    for unit in report.schema_info.program_units:
        named = unit.name.upper() in tokens
        if not named and unit.kind == ProgramKind.PACKAGE:
            named = any(routine in tokens for routine, _ in declared_subprograms(unit.source))
        if named and unit.source.strip():
            owner = f"{unit.owner}." if unit.owner else ""
            blocks.append(
                f"-- {unit.kind.value} {owner}{unit.name}\n{trim_source(unit.source)}"
            )
    return blocks


def build_codegen_prompt(
    question: str,
    report: ScanReport,
    *,
    language: str | None = None,
    history_text: str = "",
) -> str:
    """Assemble the code-writing prompt: the map, the referenced source, and the request."""
    from .nlquery import build_schema_context  # local: nlquery imports nothing from here

    context = json.dumps(build_schema_context(report), indent=2, default=str)
    refs = reference_sources(question, report, history_text)
    ref_block = (
        "Source of the existing programs this request refers to (follow their conventions):\n"
        + "\n\n".join(refs)
        + "\n\n"
        if refs
        else ""
    )
    return (
        f"Database map (semantic, PII-safe JSON):\n{context}\n\n"
        f"{ref_block}"
        f"What to build:\n{question.strip()}\n\n"
        f"{language_instruction(language)}"
        f"{_CODE_OUTPUT_CONTRACT}"
    )


# Statements that change or destroy something that already exists. Generated code is never run by
# Blossa, but a reviewer should have these pointed out rather than have to spot them.
_DESTRUCTIVE = (
    (r"\bDROP\s+(TABLE|VIEW|INDEX|SEQUENCE|PACKAGE|PROCEDURE|FUNCTION|TRIGGER|USER)\b",
     "drops an existing object"),
    (r"\bTRUNCATE\s+TABLE\b", "truncates a table"),
    (r"\bALTER\s+TABLE\b.*\bDROP\b", "drops a column or constraint"),
    (r"\bGRANT\b|\bREVOKE\b", "changes privileges"),
)


def review_generated_code(code: str) -> list[str]:
    """Deterministic review notes: statements a human should look at twice before running."""
    notes: list[str] = []
    stripped = re.sub(r"--[^\n]*", " ", code or "")
    for pattern, description in _DESTRUCTIVE:
        if re.search(pattern, stripped, re.IGNORECASE | re.DOTALL):
            notes.append(f"This code {description} — read it before running.")
    if re.search(r"\b(DELETE\s+FROM|UPDATE)\b", stripped, re.IGNORECASE) and not re.search(
        r"\bWHERE\b", stripped, re.IGNORECASE
    ):
        notes.append("There is a DELETE/UPDATE with no WHERE clause — it would hit every row.")
    # Asked to extend a package, a model naturally writes the routine the way it appears inside
    # one — which is not a statement you can run. Saying so beats letting the user find out.
    body_only = re.match(r"\s*(PROCEDURE|FUNCTION)\b", stripped, re.IGNORECASE) and not re.search(
        r"^\s*CREATE\b", stripped, re.IGNORECASE | re.MULTILINE
    )
    if body_only:
        notes.append(
            "This is a routine body as it would appear INSIDE a package — add it to the package "
            "spec and body (CREATE OR REPLACE PACKAGE …); on its own it will not compile."
        )
    return notes


_QUALIFIED_REF = re.compile(r"\b([A-Za-z][\w$#]*)\s*\.\s*([A-Za-z][\w$#]*)\b")


def unknown_column_references(code: str, report: ScanReport) -> list[str]:
    """Qualified references like `a.ACTIVE` whose name is nowhere in the map.

    Models invent a plausible column when the real one is shaped differently — asked about
    "active accounts" they write `ACTIVE = 'Y'` while the schema keeps `STATUS = 'ACTIVE'`. The
    map knows every real column, so this is checkable rather than a matter of trust. Only
    QUALIFIED references are checked, since a bare word could be a variable or a parameter; calls
    into a known package are skipped, because `core_banking.get_balance` has the same shape.
    """
    from .program import declared_subprograms

    columns = {c.name.upper() for t in report.schema_info.tables for c in t.columns}
    tables = {t.name.upper() for t in report.schema_info.tables}
    owners = {(t.owner or "").upper() for t in report.schema_info.tables}
    programs: set[str] = set()
    for unit in report.schema_info.program_units:
        programs.add(unit.name.upper())
        if unit.kind == ProgramKind.PACKAGE:
            programs.update(name for name, _ in declared_subprograms(unit.source))

    known = columns | tables | owners | programs
    stripped = re.sub(r"--[^\n]*", " ", code or "")
    unknown: list[str] = []
    for qualifier, name in _QUALIFIED_REF.findall(stripped):
        if qualifier.upper() in programs:  # a package call, not a column reference
            continue
        if name.upper() in known or name.upper() in unknown:
            continue
        unknown.append(name.upper())
    return unknown


def parse_codegen_response(raw: str, report: ScanReport | None = None) -> CodeProposal:
    """Parse the model's JSON into a CodeProposal, defensively, and attach the review notes."""
    from .nlquery import _coerce_confidence, _loads_lenient  # shared lenient JSON handling

    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        return CodeProposal(explanation="The model did not return parseable JSON.")
    code = str(data.get("code") or "").strip()
    warnings = review_generated_code(code)
    if report is not None and (invented := unknown_column_references(code, report)):
        warnings.append(
            "These names are not in the map, so they probably do not exist: "
            f"{', '.join(invented)}. Check them against the real table before running this."
        )
    return CodeProposal(
        code=code,
        object_type=str(data.get("object_type") or "").strip().upper(),
        object_name=str(data.get("object_name") or "").strip(),
        explanation=str(data.get("explanation") or "").strip(),
        assumptions=[str(a).strip() for a in (data.get("assumptions") or []) if str(a).strip()],
        confidence=_coerce_confidence(data.get("confidence")),
        warnings=warnings,
    )
