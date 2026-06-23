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


# --------------------------------------------------------- composite (multi-column) FK inference


def _composite_schema() -> SchemaInfo:
    """ORDER_ITEMS has a composite PK (ORDER_ID, ITEM_NO); ITEM_RETURNS carries both columns and
    so references it with a composite FK — the kind no Oracle sample schema actually declares."""
    order_items = TableInfo(
        name="ORDER_ITEMS",
        columns=[
            _num("ORDER_ID", 8),
            _num("ITEM_NO", 4),
            ColumnInfo(name="PRODUCT", data_type="VARCHAR2", data_length=40),
        ],
        constraints=[
            ConstraintInfo(
                name="PK_OI", type=ConstraintType.PRIMARY_KEY, columns=["ORDER_ID", "ITEM_NO"]
            )
        ],
    )
    item_returns = TableInfo(
        name="ITEM_RETURNS",
        columns=[_num("RETURN_ID", 10), _num("ORDER_ID", 8), _num("ITEM_NO", 4), _num("QTY", 4)],
        constraints=[
            ConstraintInfo(
                name="PK_IR", type=ConstraintType.PRIMARY_KEY, columns=["RETURN_ID"]
            )
        ],
    )
    return SchemaInfo(name="CFK", tables=[order_items, item_returns])


class _CompositeFakeDB:
    """Full overlap for the composite tuple-overlap query (it aliases columns C0, C1, ...),
    no overlap for any single-column query — isolating the composite pass under test."""

    def query(self, sql: str, binds=None):
        if "AS C1" in sql:  # the multi-column tuple-overlap query
            return [{"DISTINCT_TOTAL": 3, "MATCHED": 3, "ORPHAN_ROWS": 0}]
        return [{"DISTINCT_TOTAL": 0, "MATCHED": 0, "ORPHAN_ROWS": 0}]


def test_composite_fk_inferred_from_shared_key_columns():
    schema = _composite_schema()
    relationships, _ = run_checks(schema, db=_CompositeFakeDB(), owner="CFK")
    inferred = {
        (r.from_table, tuple(r.from_columns), r.to_table) for r in relationships if not r.declared
    }
    assert ("ITEM_RETURNS", ("ORDER_ID", "ITEM_NO"), "ORDER_ITEMS") in inferred
    rel = next(r for r in relationships if r.from_table == "ITEM_RETURNS" and not r.declared)
    assert rel.to_columns == ["ORDER_ID", "ITEM_NO"]
    assert rel.confidence == ConfidenceLevel.HIGH


def test_composite_fk_offline_is_low_confidence():
    """Offline (no DB) the composite name match is reported, but only LOW confidence — mirroring
    the single-column exact-name pass; data is what promotes it to MEDIUM/HIGH."""
    schema = _composite_schema()
    relationships, _ = run_checks(schema)  # no db
    comp = [
        r for r in relationships if not r.declared and r.from_columns == ["ORDER_ID", "ITEM_NO"]
    ]
    assert comp and comp[0].confidence == ConfidenceLevel.LOW


def _twin_surrogate_pk_schema() -> SchemaInfo:
    """Two unrelated tables each with a single-column surrogate PK named REC_ID. Their id ranges
    overlap by coincidence, but neither references the other — the classic surrogate-collision
    false positive that the symmetric-PK guard must suppress."""
    def _txt(name: str) -> ColumnInfo:
        return ColumnInfo(name=name, data_type="VARCHAR2", data_length=20)

    def _pk(name: str, cols: list[str]) -> ConstraintInfo:
        return ConstraintInfo(name=name, type=ConstraintType.PRIMARY_KEY, columns=cols)

    a = TableInfo(name="ALPHA", columns=[_num("REC_ID", 10), _txt("LABEL")],
                  constraints=[_pk("PK_A", ["REC_ID"])])
    b = TableInfo(name="BETA", columns=[_num("REC_ID", 10), _txt("NOTE")],
                  constraints=[_pk("PK_B", ["REC_ID"])])
    return SchemaInfo(name="TWIN", tables=[a, b])


class _FullOverlapDB:
    """Every value-overlap query returns 100% — so only the inference guards, not the data, can
    stop a candidate FK from being emitted."""

    def query(self, sql: str, binds=None):
        return [{"DISTINCT_TOTAL": 5, "MATCHED": 5, "ORPHAN_ROWS": 0}]


def test_symmetric_surrogate_pk_collision_suppressed():
    schema = _twin_surrogate_pk_schema()
    relationships, _ = run_checks(schema, db=_FullOverlapDB(), owner="TWIN")
    inferred = [r for r in relationships if not r.declared]
    # Despite identical names and full value overlap in BOTH directions, neither surrogate PK is
    # reported as a foreign key to the other.
    assert not any(r.from_columns == ["REC_ID"] for r in inferred)


def _composite_suffix_schema() -> SchemaInfo:
    """ORDER_ITEMS has composite PK (ORDER_ID, ITEM_NO). RETURN_LINES references it, but its
    columns are role-named (SOURCE_ORDER_ID, RETURN_ITEM_NO) — no exact-name overlap, so only the
    suffix+type+tuple-overlap pass can rediscover it. RETURN_ID is a decoy '_ID' column."""
    order_items = TableInfo(
        name="ORDER_ITEMS",
        columns=[
            _num("ORDER_ID", 8),
            _num("ITEM_NO", 4),
            ColumnInfo(name="PRODUCT", data_type="VARCHAR2", data_length=40),
        ],
        constraints=[
            ConstraintInfo(
                name="PK_OI", type=ConstraintType.PRIMARY_KEY, columns=["ORDER_ID", "ITEM_NO"]
            )
        ],
    )
    return_lines = TableInfo(
        name="RETURN_LINES",
        columns=[
            _num("RETURN_ID", 10),
            _num("SOURCE_ORDER_ID", 8),
            _num("RETURN_ITEM_NO", 4),
            _num("QTY", 4),
        ],
        constraints=[
            ConstraintInfo(name="PK_RL", type=ConstraintType.PRIMARY_KEY, columns=["RETURN_ID"])
        ],
    )
    return SchemaInfo(name="CFK", tables=[order_items, return_lines])


class _CompositeSuffixFakeDB:
    """Full tuple overlap only for the correct role-named pair (SOURCE_ORDER_ID, RETURN_ITEM_NO);
    every other combination and any single-column query gets zero overlap."""

    def query(self, sql: str, binds=None):
        if "AS C1" in sql:  # composite tuple-overlap query
            correct = (
                'TO_CHAR("SOURCE_ORDER_ID")' in sql and 'TO_CHAR("RETURN_ITEM_NO")' in sql
            )
            matched = 3 if correct else 0
            return [{"DISTINCT_TOTAL": 3, "MATCHED": matched, "ORPHAN_ROWS": 3 - matched}]
        return [{"DISTINCT_TOTAL": 0, "MATCHED": 0, "ORPHAN_ROWS": 0}]


def test_composite_fk_inferred_by_suffix_and_data():
    schema = _composite_suffix_schema()
    relationships, _ = run_checks(schema, db=_CompositeSuffixFakeDB(), owner="CFK")
    inferred = {
        (r.from_table, tuple(r.from_columns), r.to_table) for r in relationships if not r.declared
    }
    # Rediscovered despite no exact column-name overlap — data picks the right role-named pair.
    assert ("RETURN_LINES", ("SOURCE_ORDER_ID", "RETURN_ITEM_NO"), "ORDER_ITEMS") in inferred
    rel = next(
        r for r in relationships
        if r.from_table == "RETURN_LINES" and not r.declared and len(r.from_columns) == 2
    )
    assert rel.to_columns == ["ORDER_ID", "ITEM_NO"]  # parent key order preserved
    assert rel.confidence == ConfidenceLevel.HIGH
    assert any("name suffix" in e for e in rel.evidence)


def test_composite_suffix_fk_not_inferred_offline():
    """Like the single-column suffix pass, composite suffix matching must NOT fire without a DB."""
    schema = _composite_suffix_schema()
    relationships, _ = run_checks(schema)  # no db
    inferred = {tuple(r.from_columns) for r in relationships if not r.declared}
    assert ("SOURCE_ORDER_ID", "RETURN_ITEM_NO") not in inferred


# --------------------------------------------------------- cross-schema (multi-owner) FK inference


def _owned(name: str, owner: str, cols, key_cols) -> TableInfo:
    return TableInfo(
        name=name,
        owner=owner,
        columns=cols,
        constraints=[
            ConstraintInfo(name=f"PK_{name}", type=ConstraintType.PRIMARY_KEY, columns=key_cols)
        ],
    )


def _cross_schema() -> SchemaInfo:
    """HR owns EMPLOYEES/LOCATIONS; a separate EXT schema's SALES_CONTACTS points into both —
    LOCATION_ID by exact name, REP_ID by '_ID' suffix. The merged schema carries owner per table."""
    employees = _owned("EMPLOYEES", "HR", [_num("EMPLOYEE_ID", 6)], ["EMPLOYEE_ID"])
    locations = _owned("LOCATIONS", "HR", [_num("LOCATION_ID", 4)], ["LOCATION_ID"])
    contacts = _owned(
        "SALES_CONTACTS", "EXT",
        [_num("CONTACT_ID", 8), _num("REP_ID", 6), _num("LOCATION_ID", 4)],
        ["CONTACT_ID"],
    )
    return SchemaInfo(name="HR+EXT", tables=[employees, locations, contacts])


class _CrossFakeDB:
    """Precise single-column overlap fake: matches the child column via its TO_CHAR("col")
    projection so a parent column of the same name elsewhere can't be confused for the child."""

    def __init__(self, overlaps: dict[tuple[str, str], float]):
        self._overlaps = overlaps

    def query(self, sql: str, binds=None):
        for (child, parent), f in self._overlaps.items():
            if f'TO_CHAR("{child}")' in sql and f'p."{parent}"' in sql:
                total = 100
                m = round(f * total)
                return [{"DISTINCT_TOTAL": total, "MATCHED": m, "ORPHAN_ROWS": total - m}]
        return [{"DISTINCT_TOTAL": 0, "MATCHED": 0, "ORPHAN_ROWS": 0}]


def test_cross_schema_fks_inferred_when_both_schemas_in_scope():
    schema = _cross_schema()
    db = _CrossFakeDB(
        {
            ("LOCATION_ID", "LOCATION_ID"): 1.0,   # exact-name, into HR.LOCATIONS
            ("REP_ID", "EMPLOYEE_ID"): 1.0,        # suffix, into HR.EMPLOYEES
            ("REP_ID", "LOCATION_ID"): 0.1,        # decoy: same '_ID' suffix, data disagrees
        }
    )
    relationships, _ = run_checks(schema, db=db, owner=None)
    inferred = {
        (r.from_table, tuple(r.from_columns), r.to_owner, r.to_table)
        for r in relationships if not r.declared
    }
    assert ("SALES_CONTACTS", ("LOCATION_ID",), "HR", "LOCATIONS") in inferred
    assert ("SALES_CONTACTS", ("REP_ID",), "HR", "EMPLOYEES") in inferred

    rep = next(r for r in relationships if r.from_columns == ["REP_ID"])
    assert rep.cross_schema and rep.from_owner == "EXT" and rep.to_owner == "HR"
    assert any("cross-schema" in e for e in rep.evidence)
