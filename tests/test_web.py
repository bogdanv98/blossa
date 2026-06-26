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


def test_index_page_served():
    r = _client().get("/")
    assert r.status_code == 200
    assert "Blossa" in r.text
