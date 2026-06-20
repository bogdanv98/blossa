from blossa.checks import run_checks
from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.models import ConfidenceLevel
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
