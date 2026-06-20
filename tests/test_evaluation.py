from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.evaluation import build_ground_truth, evaluate
from blossa.llm.heuristic import HeuristicProvider
from blossa.pipeline import run_scan_over_schema


def _scan_demo():
    schema = build_demo_schema()
    settings = Settings()
    settings.llm.provider = "heuristic"
    report = run_scan_over_schema(schema, settings, HeuristicProvider(), db=None, owner=None)
    return schema, report


def test_build_ground_truth_captures_fks_and_comments():
    schema = build_demo_schema()
    gt = build_ground_truth(schema)
    assert gt["schema"] == "BLOSSA_DEMO"
    # ORDERS has a declared FK to CUSTOMERS.
    orders_fks = gt["tables"]["ORDERS"]["foreign_keys"]
    assert any(fk["to_table"] == "CUSTOMERS" for fk in orders_fks)
    # CUSTOMERS table + columns are documented.
    assert gt["tables"]["CUSTOMERS"]["comment"]
    assert "CUST_ID" in gt["tables"]["CUSTOMERS"]["columns"]
    # Undocumented table has no comment captured.
    assert gt["tables"]["PRODUCTS"]["comment"] is None


def test_evaluate_recovers_declared_fks():
    schema, report = _scan_demo()
    gt = build_ground_truth(schema)
    result = evaluate(gt, report)
    # Both declared FKs are present in the scan, so recall should be perfect.
    assert result.fk.truth_total == 2
    assert result.fk.recall == 1.0
    assert result.fk.missed == []


def test_evaluate_doc_coverage_with_heuristic():
    schema, report = _scan_demo()
    gt = build_ground_truth(schema)
    result = evaluate(gt, report)
    # CUSTOMERS is the only documented table; heuristic reuses its comment at high confidence.
    assert result.table_docs.documented == 1
    assert result.table_docs.coverage == 1.0
    # Documented columns (CUST_ID, EMAIL, TOTAL_AMT) all get a meaning.
    assert result.column_docs.documented == 3
    assert result.column_docs.coverage == 1.0


def test_fk_metrics_reports_missed():
    schema, report = _scan_demo()
    gt = build_ground_truth(schema)
    # Inject a fake real FK that Blossa cannot know about -> should be counted as missed.
    gt["tables"]["CUSTOMERS"]["foreign_keys"].append(
        {"from_columns": ["NAME"], "to_table": "STATUS_REF", "to_columns": ["DESCR"]}
    )
    result = evaluate(gt, report)
    assert result.fk.truth_total == 3
    assert result.fk.matched == 2
    assert any("CUSTOMERS" in m for m in result.fk.missed)
