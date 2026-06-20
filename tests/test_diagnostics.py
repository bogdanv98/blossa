from blossa.config import Settings
from blossa.diagnostics import (
    Status,
    check_config,
    check_llm,
    check_oracledb,
    check_output,
    check_python,
    llm_remediation,
    run_diagnostics,
)


def _settings_offline(tmp_path):
    s = Settings()
    s.llm.provider = "heuristic"
    s.output.dir = str(tmp_path / "out")
    # Guarantee the Oracle check fails fast (connection refused on an unused port).
    s.oracle.dsn = "localhost:1/NOPE"
    return s


def test_check_python_ok():
    assert check_python().status == Status.OK


def test_check_oracledb_installed():
    assert check_oracledb().status == Status.OK


def test_check_config_missing_path_fails():
    assert check_config("does-not-exist.yml").status == Status.FAIL


def test_check_llm_heuristic_ok():
    s = Settings()
    s.llm.provider = "heuristic"
    assert check_llm(s).status == Status.OK


def test_check_llm_ollama_unreachable_fails_with_hint():
    s = Settings()
    s.llm.provider = "ollama"
    s.llm.ollama.base_url = "http://localhost:1"  # nothing listens here
    result = check_llm(s)
    assert result.status == Status.FAIL
    assert "ollama pull" in result.hint


def test_check_output_writable(tmp_path):
    s = Settings()
    s.output.dir = str(tmp_path / "out")
    assert check_output(s).status == Status.OK


def test_run_diagnostics_flags_failures(tmp_path):
    diag = run_diagnostics(_settings_offline(tmp_path), config_path=None)
    assert diag.has_failures  # Oracle is unreachable in this setup
    names = {r.name for r in diag.results}
    assert {"Python", "oracledb driver", "Oracle connection", "LLM provider"} <= names


def test_llm_remediation_is_actionable(tmp_path):
    s = _settings_offline(tmp_path)
    s.llm.provider = "ollama"
    s.llm.ollama.base_url = "http://localhost:1"
    text = llm_remediation(s)
    assert "ollama" in text.lower()
