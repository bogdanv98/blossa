# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Choosing the slice of the map one question needs (src/blossa/relevance.py)."""

from datetime import UTC, datetime

from blossa.models import (
    ColumnInfo,
    ColumnSemantics,
    ConfidenceLevel,
    ConstraintInfo,
    ConstraintType,
    LogColumn,
    LogKind,
    LogRole,
    LogTable,
    ProgramKind,
    ProgramSemantics,
    ProgramUnit,
    Relationship,
    ScanMetadata,
    ScanReport,
    SchemaInfo,
    TableInfo,
    TableSemantics,
)
from blossa.relevance import DEFAULT_MAX_TABLES, select_map_slice, tokenize


def _table(name: str, columns: list[str], *, fk: tuple[str, str] | None = None) -> TableInfo:
    constraints = []
    if fk:
        column, target = fk
        constraints.append(
            ConstraintInfo(
                name=f"FK_{name}_{target}",
                type=ConstraintType.FOREIGN_KEY,
                columns=[column],
                referenced_table=target,
            )
        )
    return TableInfo(
        name=name,
        owner="BANKDEMO",
        columns=[ColumnInfo(name=c, data_type="VARCHAR2", nullable=True) for c in columns],
        constraints=constraints,
    )


def _fk(child: str, column: str, parent: str) -> Relationship:
    return Relationship(
        from_table=child,
        from_columns=[column],
        to_table=parent,
        to_columns=[f"{parent[:-1]}_ID"],
        declared=True,
        from_owner="BANKDEMO",
        to_owner="BANKDEMO",
    )


def _bank_report() -> ScanReport:
    """A schema too big to send whole: six banking tables plus twelve unrelated ones."""
    tables = [
        _table("CUSTOMERS", ["CUSTOMER_ID", "FULL_NAME", "COUNTRY"]),
        _table("ACCOUNTS", ["ACCOUNT_ID", "CUSTOMER_ID", "STATUS", "BALANCE"],
               fk=("CUSTOMER_ID", "CUSTOMERS")),
        _table("TRANSACTIONS", ["TXN_ID", "ACCOUNT_ID", "AMOUNT", "TXN_DATE"],
               fk=("ACCOUNT_ID", "ACCOUNTS")),
        _table("CARDS", ["CARD_ID", "ACCOUNT_ID", "EXPIRES_AT"], fk=("ACCOUNT_ID", "ACCOUNTS")),
        _table("LOANS", ["LOAN_ID", "CUSTOMER_ID", "PRINCIPAL"], fk=("CUSTOMER_ID", "CUSTOMERS")),
        _table("FEE_CHARGE", ["FEE_ID", "ACCOUNT_ID", "AMOUNT"], fk=("ACCOUNT_ID", "ACCOUNTS")),
        _table("ERROR_LOG", ["ERROR_ID", "LOG_TIME", "SEVERITY", "MESSAGE"]),
    ]
    tables += [_table(f"ST_MISC_{i}", ["ID", "CODE", "VALUE"]) for i in range(1, 13)]
    return ScanReport(
        metadata=ScanMetadata(
            blossa_version="test",
            schema_name="BANKDEMO",
            generated_at=datetime.now(UTC),
            llm_provider="heuristic",
        ),
        schema_info=SchemaInfo(name="BANKDEMO", tables=tables),
        relationships=[
            _fk("ACCOUNTS", "CUSTOMER_ID", "CUSTOMERS"),
            _fk("TRANSACTIONS", "ACCOUNT_ID", "ACCOUNTS"),
            _fk("CARDS", "ACCOUNT_ID", "ACCOUNTS"),
            _fk("LOANS", "CUSTOMER_ID", "CUSTOMERS"),
            _fk("FEE_CHARGE", "ACCOUNT_ID", "ACCOUNTS"),
        ],
        log_tables=[
            LogTable(
                table="ERROR_LOG",
                owner="BANKDEMO",
                kind=LogKind.ERROR,
                confidence=ConfidenceLevel.HIGH,
                columns=[
                    LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
                    LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
                ],
            )
        ],
    )


def _kept(report: ScanReport, question: str, **kw) -> set[str]:
    sl = select_map_slice(report, question, **kw)
    return {key.split(".")[-1] for key in sl.tables}


# --------------------------------------------------------------- tokenizing


def test_tokenize_drops_noise_and_folds_diacritics():
    assert tokenize("Câte conturi avem pe fiecare status?") == tokenize("cate conturi status")
    assert "cate" not in tokenize("cate conturi")  # stopword
    assert tokenize("ACCOUNT_ID") == tokenize("account id")  # identifiers split on _


def test_stemmer_collapses_plurals_in_both_languages():
    assert tokenize("customers") == tokenize("customer")
    assert tokenize("conturi") == tokenize("cont")
    assert tokenize("tranzactii") == tokenize("tranzactie")


# --------------------------------------------------------------- selection


def test_small_map_is_sent_whole():
    # Below the budget nothing is filtered: a small schema behaves exactly as it did before
    # there was a budget at all, note and all.
    report = _bank_report()
    report.schema_info.tables = report.schema_info.tables[:6]
    sl = select_map_slice(report, "how many accounts?")
    assert not sl.trimmed
    assert len(sl.tables) == 6 and not sl.omitted_tables


def test_question_words_pick_the_tables():
    kept = _kept(_bank_report(), "how many accounts per status?")
    assert "ACCOUNTS" in kept
    assert not any(name.startswith("ST_MISC") for name in kept)


def test_the_bridge_table_of_a_join_survives():
    # The question names transactions and customers; they are joinable only THROUGH accounts,
    # which the question never mentions. Cutting it would leave a query that cannot be written.
    kept = _kept(_bank_report(), "how many transactions per customer?")
    assert {"TRANSACTIONS", "CUSTOMERS", "ACCOUNTS"} <= kept


def test_a_follow_up_keeps_the_tables_of_the_previous_query():
    # "only the top 5" says nothing about loans — the query it refines does.
    kept = _kept(
        _bank_report(),
        "only the top 5",
        history_sql=["SELECT l.LOAN_ID FROM LOANS l ORDER BY l.PRINCIPAL DESC"],
    )
    assert "LOANS" in kept


def test_an_alias_in_the_history_sql_is_not_mistaken_for_a_table():
    kept = _kept(_bank_report(), "only the top 5", history_sql=["SELECT c.ID FROM CARDS c"])
    assert "CARDS" in kept and "CUSTOMERS" not in kept  # 'c' is an alias, not CUSTOMERS


def test_romanian_question_finds_an_english_table_through_the_map():
    # The map IS the model's vocabulary: a Romanian purpose is what connects "conturi" to
    # a table called ACCOUNTS. This is the same effect that fixed the CUSTOMERS/ACCOUNTS
    # mix-up when the map was first written in Romanian.
    report = _bank_report()
    report.semantics.append(
        TableSemantics(
            table="ACCOUNTS",
            purpose="Conturile bancare ale clientilor.",
            confidence=ConfidenceLevel.HIGH,
            columns=[
                ColumnSemantics(
                    column="STATUS", meaning="Starea contului", confidence=ConfidenceLevel.MEDIUM
                )
            ],
        )
    )
    assert "ACCOUNTS" in _kept(report, "cate conturi avem pe fiecare status?")


def test_a_romanian_question_finds_an_english_map():
    # The map is written once, at scan time, in one language; the analyst asks in whatever they
    # think in. "comisioane" scores zero against "Fees charged to accounts" — and FEE_CHARGE is
    # then cut from the very report that needs it. The shared business vocabulary is the bridge.
    report = _bank_report()
    report.semantics.append(
        TableSemantics(
            table="FEE_CHARGE",
            purpose="Fees charged to accounts, including waived ones.",
            confidence=ConfidenceLevel.HIGH,
        )
    )
    assert "FEE_CHARGE" in _kept(report, "ce comisioane am incasat luna trecuta?")


def test_one_shared_column_name_is_not_relevance():
    # STATUS, CODE and CREATED_AT appear all over a real schema. Scoring above zero must not be
    # enough to spend a slot: the field is cut against the best table, not against zero.
    report = _bank_report()
    for table in report.schema_info.tables:
        if table.name.startswith("ST_MISC"):
            table.columns.append(ColumnInfo(name="STATUS", data_type="VARCHAR2", nullable=True))
    kept = _kept(report, "how many accounts per status?")
    assert "ACCOUNTS" in kept
    assert not any(name.startswith("ST_MISC") for name in kept)


def test_the_budget_is_a_ceiling_not_a_target():
    # A question about one table must not drag in eleven more just because there is room.
    sl = select_map_slice(_bank_report(), "how many accounts per status?", max_tables=12)
    assert len(sl.tables) < 12


def test_a_question_about_errors_keeps_the_log_table():
    assert "ERROR_LOG" in _kept(_bank_report(), "care sunt cele mai frecvente erori?")


def test_a_program_named_in_the_question_pulls_in_the_tables_it_uses():
    report = _bank_report()
    report.schema_info.program_units.append(
        ProgramUnit(name="CORE_BANKING", owner="BANKDEMO", kind=ProgramKind.PACKAGE, source="")
    )
    report.program_semantics.append(
        ProgramSemantics(
            name="CORE_BANKING",
            owner="BANKDEMO",
            kind=ProgramKind.PACKAGE,
            summary="Core banking operations.",
            tables_used=["FEE_CHARGE"],
            confidence=ConfidenceLevel.HIGH,
        )
    )
    assert "FEE_CHARGE" in _kept(report, "what does core_banking do?")


def test_what_is_left_out_is_named_never_hidden():
    # The model must be able to say "I need ORDERS" instead of inventing its columns.
    sl = select_map_slice(_bank_report(), "how many accounts per status?")
    assert sl.trimmed
    assert any(name.startswith("ST_MISC") for name in sl.omitted_tables)
    assert sl.omitted_table_count == len(_bank_report().schema_info.tables) - len(sl.tables)


def test_a_question_matching_nothing_falls_back_to_the_connected_core():
    # No schema word in sight. The best blind guess is the tables the schema is built around,
    # not the first N in catalog order — and never an empty slice.
    sl = select_map_slice(_bank_report(), "cat am pierdut anul trecut?")
    kept = {key.split(".")[-1] for key in sl.tables}
    assert len(sl.tables) == DEFAULT_MAX_TABLES
    assert {"ACCOUNTS", "CUSTOMERS"} <= kept


def test_the_budget_is_respected():
    sl = select_map_slice(_bank_report(), "accounts customers transactions cards loans fees",
                          max_tables=3)
    assert len(sl.tables) == 3
