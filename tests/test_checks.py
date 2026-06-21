from blossa.checks import run_checks
from blossa.demo import build_demo_schema
from blossa.models import (
    ColumnInfo,
    ConfidenceLevel,
    ConstraintInfo,
    ConstraintType,
    FindingKind,
    SchemaInfo,
    TableInfo,
)


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


# --------------------------------------------------------- suffix / self-ref FK inference


def _num(name: str, prec: int) -> ColumnInfo:
    return ColumnInfo(name=name, data_type="NUMBER", data_precision=prec, nullable=False)


def _hr_like_schema() -> SchemaInfo:
    """EMPLOYEES.MANAGER_ID is a self-FK to EMPLOYEES.EMPLOYEE_ID — same '_ID' suffix, not the
    same name. DEPARTMENT_ID is an ordinary (exact-name) undeclared FK to DEPARTMENTS."""
    employees = TableInfo(
        name="EMPLOYEES",
        columns=[_num("EMPLOYEE_ID", 6), _num("MANAGER_ID", 6), _num("DEPARTMENT_ID", 4)],
        constraints=[
            ConstraintInfo(name="PK_EMP", type=ConstraintType.PRIMARY_KEY, columns=["EMPLOYEE_ID"])
        ],
    )
    departments = TableInfo(
        name="DEPARTMENTS",
        columns=[_num("DEPARTMENT_ID", 4)],
        constraints=[
            ConstraintInfo(
                name="PK_DEPT", type=ConstraintType.PRIMARY_KEY, columns=["DEPARTMENT_ID"]
            )
        ],
    )
    return SchemaInfo(name="HR", tables=[employees, departments])


class _FakeDB:
    """Returns a controlled value-overlap per (child_col, parent_col) pair the check queries."""

    def __init__(self, overlaps: dict[tuple[str, str], float]):
        self._overlaps = overlaps

    def query(self, sql: str, binds=None):
        frac = None
        for (child, parent), f in self._overlaps.items():
            if f'"{child}"' in sql and f'p."{parent}"' in sql:
                frac = f
                break
        if frac is None:
            return [{"DISTINCT_TOTAL": 0, "MATCHED": 0, "ORPHAN_ROWS": 0}]
        total = 100
        matched = round(frac * total)
        return [{"DISTINCT_TOTAL": total, "MATCHED": matched, "ORPHAN_ROWS": total - matched}]


def test_self_referential_fk_inferred_by_suffix_and_data():
    schema = _hr_like_schema()
    db = _FakeDB(
        {
            ("MANAGER_ID", "EMPLOYEE_ID"): 1.0,   # managers are employees -> full overlap
            ("MANAGER_ID", "DEPARTMENT_ID"): 0.1,  # decoy: same '_ID' suffix, but data disagrees
            ("DEPARTMENT_ID", "DEPARTMENT_ID"): 1.0,
            # A PK column whose values happen to overlap another key — must NOT become an FK.
            ("DEPARTMENT_ID", "EMPLOYEE_ID"): 1.0,
        }
    )
    relationships, _ = run_checks(schema, db=db, owner="HR")
    inferred = {
        (r.from_table, tuple(r.from_columns), r.to_table) for r in relationships if not r.declared
    }
    # The self-referential manager FK is rediscovered despite the name mismatch...
    assert ("EMPLOYEES", ("MANAGER_ID",), "EMPLOYEES") in inferred
    # ...and the ordinary exact-name candidate still works.
    assert ("EMPLOYEES", ("DEPARTMENT_ID",), "DEPARTMENTS") in inferred
    # ...but a column that is its own table's primary key is never inferred as an FK,
    # even when its values overlap another key (precision guard).
    assert ("DEPARTMENTS", ("DEPARTMENT_ID",), "EMPLOYEES") not in inferred

    mgr = next(r for r in relationships if r.from_columns == ["MANAGER_ID"])
    assert mgr.to_columns == ["EMPLOYEE_ID"]  # data picked EMPLOYEE_ID over the DEPARTMENT_ID decoy
    assert mgr.confidence == ConfidenceLevel.HIGH
    assert any("self-reference" in e for e in mgr.evidence)


def test_suffix_fk_not_inferred_offline():
    """Precision guard: without a live DB the suffix heuristic must NOT fire (it would flood
    every '_ID' column with guesses). Exact-name candidates remain LOW-confidence as before."""
    schema = _hr_like_schema()
    relationships, _ = run_checks(schema)  # no db
    inferred = {(r.from_table, tuple(r.from_columns)) for r in relationships if not r.declared}
    assert ("EMPLOYEES", ("MANAGER_ID",)) not in inferred
    assert ("EMPLOYEES", ("DEPARTMENT_ID",)) in inferred  # exact-name match still works offline
