from blossa.checks import run_checks
from blossa.demo import build_demo_schema
from blossa.models import FindingKind


def test_declared_relationships_detected():
    schema = build_demo_schema()
    relationships, _ = run_checks(schema)
    declared = [r for r in relationships if r.declared]
    pairs = {(r.from_table, r.to_table) for r in declared}
    assert ("ORDERS", "CUSTOMERS") in pairs
    assert ("ORDER_ITEMS", "ORDERS") in pairs


def test_candidate_fk_inferred_by_name():
    schema = build_demo_schema()
    relationships, findings = run_checks(schema)
    inferred = {
        (r.from_table, tuple(r.from_columns), r.to_table)
        for r in relationships
        if not r.declared
    }
    # PROD_ID in ORDER_ITEMS references PRODUCTS but is undeclared.
    assert ("ORDER_ITEMS", ("PROD_ID",), "PRODUCTS") in inferred
    assert any(f.kind == FindingKind.UNDECLARED_FK_CANDIDATE for f in findings)


def test_type_inconsistency_for_cust_id():
    schema = build_demo_schema()
    _, findings = run_checks(schema)
    type_findings = [f for f in findings if f.kind == FindingKind.TYPE_INCONSISTENCY]
    assert any("CUST_ID" in f.columns for f in type_findings)


def test_naming_inconsistency_dt_vs_date():
    schema = build_demo_schema()
    _, findings = run_checks(schema)
    naming = [f for f in findings if f.kind == FindingKind.NAMING_INCONSISTENCY]
    assert any("_DT" in f.message and "_DATE" in f.message for f in naming)


def test_missing_comments_reported():
    schema = build_demo_schema()
    _, findings = run_checks(schema)
    # PRODUCTS has no table comment; CUSTOMERS does.
    missing_table = {f.table for f in findings if f.kind == FindingKind.MISSING_TABLE_COMMENT}
    assert "PRODUCTS" in missing_table
    assert "CUSTOMERS" not in missing_table
