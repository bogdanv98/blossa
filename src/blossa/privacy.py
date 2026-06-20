# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""PII-safety helpers: turn raw cell values into structural *patterns* and *masked* samples.

These run in-process on values read from the DB. The raw value never leaves this module —
only the derived pattern (e.g. "AAA-999") or a masked sample (e.g. "c***@m***.example") does,
and those are the only column-value artifacts allowed to flow toward the LLM or the report.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def structural_pattern(value: str) -> str:
    """Collapse a value to a structural template: letters->A, digits->9, separators kept.

    "SKU-ABX-001" -> "AAA-AAA-999"; an email -> "email"; an ISO date -> "date".
    Long runs are collapsed so e.g. a 200-char note doesn't produce a 200-char pattern.
    """
    v = value.strip()
    if not v:
        return "empty"
    if _EMAIL_RE.match(v):
        return "email"
    if _UUID_RE.match(v):
        return "uuid"
    if _ISO_DATE_RE.match(v):
        return "date"

    out: list[str] = []
    for ch in v:
        if ch.isalpha():
            out.append("A")
        elif ch.isdigit():
            out.append("9")
        else:
            out.append(ch)
    template = "".join(out)
    # Collapse runs of 5+ identical class chars: "AAAAAAA" -> "A{7}".
    return re.sub(r"(.)\1{4,}", lambda m: f"{m.group(1)}{{{len(m.group(0))}}}", template)


def mask_value(value: str) -> str:
    """Return a masked sample that preserves shape but hides content.

    Emails keep their structure and TLD; everything else keeps first/last char and masks the
    middle. Short values are fully masked.
    """
    v = value.strip()
    if not v:
        return ""
    if _EMAIL_RE.match(v):
        local, _, domain = v.partition("@")
        host, _, tld = domain.rpartition(".")
        return f"{_edge(local)}@{_edge(host)}.{tld}"
    if len(v) <= 2:
        return "*" * len(v)
    return f"{v[0]}{'*' * (len(v) - 2)}{v[-1]}"


def _edge(s: str) -> str:
    if not s:
        return ""
    if len(s) == 1:
        return "*"
    return f"{s[0]}***"


def summarize_patterns(values: list[str], limit: int = 4) -> list[str]:
    """Distinct structural patterns across `values`, most common first, capped at `limit`."""
    counts: dict[str, int] = {}
    for raw in values:
        p = structural_pattern(str(raw))
        counts[p] = counts.get(p, 0) + 1
    ordered = sorted(counts, key=lambda p: (-counts[p], p))
    return ordered[:limit]


def masked_samples(values: list[str], limit: int = 3) -> list[str]:
    """A few distinct masked example values (deduplicated, order-stable)."""
    seen: list[str] = []
    for raw in values:
        m = mask_value(str(raw))
        if m and m not in seen:
            seen.append(m)
        if len(seen) >= limit:
            break
    return seen
