"""Web API endpoints (/api/map, /api/ask, /api/run), exercised with FastAPI's TestClient.

A fake provider and a fake DB are injected so the endpoints run without a live model or Oracle.
"""

import pytest
from fastapi.testclient import TestClient

from blossa.config import Settings
from blossa.demo import build_demo_schema
from blossa.llm.heuristic import HeuristicProvider
from blossa.pipeline import run_scan_over_schema
from blossa.web.server import create_app


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


def test_index_page_served():
    r = _client().get("/")
    assert r.status_code == 200
    assert "Blossa" in r.text
