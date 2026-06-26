import yaml
from typer.testing import CliRunner

from blossa.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Blossa" in result.stdout


def test_scan_demo_writes_artifacts(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["scan", "--demo", "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert (out / "database_map.md").exists()
    assert (out / "database_map.json").exists()


def test_init_writes_config_and_grant_script(tmp_path):
    cfg = tmp_path / "blossa.local.yml"
    # Answers: DSN (default), account (default), profile (default scoped), schemas, store-pw? n,
    # provider heuristic.
    answers = "\n\n\nHR\nn\nheuristic\n"
    result = runner.invoke(app, ["init", "--output", str(cfg)], input=answers)
    assert result.exit_code == 0, result.output
    assert cfg.exists()

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["llm"]["provider"] == "heuristic"
    assert data["oracle"]["catalog_scope"] == "scoped"
    assert data["oracle"]["schema"] == "HR"
    assert data["oracle"]["user"] == "BLOSSA_ASSISTANT"
    assert "password" not in data["oracle"]  # we declined storing it

    grants = tmp_path / "blossa_grants.sql"
    assert grants.exists()
    sql = grants.read_text(encoding="utf-8")
    assert "CREATE USER BLOSSA_ASSISTANT" in sql
    assert "GRANT READ" in sql and "'HR'" in sql  # scoped grant for HR


def test_init_full_profile_grants_catalog_role(tmp_path):
    cfg = tmp_path / "blossa.local.yml"
    # DSN, account, profile=full, scan-schema (blank → "*"), store-pw n, provider heuristic.
    answers = "\n\nfull\n\nn\nheuristic\n"
    result = runner.invoke(app, ["init", "--output", str(cfg)], input=answers)
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["oracle"]["catalog_scope"] == "full"
    assert data["oracle"]["schema"] == "*"
    sql = (tmp_path / "blossa_grants.sql").read_text(encoding="utf-8")
    assert "READ ANY TABLE" in sql and "SELECT_CATALOG_ROLE" in sql


def test_init_refuses_overwrite_without_force(tmp_path):
    cfg = tmp_path / "blossa.local.yml"
    cfg.write_text("oracle: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--output", str(cfg)], input="\n\n\nHR\nn\nheuristic\n")
    assert result.exit_code == 1
    assert cfg.read_text(encoding="utf-8") == "oracle: {}\n"  # left untouched


def test_check_llm_heuristic_ok():
    result = runner.invoke(app, ["check-llm", "--llm-provider", "heuristic"])
    assert result.exit_code == 0
    assert "heuristic" in result.stdout


def _demo_map(tmp_path):
    out = tmp_path / "out"
    runner.invoke(app, ["scan", "--demo", "--out", str(out)])
    return out / "database_map.json"


def test_ask_rejects_heuristic_provider(tmp_path):
    mp = _demo_map(tmp_path)
    result = runner.invoke(
        app, ["ask", "how many customers?", "--map", str(mp), "--llm-provider", "heuristic"]
    )
    assert result.exit_code == 1
    assert "model provider" in result.output


def test_ask_dry_run_shows_sql_without_executing(tmp_path, monkeypatch):
    mp = _demo_map(tmp_path)

    class _FakeProvider:
        name = "ollama"
        model = "fake"

        def generate(self, system_prompt, user_prompt):
            return (
                '{"sql":"SELECT COUNT(*) FROM CUSTOMERS","explanation":"counts customers",'
                '"assumptions":["counts all rows"],"confidence":"high"}'
            )

    import blossa.cli as climod

    monkeypatch.setattr(climod, "get_provider", lambda cfg: _FakeProvider())
    monkeypatch.setattr(climod, "_preflight_llm", lambda settings: None)

    result = runner.invoke(
        app,
        ["ask", "how many customers?", "--map", str(mp), "--llm-provider", "ollama", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "COUNT(*) FROM CUSTOMERS" in result.output
    assert "not executed" in result.output


def test_ask_interactive_refines_with_history(tmp_path, monkeypatch):
    mp = _demo_map(tmp_path)

    class _RecordingProvider:
        name = "ollama"
        model = "fake"

        def __init__(self):
            self.prompts = []

        def generate(self, system_prompt, user_prompt):
            self.prompts.append(user_prompt)
            return '{"sql":"SELECT COUNT(*) FROM CUSTOMERS","confidence":"high"}'

    provider = _RecordingProvider()
    import blossa.cli as climod

    monkeypatch.setattr(climod, "get_provider", lambda cfg: provider)
    monkeypatch.setattr(climod, "_preflight_llm", lambda settings: None)

    # No question argument → interactive. Two turns, then a blank line to quit. --dry-run keeps it
    # off the database while still recording the model's SQL into the conversation history.
    result = runner.invoke(
        app,
        ["ask", "--map", str(mp), "--llm-provider", "ollama", "--dry-run"],
        input="how many customers?\nnow break it down by country\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Interactive ask" in result.output
    # Two questions → two model calls; the second must carry the first turn as history.
    assert len(provider.prompts) == 2
    assert "Conversation so far" not in provider.prompts[0]
    assert "Conversation so far" in provider.prompts[1]
    assert "how many customers?" in provider.prompts[1]


def _applog_map(tmp_path):
    """A scan JSON map carrying one detected error-log table, for the `logs` command."""
    from datetime import UTC, datetime

    from blossa.models import (
        ColumnInfo,
        ConstraintInfo,
        ConstraintType,
        ScanMetadata,
        ScanReport,
        SchemaInfo,
        TableInfo,
    )

    table = TableInfo(
        name="ERROR_LOG",
        owner="APPLOG",
        columns=[
            ColumnInfo(name="ERROR_ID", data_type="NUMBER"),
            ColumnInfo(name="LOG_TIME", data_type="TIMESTAMP"),
            ColumnInfo(name="SEVERITY", data_type="VARCHAR2", data_length=10),
            ColumnInfo(name="MODULE", data_type="VARCHAR2", data_length=80),
            ColumnInfo(name="MESSAGE", data_type="VARCHAR2", data_length=2000),
        ],
        constraints=[
            ConstraintInfo(name="pk", type=ConstraintType.PRIMARY_KEY, columns=["ERROR_ID"])
        ],
    )
    from blossa.logs import detect_log_tables

    schema = SchemaInfo(name="APPLOG", tables=[table])
    report = ScanReport(
        metadata=ScanMetadata(
            blossa_version="0", schema_name="APPLOG",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            llm_provider="ollama", table_count=1,
        ),
        schema_info=schema,
        log_tables=detect_log_tables(schema),
    )
    mp = tmp_path / "applog_map.json"
    mp.write_text(report.model_dump_json(), encoding="utf-8")
    return mp


def test_logs_explain_refuses_remote_provider(tmp_path):
    # Root-cause --explain reads real error text, so it must refuse a remote model and never touch
    # the database (the refusal comes before any query).
    mp = _applog_map(tmp_path)
    result = runner.invoke(
        app, ["logs", "--map", str(mp), "--llm-provider", "openai_compatible", "--explain"]
    )
    assert result.exit_code == 1
    assert "remote model" in result.output or "LOCAL model" in result.output


def test_logs_explain_refuses_heuristic(tmp_path):
    mp = _applog_map(tmp_path)
    result = runner.invoke(
        app, ["logs", "--map", str(mp), "--llm-provider", "heuristic", "--explain"]
    )
    assert result.exit_code == 1
    assert "model provider" in result.output


def test_ask_without_map_errors(tmp_path):
    missing = tmp_path / "nope.json"
    result = runner.invoke(
        app, ["ask", "anything", "--map", str(missing), "--llm-provider", "ollama"]
    )
    assert result.exit_code == 1
    assert "No database map" in result.output
