# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Pick the slice of the map that one question actually needs.

Every `ask` used to ship the WHOLE map — all tables, all their columns and meanings, every
relationship, every program summary. On a 17-table demo that is already ~12k tokens, which is both
the accuracy ceiling (the answer is buried in noise) and the speed ceiling (a bigger context means
fewer layers of the model fit in VRAM). On a real bank schema with thousands of tables it does not
fit at all, at any window size.

So the map is filtered per question, deterministically and with no model call:

  * score each table on the question's own words — its name, its purpose, its column names, its
    column meanings — with a light stemmer so "conturi" finds ACCOUNTS and "customers" finds
    CUSTOMER;
  * add the tables the conversation already queried (a follow-up must keep its own FROM clause);
  * add the tables named by a program the question mentions;
  * expand ONE hop along the relationship graph, so the join path between two wanted tables — and
    the bridge table in the middle — survives the cut;
  * keep the highest-scoring `max_tables` of those.

What is left out is not hidden: the names of the omitted tables still go to the model, so it can
say "I need ORDERS, which is not in the slice you gave me" instead of inventing its columns. That
is the whole reason this is a filter and not a truncation.

The fallback matters as much as the scoring: a question that matches nothing (a vague "how much did
we lose last year") falls back to the most connected tables, which are the ones a schema is built
around — never to an empty slice.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .models import ScanReport

# How much of the map one question is allowed to carry. Twelve tables is roughly what a 14B local
# model can hold in mind at once while still following the join rules; raise it for a bigger model
# (llm.max_map_tables), lower it if answers start drifting to the wrong table.
DEFAULT_MAX_TABLES = 12
DEFAULT_MAX_PROGRAMS = 20
# Names-only index of what was cut. Cheap (a few tokens each) and it keeps "does X exist" honest —
# but on a 4000-table schema even names add up, so it is capped too.
MAX_OMITTED_NAMES = 120
MAX_LOG_TABLES = 10

# Scoring weights. They are relative, not absolute: a table whose NAME the question says outranks
# one that merely has a matching column, which outranks one that only matches a column meaning.
_W_TABLE_NAME = 10
_W_PURPOSE = 3
_W_COLUMN_NAME = 2
_W_COLUMN_MEANING = 1
_W_HISTORY = 12  # the previous turn's query already used it — a refinement must not lose it
_W_PROGRAM = 6  # a program the question names reads/writes it
_W_LOG = 4  # the question is about errors/logs and this is a log table
_W_NEIGHBOUR = 2  # one FK hop from a wanted table: the join path, and the bridge in the middle
# What a table must score to count as a direct hit: one word that is genuinely its own — its name,
# a rare word in its purpose, a distinctive column — rather than vocabulary the whole schema shares
# (which, weighted by rarity, adds up to a fraction of a point).
_MIN_DIRECT_SCORE = _W_PURPOSE

_WORD = re.compile(r"[A-Za-zÀ-ɏ][A-Za-z0-9À-ɏ]*")
_SQL_WORD = re.compile(r"[A-Za-z_$#][\w$#]*")

# Words that carry no schema signal. Both languages Blossa is asked in, plus the SQL-ish verbs a
# question wraps around the nouns that matter.
_STOPWORDS = {
    # English
    "a", "about", "all", "and", "any", "are", "as", "at", "average", "be", "between", "but", "by",
    "can", "count", "did", "do", "does", "each", "every", "for", "from", "get", "give", "group",
    "has", "have", "how", "in", "into", "is", "it", "its", "last", "list", "many", "me", "most",
    "much", "my", "no", "not", "of", "on", "one", "only", "or", "our", "per", "show", "since",
    "some", "sum", "that", "the", "their", "there", "they", "this", "to", "top", "total", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "with", "year", "you",
    # Romanian (unaccented — the tokenizer strips diacritics before this set is consulted)
    "acum", "al", "ale", "alta", "alte", "am", "ai", "arata", "au", "avem", "care",
    "cat", "cata", "cate", "cati", "catre", "ce", "cea", "cele", "cine", "cu", "cum", "da", "dar",
    "acea", "aceasta", "aceste", "acesti", "acest", "asta", "astea", "atat", "atatea",
    "de", "din", "doar", "dupa", "ei", "el", "era", "este", "eu", "fara", "fi", "fie", "fiecare",
    "fost",
    "ii", "il", "imi", "intre", "isi", "la", "le", "lor", "lui", "mai", "mea", "mi", "mult",
    "multe", "multi", "ne", "ni", "noi", "nostru", "nu", "numai", "o", "pe", "pentru", "peste",
    "pot", "poti", "prin", "sa", "sau", "se", "si", "sunt", "sub", "spune", "te", "toate", "toti",
    "tot", "un", "una", "unde", "unei", "unor", "unu", "va", "vor", "vrea",
}

# A question about errors/logs should keep the log tables even when it names none of them.
_LOG_WORDS = {
    "error", "errors", "log", "logs", "logging", "exception", "exceptions", "failure", "failures",
    "audit", "incident", "incidents", "severity", "stacktrace", "eroare", "erori", "eroril",
    "jurnal", "loguri", "esec", "esecuri", "exceptie", "exceptii", "auditul",
}

# Plural/inflection tails, longest first. This is a stemmer only in the loosest sense: it exists so
# "conturi"/"cont", "clienti"/"client" and "accounts"/"account" collapse to the same token, not to
# be linguistically right.
_SUFFIXES = ("urilor", "elor", "ilor", "uri", "ile", "ele", "lui", "ii", "ul", "es", "s", "i", "e")


def _stem_all(words: Iterable[str]) -> set[str]:
    return {_stem(w) for w in words}


def _fold(text: str) -> str:
    """Lower-case and strip diacritics, so `ținte` and `tinte` are the same word."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stem(token: str) -> str:
    """Strip inflection tails until none is left — never below four characters.

    Repeatedly, because Romanian stacks them: tranzactii -> tranzacti -> tranzact, which is where
    tranzactie also lands. One pass would leave the two forms one letter apart and unmatched.
    """
    for _ in range(3):
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                token = token[: -len(suffix)]
                break
        else:
            break
    return token


def tokenize(text: str) -> set[str]:
    """Content words of `text`, folded and stemmed. Identifiers split on _ and camelCase."""
    tokens: set[str] = set()
    for raw in _WORD.findall(_fold(text.replace("_", " "))):
        for part in re.findall(r"[a-z][a-z0-9]*", raw):
            if len(part) < 3 or part in _STOPWORDS:
                continue
            tokens.add(_stem(part))
    return tokens


_LOG_STEMS = _stem_all(_LOG_WORDS)

# The map is written in ONE language (llm.language, set at scan time) and the question arrives in
# whatever language the analyst thinks in. When they differ there is no lexical bridge at all:
# "cate comisioane" scores exactly zero against a table whose purpose says "fees charged to
# accounts", and FEE_CHARGE is cut from the very report that needs it. This is the bridge — the
# business vocabulary the two languages share, as data rather than logic, so it can be extended
# without touching the scorer. It is NOT a translator: only the question's own words are expanded,
# and only for scoring.
_SYNONYM_GROUPS = [
    ("cont", "account"), ("client", "customer"), ("tranzactie", "transaction"),
    ("comision", "fee"), ("taxa", "charge"), ("imprumut", "loan"), ("credit", "loan"),
    ("restanta", "arrears"), ("intarziere", "overdue"), ("sold", "balance"),
    ("dobanda", "interest"), ("plata", "payment"), ("factura", "invoice"),
    ("angajat", "employee"), ("departament", "department"), ("salariu", "salary"),
    ("comanda", "order"), ("produs", "product"), ("furnizor", "supplier"),
    ("adresa", "address"), ("tara", "country"), ("oras", "city"), ("moneda", "currency"),
    ("suma", "amount"), ("pret", "price"), ("cantitate", "quantity"), ("stoc", "stock"),
    ("utilizator", "user"), ("alerta", "alert"), ("sucursala", "branch"), ("filiala", "branch"),
    ("scadenta", "maturity"), ("rambursare", "repayment"), ("retragere", "withdrawal"),
    ("depunere", "deposit"), ("extras", "statement"), ("garantie", "collateral"),
    ("stare", "status"), ("nume", "name"), ("rol", "role"), ("contract", "contract"),
    ("card", "card"), ("banca", "bank"),
]
_SYNONYMS: dict[str, set[str]] = {}
for _group in _SYNONYM_GROUPS:
    _stems = _stem_all(_group)
    for _stem_word in _stems:
        _SYNONYMS.setdefault(_stem_word, set()).update(_stems)


def question_tokens(question: str) -> set[str]:
    """The question's words plus their known equivalents in the other language."""
    tokens = tokenize(question)
    return tokens | {alt for t in tokens for alt in _SYNONYMS.get(t, ())}


def _overlap(question: set[str], text: str) -> int:
    """How many distinct question words `text` contains — the unweighted count."""
    return len(question & tokenize(text))


@dataclass(frozen=True)
class _TableProfile:
    """One table's searchable words, tokenized once and reused for both rarity and scoring."""

    name: set[str]
    purpose: set[str]
    columns: set[str]
    meanings: set[str]

    @property
    def all_words(self) -> set[str]:
        return self.name | self.purpose | self.columns | self.meanings


def _profile(table, sem) -> _TableProfile:  # noqa: ANN001 - TableInfo / TableSemantics | None
    purpose = tokenize(sem.purpose) if sem else set()
    if table.comment:
        purpose |= tokenize(table.comment)
    meanings: set[str] = set()
    if sem:
        for column in sem.columns:
            meanings |= tokenize(column.meaning)
    columns: set[str] = set()
    for column in table.columns:
        columns |= tokenize(column.name)
    return _TableProfile(tokenize(table.name), purpose, columns, meanings)


def _rarity(profiles: list[_TableProfile]) -> dict[str, float]:
    """How much one word is worth as evidence, from 1.0 (unique) down towards 0.

    Half a banking schema mentions balances and amounts; exactly one table mentions arrears. Both
    are one word of overlap, and counting them equally is what lets a table full of common
    vocabulary outscore the one the question is actually about. So a word is worth what its
    scarcity says it is worth: the standard inverse-document-frequency, normalised so a word
    unique to one table scores the full weight of whatever matched it.
    """
    total = len(profiles) or 1
    seen: Counter[str] = Counter()
    for profile in profiles:
        seen.update(profile.all_words)
    ceiling = math.log(1 + total)
    return {word: math.log(1 + total / count) / ceiling for word, count in seen.items()}


def _weigh(matched: set[str], rarity: dict[str, float]) -> float:
    """The evidential weight of the matched words — DISTINCT words, each worth its scarcity.

    Distinct, not total occurrences: a purpose that says "account" six times is not six times as
    relevant as one that says it once, and counting repeats lets a chatty summary outrank the
    table the question actually named.
    """
    return sum(rarity.get(word, 1.0) for word in matched)


def table_key(owner: str | None, name: str) -> str:
    """Stable identity for a table. Two schemas may each own an ORDERS."""
    return f"{(owner or '').upper()}.{name.upper()}"


def _resolve(
    owner: str | None, name: str, known: dict[str, float], by_name: dict[str, str]
) -> str | None:
    """The key of a table referred to by owner+name, tolerating a missing or unmatched owner.

    Relationships carry owners only on a multi-schema scan, and an inferred one may name an owner
    the table list writes differently; falling back to the bare name keeps the FK hop working
    instead of silently dropping every edge.
    """
    key = table_key(owner, name)
    if key in known:
        return key
    fallback = by_name.get(name.upper())
    return fallback if fallback in known else None


@dataclass(frozen=True)
class MapSlice:
    """Which parts of the map go into this question's prompt."""

    tables: list[str] = field(default_factory=list)  # table_key(), most relevant first
    programs: list[str] = field(default_factory=list)  # table_key()-style program keys
    log_tables: list[str] = field(default_factory=list)
    # Tables that are here only because they are one join hop from a wanted one. They are needed
    # for their KEYS, not for their prose, and are rendered without column meanings.
    context_tables: list[str] = field(default_factory=list)
    omitted_tables: list[str] = field(default_factory=list)  # display names, capped
    omitted_table_count: int = 0
    omitted_program_count: int = 0
    trimmed: bool = False

    def keeps_table(self, owner: str | None, name: str) -> bool:
        return table_key(owner, name) in self._table_set

    def keeps_program(self, owner: str | None, name: str) -> bool:
        return table_key(owner, name) in self._program_set

    def keeps_log_table(self, owner: str | None, name: str) -> bool:
        return table_key(owner, name) in set(self.log_tables)

    def is_context_only(self, owner: str | None, name: str) -> bool:
        """True for a table kept only to carry a join — worth its keys, not its prose."""
        return table_key(owner, name) in set(self.context_tables)

    # Built on demand rather than stored: the dataclass is frozen and these are pure views of it.
    @property
    def _table_set(self) -> set[str]:
        return set(self.tables)

    @property
    def _program_set(self) -> set[str]:
        return set(self.programs)


def _names_in_sql(sql: str, known: dict[str, str]) -> set[str]:
    """Table keys the SQL text refers to. Word-level, so an alias or a column cannot match."""
    hits: set[str] = set()
    for word in _SQL_WORD.findall(sql.upper()):
        key = known.get(word)
        if key:
            hits.add(key)
    return hits


def _program_tables(report: ScanReport, question: set[str], by_name: dict[str, str]) -> set[str]:
    """Tables used by any program the question names — by unit name or by packaged routine."""
    wanted: set[str] = set()
    for sem in report.program_semantics:
        named = bool(question & tokenize(sem.name)) or any(
            question & tokenize(r.name) for r in sem.routines
        )
        if not named:
            continue
        for used in sem.tables_used:
            key = by_name.get(used.split(".")[-1].upper())
            if key:
                wanted.add(key)
    return wanted


def select_map_slice(
    report: ScanReport,
    question: str,
    *,
    history_sql: Sequence[str] = (),
    max_tables: int = DEFAULT_MAX_TABLES,
    max_programs: int = DEFAULT_MAX_PROGRAMS,
) -> MapSlice:
    """Score the map against one question and keep the part worth sending.

    Returns everything (trimmed=False) when the map already fits the budget — a small schema
    should behave exactly as it did before there was a budget at all.
    """
    tables = report.schema_info.tables
    units = report.schema_info.program_units
    if len(tables) <= max_tables and len(units) <= max_programs:
        return MapSlice(
            tables=[table_key(t.owner, t.name) for t in tables],
            programs=[table_key(u.owner, u.name) for u in units],
            log_tables=[table_key(lt.owner, lt.table) for lt in report.log_tables],
        )

    qtokens = question_tokens(question)
    keys = [table_key(t.owner, t.name) for t in tables]
    # Unqualified name -> key, for the lookups that only have a bare name (SQL text, tables_used).
    # An ambiguous name across schemas resolves to the first owner; the FK hop recovers the rest.
    by_name: dict[str, str] = {}
    for table, key in zip(tables, keys, strict=True):
        by_name.setdefault(table.name.upper(), key)

    profiles = [_profile(table, report.semantics_for(table.name)) for table in tables]
    rarity = _rarity(profiles)

    scores: dict[str, float] = dict.fromkeys(keys, 0.0)
    for profile, key in zip(profiles, keys, strict=True):
        scores[key] = (
            _W_TABLE_NAME * _weigh(qtokens & profile.name, rarity)
            + _W_PURPOSE * _weigh(qtokens & profile.purpose, rarity)
            + _W_COLUMN_NAME * _weigh(qtokens & profile.columns, rarity)
            + _W_COLUMN_MEANING * _weigh(qtokens & profile.meanings, rarity)
        )

    # A follow-up ("now break it down by year") refines the previous query. Whatever that query
    # was built on has to stay in the slice, however little the new words look like it.
    for sql in history_sql:
        for key in _names_in_sql(sql, by_name):
            scores[key] += _W_HISTORY

    for key in _program_tables(report, qtokens, by_name):
        scores[key] += _W_PROGRAM

    asks_about_logs = bool(qtokens & _LOG_STEMS)
    if asks_about_logs:
        for lt in report.log_tables:
            key = table_key(lt.owner, lt.table)
            if key in scores:
                scores[key] += _W_LOG

    edges: list[tuple[str, str]] = []
    degree: dict[str, int] = dict.fromkeys(keys, 0)
    for rel in report.relationships:
        src = _resolve(rel.from_owner, rel.from_table, scores, by_name)
        dst = _resolve(rel.to_owner, rel.to_table, scores, by_name)
        if src is None or dst is None or src == dst:
            continue
        edges.append((src, dst))
        degree[src] += 1
        degree[dst] += 1

    order = {key: i for i, key in enumerate(keys)}
    kept, context = _rank(keys, scores, edges, degree, order, max_tables)
    kept_set = set(kept)

    omitted = [t.name for t, k in zip(tables, keys, strict=True) if k not in kept_set]
    programs = _select_programs(report, qtokens, kept_set, by_name, max_programs)
    # A log table is a few names and roles — cheap enough to keep whenever the question is about
    # errors at all, and a question about errors that gets no log table is answered from nothing.
    # It also survives when its base table was cut, or was never in the map to begin with.
    log_tables = [
        table_key(lt.owner, lt.table)
        for lt in report.log_tables
        if asks_about_logs or table_key(lt.owner, lt.table) in kept_set
    ][:MAX_LOG_TABLES]

    return MapSlice(
        tables=sorted(kept, key=lambda k: order[k]),
        programs=programs,
        log_tables=log_tables,
        context_tables=context,
        omitted_tables=omitted[:MAX_OMITTED_NAMES],
        omitted_table_count=len(omitted),
        omitted_program_count=len(units) - len(programs),
        trimmed=True,
    )


def _rank(
    keys: list[str],
    scores: dict[str, float],
    edges: list[tuple[str, str]],
    degree: dict[str, int],
    order: dict[str, int],
    max_tables: int,
) -> tuple[list[str], list[str]]:
    """The tables to keep, and which of them are there only to carry a join.

    `max_tables` is a CEILING, not a target. Filling every free slot with whatever scored next is
    how a question about accounts ends up carrying LOCATIONS: the neighbours of a neighbour are
    not context, they are noise, and each one costs the model attention it needs elsewhere. So the
    hop-expanded tables are added only up to a soft limit that grows with the number of tables the
    question actually named.
    """
    ranked = sorted((k for k in keys if scores[k] > 0), key=lambda k: (-scores[k], order[k]))
    if not ranked:
        # Nothing in the question resembles this schema's vocabulary. The most connected tables are
        # the ones the schema is built around, so they are the best blind guess — and far better
        # than the first N in catalog order, which is alphabetical accident.
        return sorted(keys, key=lambda k: (-degree[k], order[k]))[:max_tables], []
    # A single shared column name ("STATUS", "CREATED_AT") makes almost every table in a schema
    # score SOMETHING, so scoring above zero is not relevance. The bar is absolute rather than
    # relative to the best table on purpose: a long question that names six things makes its top
    # table score enormously, and a bar set as a fraction of that would cut the five tables that
    # each answer only one part of it — exactly the multi-table report this is meant to serve.
    kept = [k for k in ranked if scores[k] >= _MIN_DIRECT_SCORE][:max_tables]

    # ONE hop out from what is kept — deliberately not cascading: two hops on a well-connected
    # schema reaches everything and therefore selects nothing. Counted only now, against the
    # tables that survived the floor, so a weak direct score cannot make a table ineligible as
    # the bridge between two strong ones.
    seeds = set(kept)
    hops: dict[str, float] = dict.fromkeys(keys, 0.0)
    for src, dst in edges:
        if src in seeds and dst not in seeds:
            hops[dst] += _W_NEIGHBOUR
        if dst in seeds and src not in seeds:
            hops[src] += _W_NEIGHBOUR

    soft_limit = min(max_tables, max(6, 2 * len(kept) + 2))
    # Most hops first: a table linked to TWO kept tables is the bridge BETWEEN them, and without
    # it the join the question needs cannot be written at all.
    reachable = sorted(
        (k for k in keys if k not in seeds and hops[k] > 0),
        key=lambda k: (-hops[k], -scores[k], order[k]),
    )
    context: list[str] = []
    for key in reachable:
        if len(kept) >= soft_limit:
            break
        kept.append(key)
        context.append(key)
    return kept, context


def _select_programs(
    report: ScanReport,
    qtokens: set[str],
    kept_tables: set[str],
    by_name: dict[str, str],
    max_programs: int,
) -> list[str]:
    """Keep the programs the question names, then those working on the kept tables."""
    units = report.schema_info.program_units
    if len(units) <= max_programs:
        return [table_key(u.owner, u.name) for u in units]

    sem_by_key = {table_key(s.owner, s.name): s for s in report.program_semantics}
    scored: list[tuple[float, int, str]] = []
    for i, unit in enumerate(units):
        key = table_key(unit.owner, unit.name)
        score = _W_TABLE_NAME * _overlap(qtokens, unit.name)
        sem = sem_by_key.get(key)
        if sem:
            if any(qtokens & tokenize(r.name) for r in sem.routines):
                score += _W_TABLE_NAME
            score += _W_PURPOSE * _overlap(qtokens, sem.summary)
            # A program that reads the tables this question is about is part of its story even
            # when the question never says its name.
            score += _W_NEIGHBOUR * sum(
                1 for t in sem.tables_used if by_name.get(t.split(".")[-1].upper()) in kept_tables
            )
        scored.append((-score, i, key))
    scored.sort()
    return [key for _, _, key in scored[:max_programs]]
