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


def test_init_writes_config(tmp_path):
    cfg = tmp_path / "blossa.local.yml"
    # Answers: DSN (default), user (default), schema (blank), store-pw? n, provider heuristic.
    answers = "\n\n\nn\nheuristic\n"
    result = runner.invoke(app, ["init", "--output", str(cfg)], input=answers)
    assert result.exit_code == 0, result.stdout
    assert cfg.exists()

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["llm"]["provider"] == "heuristic"
    assert "password" not in data["oracle"]  # we declined storing it


def test_init_refuses_overwrite_without_force(tmp_path):
    cfg = tmp_path / "blossa.local.yml"
    cfg.write_text("oracle: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--output", str(cfg)], input="\n\n\nn\nheuristic\n")
    assert result.exit_code == 1
    assert cfg.read_text(encoding="utf-8") == "oracle: {}\n"  # left untouched


def test_check_llm_heuristic_ok():
    result = runner.invoke(app, ["check-llm", "--llm-provider", "heuristic"])
    assert result.exit_code == 0
    assert "heuristic" in result.stdout
