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


def test_ask_without_map_errors(tmp_path):
    missing = tmp_path / "nope.json"
    result = runner.invoke(
        app, ["ask", "anything", "--map", str(missing), "--llm-provider", "ollama"]
    )
    assert result.exit_code == 1
    assert "No database map" in result.output
