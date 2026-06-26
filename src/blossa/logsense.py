# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Analyse the *contents* of application log tables: breakdowns, and LLM root-cause clustering.

Two levels, with two different trust postures:

  * Deterministic breakdowns (counts by severity / by source, the most recent entries) are plain
    read-only SQL built from the roles `logs.py` already tagged. The rows go to the USER only —
    this stays inside Blossa's normal "no raw rows to the LLM" boundary.

  * Root-cause clustering DOES send the actual error text to the model, so it crosses that boundary
    deliberately and is allowed ONLY when the configured provider is LOCAL (e.g. Ollama), where the
    data never leaves the machine. Even then the message is run through `redact_text` first as
    defence-in-depth. With a remote provider the feature refuses rather than leak data off-box.

The pure pieces (SQL builders, redaction, prompt/parse, the local-provider gate) live here; the CLI
and web layer wire them to a live database + provider.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from pydantic import BaseModel, Field

from .llm.base import LLMProvider
from .models import ConfidenceLevel, LogRole, LogTable
from .privacy import mask_value

# Providers that run on the same machine as Blossa — sending row text to them keeps it local.
LOCAL_PROVIDERS = frozenset({"ollama"})

# Severity values that count as an actual failure worth root-causing / filtering to.
ERROR_SEVERITIES = ("ERROR", "FATAL", "SEVERE", "CRITICAL", "FAILED", "FAIL")

_EMAIL_INLINE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A long numeric run (card / account / phone) — 7+ chars of digits with optional spaces/dashes.
_LONG_NUMBER = re.compile(r"\b\d[\d -]{5,}\d\b")


def is_local_provider(provider: LLMProvider) -> bool:
    """True if the provider runs locally, so sending row text to it doesn't leave the machine."""
    return provider.name in LOCAL_PROVIDERS


def redact_text(text: str) -> str:
    """Strip the obvious PII out of a free-text log message (emails, long account/card numbers).

    This is defence-in-depth on top of the local-provider gate — it reduces incidental PII, it is
    not a guarantee. Short numbers (error codes like ORA-00600, last-4 card tails, small ids) are
    left intact because they carry diagnostic value and are not, on their own, identifying.
    """
    s = _EMAIL_INLINE.sub(lambda m: mask_value(m.group(0)), text or "")
    return _LONG_NUMBER.sub("<number>", s)


# ------------------------------------------------------------- deterministic SQL


def _qualified(lt: LogTable) -> str:
    return f"{lt.owner}.{lt.table}" if lt.owner else lt.table


def _error_filter(severity_col: str) -> str:
    values = ", ".join(f"'{v}'" for v in ERROR_SEVERITIES)
    return f"UPPER({severity_col}) IN ({values})"


def severity_breakdown_sql(lt: LogTable) -> str | None:
    """`SELECT severity, COUNT(*) ...` over the log's severity column, or None if it has none."""
    sev = lt.column_for(LogRole.SEVERITY)
    if not sev:
        return None
    return (
        f"SELECT {sev} AS SEVERITY, COUNT(*) AS ENTRIES FROM {_qualified(lt)} "
        f"GROUP BY {sev} ORDER BY ENTRIES DESC"
    )


def source_breakdown_sql(lt: LogTable, *, only_errors: bool = True) -> str | None:
    """`SELECT source, COUNT(*) ...` — which module/job emitted the most (errors), or None."""
    src = lt.column_for(LogRole.SOURCE)
    if not src:
        return None
    sev = lt.column_for(LogRole.SEVERITY)
    where = f" WHERE {_error_filter(sev)}" if (only_errors and sev) else ""
    return (
        f"SELECT {src} AS SOURCE, COUNT(*) AS ENTRIES FROM {_qualified(lt)}{where} "
        f"GROUP BY {src} ORDER BY ENTRIES DESC"
    )


def recent_entries_sql(lt: LogTable, *, limit: int = 50, only_errors: bool = True) -> str:
    """Most recent log rows (the role columns only), newest first, capped at `limit`.

    The message column is SUBSTR-bounded so a CLOB comes back as a bounded string, never the whole
    blob. Used both for the user-facing "recent errors" view and to feed root-cause clustering.
    """
    when = lt.column_for(LogRole.EVENT_TIME)
    msg = lt.column_for(LogRole.MESSAGE)
    sev = lt.column_for(LogRole.SEVERITY)
    src = lt.column_for(LogRole.SOURCE)

    cols: list[str] = []
    if when:
        cols.append(when)
    if sev:
        cols.append(sev)
    if src:
        cols.append(src)
    if msg:
        cols.append(f"SUBSTR({msg}, 1, 2000) AS {msg}")
    select_cols = ", ".join(cols) if cols else "*"

    where = f" WHERE {_error_filter(sev)}" if (only_errors and sev) else ""
    order = f" ORDER BY {when} DESC" if when else ""
    n = max(1, int(limit))
    return f"SELECT {select_cols} FROM {_qualified(lt)}{where}{order} FETCH FIRST {n} ROWS ONLY"


# --------------------------------------------------------- root-cause (LLM) types


class ErrorCluster(BaseModel):
    """One recurring root cause the model distilled from the (masked) log entries."""

    cause: str
    count: int = 0
    severity: str = ""
    suggested_action: str = ""
    example: str = ""


class RootCauseReport(BaseModel):
    log_table: str
    sample_size: int = 0
    clusters: list[ErrorCluster] = Field(default_factory=list)
    note: str = ""


ROOT_CAUSE_SYSTEM_PROMPT = (
    "You are an SRE analysing an application's error log. You are given a sample of recent log "
    "entries (timestamps, severity, source, and a free-text message that has already been "
    "PII-redacted). Cluster them into the few DISTINCT root causes behind them.\n\n"
    "Rules:\n"
    "- Group entries that share a cause into ONE cluster; do not list every line.\n"
    "- For each cluster give: a short 'cause' label, how many sampled entries it covers "
    "('count'), the typical 'severity', a concrete 'suggested_action', and one short "
    "representative 'example'.\n"
    "- Base everything ONLY on the given entries — never invent errors you cannot see.\n"
    "- Order clusters by count, most frequent first.\n"
    "- The messages are already redacted; do not try to reconstruct removed values.\n"
    "- Respond with STRICT JSON only — no prose, no markdown fences."
)

_ROOT_CAUSE_CONTRACT = (
    "Respond with JSON of exactly this shape:\n"
    "{\n"
    '  "clusters": [\n'
    '    {"cause": "<short label>", "count": <int>, "severity": "<e.g. ERROR>",\n'
    '     "suggested_action": "<what to do>", "example": "<one short representative message>"}\n'
    "  ]\n"
    "}"
)


def build_root_cause_prompt(log_table: str, entries: list[dict]) -> str:
    """Prompt for clustering. `entries` are already-redacted dicts (when/severity/source/msg)."""
    payload = json.dumps(entries, indent=2, default=str)
    return (
        f"Error log: {log_table}\n"
        f"Sampled entries ({len(entries)}, PII-redacted):\n{payload}\n\n"
        f"{_ROOT_CAUSE_CONTRACT}"
    )


def redact_entries(rows: list[dict], message_col: str | None) -> list[dict]:
    """Copy DB rows for the prompt, redacting the message column's free text."""
    out: list[dict] = []
    for row in rows:
        clean = dict(row)
        if message_col and message_col in clean and clean[message_col] is not None:
            clean[message_col] = redact_text(str(clean[message_col]))
        out.append(clean)
    return out


def parse_root_cause_response(log_table: str, raw: str, sample_size: int) -> RootCauseReport:
    data = _loads_lenient(raw)
    clusters: list[ErrorCluster] = []
    if isinstance(data, dict) and isinstance(data.get("clusters"), list):
        for item in data["clusters"]:
            if not isinstance(item, dict):
                continue
            clusters.append(
                ErrorCluster(
                    cause=str(item.get("cause") or "").strip() or "Unclassified",
                    count=_as_int(item.get("count")),
                    severity=str(item.get("severity") or "").strip(),
                    suggested_action=str(item.get("suggested_action") or "").strip(),
                    example=str(item.get("example") or "").strip(),
                )
            )
    note = "" if clusters else "The model did not return any clusters."
    return RootCauseReport(
        log_table=log_table, sample_size=sample_size, clusters=clusters, note=note
    )


def run_root_cause(
    provider: LLMProvider,
    log_table: str,
    redacted_entries: list[dict],
) -> RootCauseReport:
    """Call the (local) provider to cluster already-redacted entries into root causes."""
    raw = provider.generate(
        ROOT_CAUSE_SYSTEM_PROMPT, build_root_cause_prompt(log_table, redacted_entries)
    )
    return parse_root_cause_response(log_table, raw, len(redacted_entries))


# ------------------------------------------------------------------- selection


def choose_log_table(log_tables: list[LogTable], name: str | None) -> LogTable | None:
    """Pick which log to analyse: the named one, else the single one, else the best error log."""
    if not log_tables:
        return None
    if name:
        wanted = name.upper()
        return next(
            (lt for lt in log_tables
             if lt.table.upper() == wanted or _qualified(lt).upper() == wanted),
            None,
        )
    if len(log_tables) == 1:
        return log_tables[0]
    # Prefer an error log with both a message and a severity, by confidence.
    rank = {ConfidenceLevel.HIGH: 0, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.LOW: 2}
    scored = sorted(
        log_tables,
        key=lambda lt: (
            0 if lt.kind.value == "error" else 1,
            rank.get(lt.confidence, 3),
        ),
    )
    return scored[0]


# ------------------------------------------------------------------- helpers


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _loads_lenient(raw: str) -> object:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


# Re-exported for the CLI/web to surface a consistent message.
def local_only_message() -> str:
    return (
        "Root-cause explanation reads the actual error text, so Blossa only does it with a LOCAL "
        "model (e.g. Ollama) where the data never leaves your machine. The configured provider is "
        "remote — switch to ollama (or run the deterministic breakdown without --explain)."
    )


ProgressFn = Callable[[str], None]
