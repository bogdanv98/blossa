"""Web API endpoints (/api/map, /api/ask, /api/run), exercised with FastAPI's TestClient.

A fake provider and a fake DB are injected so the endpoints run without a live model or Oracle.
"""

import pytest
from fastapi.testclient import TestClient

from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.models import ConfidenceLevel, LogColumn, LogKind, LogRole, LogTable
from blossa.pipeline import run_scan_over_schema
from blossa.web.server import build_map_view, create_app


def _report():
    settings = Settings()
    settings.llm.provider = "heuristic"
    return run_scan_over_schema(
        build_demo_schema(), settings, HeuristicProvider(), db=None, owner=None
    )


class _FakeProvider:
    name = "ollama"
    model = "fake"

    def generate(self, system_prompt, user_prompt):
        return (
            '{"sql":"SELECT COUNT(*) AS N FROM CUSTOMERS","explanation":"counts customers",'
            '"assumptions":["all rows"],"confidence":"high"}'
        )


class _FakeDB:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, sql, binds=None):
        return [{"N": 4}]


def _client(provider=None, *, heuristic=False):
    settings = Settings()
    settings.llm.provider = "heuristic" if heuristic else "ollama"
    app = create_app(
        settings,
        _report(),
        provider=provider or _FakeProvider(),
        db_factory=lambda: _FakeDB(),
    )
    return TestClient(app)


def test_map_endpoint_returns_tables_with_meanings():
    data = _client().get("/api/map").json()
    assert data["schema_name"] == "BLOSSA_DEMO"
    names = {t["name"] for t in data["tables"]}
    assert "CUSTOMERS" in names
    customers = next(t for t in data["tables"] if t["name"] == "CUSTOMERS")
    email = next(c for c in customers["columns"] if c["name"] == "EMAIL")
    assert email["type"] and email["meaning"]  # computed type + inferred meaning present


def test_ask_endpoint_returns_sql_proposal():
    r = _client().post("/api/ask", json={"question": "how many customers?"})
    assert r.status_code == 200
    data = r.json()
    assert data["sql"].startswith("SELECT COUNT(*)")
    assert data["confidence"] == "high"


def test_ask_endpoint_forwards_history_for_followups():
    # A follow-up sends prior turns; the server must weave them into the model prompt so the model
    # can refine the last query. The fake provider records the prompt it was given.
    class _Recorder:
        name = "ollama"
        model = "fake"

        def __init__(self):
            self.last_prompt = ""

        def generate(self, system_prompt, user_prompt):
            self.last_prompt = user_prompt
            return '{"sql":"SELECT COUNT(*) FROM CUSTOMERS","confidence":"high"}'

    rec = _Recorder()
    client = _client(provider=rec)
    r = client.post(
        "/api/ask",
        json={
            "question": "now break it down by country",
            "history": [
                {"question": "how many customers?", "sql": "SELECT COUNT(*) FROM CUSTOMERS"}
            ],
        },
    )
    assert r.status_code == 200
    assert "Conversation so far" in rec.last_prompt
    assert "how many customers?" in rec.last_prompt


def test_ask_rejects_heuristic_provider():
    r = _client(heuristic=True).post("/api/ask", json={"question": "how many?"})
    assert r.status_code == 400
    assert "model provider" in r.json()["detail"]


def test_run_endpoint_executes_and_returns_rows():
    r = _client().post("/api/run", json={"sql": "SELECT COUNT(*) AS N FROM CUSTOMERS"})
    assert r.status_code == 200
    data = r.json()
    assert data["columns"] == ["N"] and data["rows"] == [[4]]


@pytest.mark.parametrize("bad", ["DROP TABLE customers", "DELETE FROM customers", "  "])
def test_run_endpoint_rejects_unsafe_sql(bad):
    r = _client().post("/api/run", json={"sql": bad})
    assert r.status_code == 400


def test_map_view_includes_log_tables_with_roles():
    report = _report()
    report.log_tables.append(
        LogTable(
            table="ERROR_LOG",
            kind=LogKind.ERROR,
            confidence=ConfidenceLevel.HIGH,
            columns=[
                LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
                LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
            ],
        )
    )
    view = build_map_view(report)
    assert view["log_tables"], "the Logs tab needs log tables in the map view"
    lt = view["log_tables"][0]
    assert lt["name"] == "ERROR_LOG" and lt["kind"] == "error"
    assert {c["role"] for c in lt["columns"]} == {"event_time", "message"}


def test_map_view_lists_program_objects_with_source():
    # The SQL-workspace object browser needs every view/package/proc — sourced from program_units
    # (so they appear even without a model) — each with its source, plus the AI summary joined in.
    from blossa.models import ProgramKind, ProgramSemantics, ProgramUnit

    report = _report()
    report.schema_info.program_units.append(
        ProgramUnit(name="EMP_V", owner="HR", kind=ProgramKind.VIEW,
                    source="SELECT * FROM EMPLOYEES")
    )
    report.schema_info.program_units.append(
        ProgramUnit(name="RAW_PKG", owner="HR", kind=ProgramKind.PACKAGE,
                    source="PACKAGE RAW_PKG AS ... END;")
    )
    report.program_semantics.append(
        ProgramSemantics(name="EMP_V", owner="HR", kind=ProgramKind.VIEW,
                         summary="Employee view", tables_used=["EMPLOYEES"],
                         confidence=ConfidenceLevel.HIGH)
    )
    view = build_map_view(report)
    progs = {p["name"]: p for p in view["programs"]}
    assert progs["EMP_V"]["kind"] == "VIEW"
    assert progs["EMP_V"]["source"] == "SELECT * FROM EMPLOYEES"
    assert progs["EMP_V"]["summary"] == "Employee view"  # semantics joined in
    # A unit with no semantics still shows up (empty summary), so the object exists in the tree.
    assert progs["RAW_PKG"]["source"].startswith("PACKAGE") and progs["RAW_PKG"]["summary"] == ""


def test_map_view_lists_other_catalog_objects():
    # Sequences/synonyms/etc. have no model of their own — they reach the browser through the
    # object catalog. Tables and program units are not repeated there.
    from blossa.models import CatalogObject

    report = _report()
    report.schema_info.objects.extend([
        CatalogObject(name="ORDER_SEQ", owner="BLOSSA_DEMO", type="SEQUENCE", status="VALID"),
        CatalogObject(name="CUSTOMERS", owner="BLOSSA_DEMO", type="TABLE", status="VALID"),
    ])
    view = build_map_view(report)
    types = {o["type"] for o in view["other_objects"]}
    assert types == {"SEQUENCE"}  # the TABLE already has a rich entry
    assert view["other_objects"][0]["name"] == "ORDER_SEQ"


def test_map_view_lists_a_packages_routines_with_their_kind():
    # PROCEDURES/FUNCTIONS read 0 in the tree while a package holds a dozen of them: they are not
    # catalog objects, so the package's own entry is the only place they can be discovered.
    from blossa.models import ProgramKind, ProgramUnit

    report = _report()
    report.schema_info.program_units.append(
        ProgramUnit(
            name="CORE_BANKING", owner="BANKDEMO", kind=ProgramKind.PACKAGE,
            source=(
                "PACKAGE core_banking AS\n"
                "  FUNCTION get_balance(p_id NUMBER) RETURN NUMBER;\n"
                "  PROCEDURE deposit(p_id NUMBER);\n"
                "END;PACKAGE BODY core_banking AS\n"
                "  FUNCTION money(p NUMBER) RETURN NUMBER IS BEGIN RETURN p; END;\nEND;"
            ),
        )
    )
    pkg = next(p for p in build_map_view(report)["programs"] if p["name"].endswith("CORE_BANKING"))
    assert pkg["subprograms"] == [
        {"name": "GET_BALANCE", "kind": "FUNCTION"},
        {"name": "DEPOSIT", "kind": "PROCEDURE"},
    ]  # body-private money() is not callable, so it is not advertised


def test_map_view_marks_invalid_program_units():
    from blossa.models import CatalogObject, ProgramKind, ProgramUnit

    report = _report()
    report.schema_info.program_units.append(
        ProgramUnit(name="BROKEN_PKG", owner="HR", kind=ProgramKind.PACKAGE, source="PACKAGE ...")
    )
    report.schema_info.objects.append(
        CatalogObject(name="BROKEN_PKG", owner="HR", type="PACKAGE", status="INVALID")
    )
    progs = {p["name"]: p for p in build_map_view(report)["programs"]}
    assert progs["BROKEN_PKG"]["status"] == "INVALID"


class _FakeLob:
    """Mimics an oracledb LOB locator: readable only while its connection is still open."""

    def __init__(self, text, db):
        self._text = text
        self._db = db

    def read(self):
        if self._db.closed:
            raise RuntimeError("DPY-1001: not connected to database")
        return self._text


class _DdlDB(_FakeDB):
    """GET_DDL returns a CLOB — the endpoint must read it before leaving the with-block."""

    closed = False

    def __enter__(self):
        self.closed = False
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def query(self, sql, binds=None):
        if "GET_DDL" in sql.upper():
            text = f"\n  CREATE TABLE {binds['owner']}.{binds['name']} (X NUMBER)\n"
            return [{"DDL": _FakeLob(text, self)}]
        return super().query(sql, binds)


def test_ddl_endpoint_returns_oracles_own_ddl():
    settings = Settings()
    settings.llm.provider = "ollama"
    app = create_app(settings, _report(), provider=_FakeProvider(), db_factory=lambda: _DdlDB())
    r = TestClient(app).post("/api/ddl", json={"name": "HR.CUSTOMERS", "type": "TABLE"})
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "database"
    assert data["ddl"] == "CREATE TABLE HR.CUSTOMERS (X NUMBER)"


def test_ddl_endpoint_falls_back_to_the_scanned_map():
    # No DBMS_METADATA (no privilege, or no live DB): the workspace still shows a CREATE TABLE
    # rebuilt from what the scan captured, clearly labelled as coming from the scan.
    class _NoMetadata(_FakeDB):
        def query(self, sql, binds=None):
            raise RuntimeError("ORA-31603: object not found")

    settings = Settings()
    settings.llm.provider = "ollama"
    app = create_app(settings, _report(), provider=_FakeProvider(),
                     db_factory=lambda: _NoMetadata())
    data = TestClient(app).post("/api/ddl", json={"name": "CUSTOMERS", "type": "TABLE"}).json()
    assert data["source"] == "scan"
    assert "CREATE TABLE CUSTOMERS (" in data["ddl"]
    assert "EMAIL" in data["ddl"]


def test_ddl_endpoint_rejects_a_non_identifier_name():
    r = _client().post("/api/ddl", json={"name": "CUSTOMERS; DROP TABLE X", "type": "TABLE"})
    assert r.status_code == 400


def test_ddl_endpoint_404s_when_nothing_is_known():
    r = _client().post("/api/ddl", json={"name": "NO_SUCH_THING", "type": "SEQUENCE"})
    assert r.status_code == 404


class _CodeProvider:
    """Answers with a code proposal, and records the system prompt it was handed."""

    name = "ollama"
    model = "fake"

    def __init__(self):
        self.system_prompts = []

    def generate(self, system_prompt, user_prompt):
        self.system_prompts.append(system_prompt)
        return (
            '{"code": "CREATE OR REPLACE PROCEDURE active_balance IS BEGIN NULL; END;",'
            ' "object_type": "PROCEDURE", "object_name": "ACTIVE_BALANCE",'
            ' "explanation": "returns the balance of active accounts only",'
            ' "assumptions": ["status ACTIVE means open"], "confidence": "high"}'
        )


def test_ask_returns_code_for_a_build_request():
    provider = _CodeProvider()
    r = _client(provider).post(
        "/api/ask", json={"question": "write me a procedure like get_balance for active accounts"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "code"
    assert data["code"].startswith("CREATE OR REPLACE PROCEDURE")
    assert data["object_name"] == "ACTIVE_BALANCE"
    assert "sql" not in data  # nothing here is runnable, so no SQL field to hand to /api/run
    assert "code" in provider.system_prompts[0].lower()  # the codegen prompt, not the ask one


def test_ask_still_returns_sql_for_a_question():
    data = _client().post("/api/ask", json={"question": "how many customers?"}).json()
    assert data["kind"] == "sql"
    assert data["sql"].startswith("SELECT COUNT(*)")


def test_generated_ddl_is_still_refused_by_the_run_endpoint():
    # The safety property: even if generated code were posted to /api/run, it cannot execute.
    r = _client().post(
        "/api/run",
        json={"sql": "CREATE OR REPLACE PROCEDURE active_balance IS BEGIN NULL; END;"},
    )
    assert r.status_code == 400


def _log_report():
    report = _report()
    report.log_tables.append(
        LogTable(
            table="ERROR_LOG",
            kind=LogKind.ERROR,
            confidence=ConfidenceLevel.HIGH,
            columns=[
                LogColumn(column="LOG_TIME", role=LogRole.EVENT_TIME),
                LogColumn(column="SEVERITY", role=LogRole.SEVERITY),
                LogColumn(column="MODULE", role=LogRole.SOURCE),
                LogColumn(column="MESSAGE", role=LogRole.MESSAGE),
            ],
        )
    )
    return report


def test_explain_log_refuses_remote_provider():
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    app = create_app(
        settings, _log_report(), provider=_FakeProvider(), db_factory=lambda: _FakeDB()
    )
    r = TestClient(app).post("/api/logs/explain", json={})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "remote" in detail or "LOCAL" in detail


def test_explain_log_clusters_with_local_provider():
    class _ClusterProvider:
        name = "ollama"
        model = "m"

        def generate(self, system, user):
            return (
                '{"clusters":[{"cause":"Gateway timeout","count":2,"severity":"ERROR",'
                '"suggested_action":"Add retry","example":"timeout"}]}'
            )

    settings = Settings()
    settings.llm.provider = "ollama"
    app = create_app(
        settings, _log_report(), provider=_ClusterProvider(), db_factory=lambda: _FakeDB()
    )
    r = TestClient(app).post("/api/logs/explain", json={"table": "ERROR_LOG"})
    assert r.status_code == 200
    data = r.json()
    assert data["clusters"][0]["cause"] == "Gateway timeout"


class _SpikeDB:
    """A DB whose counts show a steady baseline plus one burst hour, for the spikes endpoint."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, sql, binds=None):
        if "AS SOURCE" in sql:
            return [{"BUCKET": "2026-06-29 00:00", "SOURCE": "PAYMENT_GATEWAY.CHARGE",
                     "ENTRIES": 18}]
        rows = [{"BUCKET": f"2026-06-29 {h:02d}:00", "ENTRIES": 1} for h in range(1, 6)]
        rows.append({"BUCKET": "2026-06-29 00:00", "ENTRIES": 19})
        return rows


def test_log_spikes_endpoint_flags_burst_and_source():
    # Deterministic — only aggregate counts, so it runs even on the heuristic provider.
    settings = Settings()
    settings.llm.provider = "heuristic"
    app = create_app(
        settings, _log_report(), provider=_FakeProvider(), db_factory=lambda: _SpikeDB()
    )
    r = TestClient(app).post("/api/logs/spikes", json={"table": "ERROR_LOG", "grain": "hour"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["baseline"] == 1.0
    assert [s["bucket"] for s in data["spikes"]] == ["2026-06-29 00:00"]
    assert data["onsets"][0]["source"] == "PAYMENT_GATEWAY.CHARGE"


def test_log_spikes_endpoint_rejects_bad_grain():
    settings = Settings()
    app = create_app(
        settings, _log_report(), provider=_FakeProvider(), db_factory=lambda: _SpikeDB()
    )
    r = TestClient(app).post("/api/logs/spikes", json={"table": "ERROR_LOG", "grain": "week"})
    assert r.status_code == 400


def test_index_page_served():
    r = _client().get("/")
    assert r.status_code == 200
    assert "Blossa" in r.text


def test_index_page_has_sql_workspace():
    # Milestone 1: the SQL workspace (object tree + editor + result grid) is the default surface.
    html = _client().get("/").text
    assert 'data-tab="sql"' in html  # the workspace tab exists
    assert 'id="ws-sql"' in html  # the SQL editor textarea
    assert 'id="ws-tree"' in html  # the object browser tree


def test_static_app_js_served():
    r = _client().get("/static/app.js")
    assert r.status_code == 200
    assert "renderGrid" in r.text  # shared sortable/exportable result grid
    assert "other_objects" in r.text  # sequences/synonyms/… folders in the object tree
    assert "/api/ddl" in r.text  # the DDL viewer


def test_object_tree_lists_every_category_it_supports():
    # A schema with no views must still show a "Views 0" folder: a missing folder reads as
    # "this tool cannot show views", which is a different (and wrong) message.
    js = _client().get("/static/app.js").text
    for label in ("Tables", "Views", "Packages", "Procedures", "Functions", "Triggers",
                  "Materialized views", "Sequences", "Synonyms", "Types", "Indexes"):
        assert f'label: "{label}"' in js
    assert "empty ones included" in js  # the rule is stated where it is implemented
