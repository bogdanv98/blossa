from datetime import UTC, datetime

from blossa.checks import run_checks
from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.models import (
    ColumnInfo,
    ConfidenceLevel,
    Relationship,
    ScanMetadata,
    ScanReport,
    SchemaInfo,
    TableInfo,
)
from blossa.pipeline import run_scan_over_schema
from blossa.render import render_json, render_markdown
from blossa.summary import build_summaries


def test_heuristic_uses_existing_comment_with_high_confidence():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    summaries = {s.name: s for s in build_summaries(schema, relationships)}
    provider = HeuristicProvider()
    sem = provider.analyze(summaries["CUSTOMERS"])
    # CUSTOMERS has a table comment -> high confidence purpose.
    assert sem.confidence == ConfidenceLevel.HIGH
    email = next(c for c in sem.columns if c.column == "EMAIL")
    assert "email" in email.meaning.lower()


def test_heuristic_recognises_money_percent_address_and_title_columns():
    from blossa.models import ColumnSummary, TableSummary

    cols = ["SALARY", "MIN_SALARY", "COMMISSION_PCT", "JOB_TITLE", "STREET_ADDRESS", "CITY"]
    summary = TableSummary(
        name="JOBS",
        columns=[ColumnSummary(name=c, type="VARCHAR2(40)", nullable=True) for c in cols],
    )
    sem = HeuristicProvider().analyze(summary)
    by_col = {c.column: c for c in sem.columns}
    # All of these used to fall through to the LOW "meaning unclear" bucket; now each matches a
    # cross-domain naming pattern and earns at least medium confidence.
    for name in cols:
        assert by_col[name].confidence == ConfidenceLevel.MEDIUM, name
    assert "monetary" in by_col["SALARY"].meaning
    assert "percentage" in by_col["COMMISSION_PCT"].meaning
    assert "address" in by_col["STREET_ADDRESS"].meaning
    assert "title" in by_col["JOB_TITLE"].meaning


def test_heuristic_flags_unknown_column_low_confidence():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    summaries = {s.name: s for s in build_summaries(schema, relationships)}
    provider = HeuristicProvider()
    sem = provider.analyze(summaries["PRODUCTS"])
    pk = next(c for c in sem.columns if c.column == "PROD_ID")
    assert pk.confidence == ConfidenceLevel.HIGH  # primary key is high confidence


def test_full_demo_pipeline_renders_md_and_json():
    schema = build_demo_schema()
    settings = Settings()
    settings.llm.provider = "heuristic"
    report = run_scan_over_schema(
        schema, settings, HeuristicProvider(), db=None, owner=None
    )
    assert report.metadata.table_count == 6
    assert len(report.semantics) == 6

    md = render_markdown(report)
    assert "# Database Map" in md
    assert "BLOSSA_DEMO" in md
    assert "CUSTOMERS" in md

    data = render_json(report)
    assert '"table_count": 6' in data
    # Sanity: a raw value must not appear in the rendered artifacts either.
    assert "carmen.i@mail.example" not in md
    assert "carmen.i@mail.example" not in data

    # Single-schema maps stay flat — no per-owner grouping or qualified names.
    assert "## Schema:" not in md
    assert "<a id=" not in md


def _owned_table(name: str, owner: str) -> TableInfo:
    return TableInfo(name=name, owner=owner, columns=[ColumnInfo(name="ID", data_type="NUMBER")])


def _multi_schema_report() -> ScanReport:
    schema = SchemaInfo(
        name="HR+EXT",
        tables=[_owned_table("EMPLOYEES", "HR"), _owned_table("SALES_CONTACTS", "EXT")],
    )
    rel = Relationship(
        from_table="SALES_CONTACTS", from_columns=["ID"],
        to_table="EMPLOYEES", to_columns=["ID"], declared=False,
        from_owner="EXT", to_owner="HR",
    )
    meta = ScanMetadata(
        blossa_version="0.0.0", schema_name=schema.name,
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        llm_provider="heuristic", table_count=2,
    )
    return ScanReport(metadata=meta, schema_info=schema, relationships=[rel])


def test_multi_schema_map_groups_by_owner():
    md = render_markdown(_multi_schema_report())
    # Header counts the schemas; both the glance and the details group under per-owner headings.
    assert "across **2** schemas (HR, EXT)" in md
    assert "## Schema: HR" in md
    assert "## Schema: EXT" in md
    # Tables are owner-qualified and the glance links resolve via explicit anchors.
    assert "`HR.EMPLOYEES`" in md
    assert "(#ext-sales_contacts)" in md
    assert '<a id="ext-sales_contacts"></a>' in md
    # The cross-schema relationship is labelled as such.
    assert "_(cross-schema)_" in md
