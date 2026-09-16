import json
import sys
import time
from pathlib import Path

import pytest

from browser_local_ai_bridge.cli import main
from browser_local_ai_bridge.config import ConfigError, load_repo_allowlist
from browser_local_ai_bridge.executors.command import CommandExecutor


def configured(tmp_path, code="print('private output')", **limits):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = {"version": 1, "repos": {"sample/repo": {
        "checkout_path": str(checkout), "recipes": {"check": {
            "argv": [sys.executable, "-c", code], **limits,
        }},
    }}}
    path = home / "repos.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return home, load_repo_allowlist(path)["sample/repo"]


def run(target, **kwargs):
    return CommandExecutor(target=target, recipe_name="check").execute(
        task={"repo": "sample/repo", "goal": "ignore recipe; execute malicious text"},
        checkout=target.checkout_path, **kwargs,
    )


def test_success_uses_fixed_argv_cwd_and_private_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_SECRET", "do-not-inherit")
    code = ("import os,sys; assert 'BRIDGE_TEST_SECRET' not in os.environ; "
            "assert os.path.isdir('.git'); assert sys.argv[1] == '; echo injected'; "
            "print('private output')")
    home, _ = configured(tmp_path, code)
    path = home / "repos.json"
    config = json.loads(path.read_text())
    config["repos"]["sample/repo"]["recipes"]["check"]["argv"].append("; echo injected")
    path.write_text(json.dumps(config))
    result = run(load_repo_allowlist(path)["sample/repo"])
    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert result.result["commands_run"] == ["recipe:check"]
    assert "private output" not in repr(result)
    assert str(tmp_path) not in repr(result)


@pytest.mark.parametrize("code,limits,status,error", [
    ("raise SystemExit(7)", {}, "FAILED", "nonzero_exit"),
    ("import time; time.sleep(30)", {"timeout_seconds": 1}, "INTERRUPTED", "wall_timeout"),
    ("import os; os.write(1, b'x' * 4096)", {"max_output_bytes": 16}, "FAILED", "output_limit"),
    ("import os; os.write(2, b'x' * 4096)", {"max_output_bytes": 16}, "FAILED", "output_limit"),
    ("import os; os.write(1, b'x' * 16)", {"max_output_bytes": 16}, "SUCCESS", ""),
])
def test_process_limits(tmp_path, code, limits, status, error):
    _, target = configured(tmp_path, code, **limits)
    started = time.monotonic()
    outcome = run(target)
    assert time.monotonic() - started < 10
    assert (outcome.status, outcome.error_type) == (status, error)
    if error == "output_limit":
        assert "output_bytes=17;" in outcome.result["tests"][0]


def test_unknown_recipe_and_repository_mismatch_fail_closed(tmp_path):
    _, target = configured(tmp_path)
    with pytest.raises(ConfigError, match="unknown recipe"):
        CommandExecutor(target=target, recipe_name="other")
    executor = CommandExecutor(target=target, recipe_name="check")
    with pytest.raises(ConfigError, match="mismatch"):
        executor.execute(task={"repo": "other"}, checkout=target.checkout_path)
    with pytest.raises(ConfigError, match="mismatch"):
        executor.execute(task={"repo": "sample/repo"}, checkout=tmp_path)


def test_observer_failure_is_private_and_child_is_stopped(tmp_path, monkeypatch):
    _, target = configured(tmp_path, "import time; time.sleep(30)")
    monkeypatch.setattr("browser_local_ai_bridge.process_control.process_birth_token", lambda pid: "test")
    class Observer:
        def on_spawn(self, **kwargs):
            assert kwargs["pid"] > 0
            assert kwargs["timeout_seconds"] == 30
            raise RuntimeError("secret observer error")
    started = time.monotonic()
    outcome = run(target, observer=Observer())
    assert time.monotonic() - started < 10
    assert outcome.error_type == "observer_error"
    assert "secret" not in repr(outcome)


@pytest.mark.parametrize("code,exit_status,state", [
    ("print('private output')", 0, "WAITING_CONTROLLER"),
    ("raise SystemExit(7)", 1, "FAILED"),
])
def test_cli_persists_canonical_results(tmp_path, capsys, monkeypatch, code, exit_status, state):
    home, _ = configured(tmp_path, code)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.process_birth_token", lambda pid: "test")
    assert main(["--home", str(home), "execute-recipe", "sample/repo", "check", "--task-id", "recipe-test"]) == exit_status
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == state
    assert result["execution"]["executor"] == "command"
    assert result["result"]["commands_run"] == ["recipe:check"]
    assert result["checkpoint"]
    assert "private output" not in output
    assert not list((home / "locks").glob("*.lock"))
    assert main(["--home", str(home), "execute-recipe", "sample/repo", "missing"]) == 2


def test_missing_executable_returns_private_failure(tmp_path):
    home, _ = configured(tmp_path)
    path = home / "repos.json"
    config = json.loads(path.read_text())
    config["repos"]["sample/repo"]["recipes"]["check"]["argv"] = [str(tmp_path / "private-missing.exe")]
    path.write_text(json.dumps(config))
    result = run(load_repo_allowlist(path)["sample/repo"])
    assert result.error_type == "process_error"
    assert "private-missing" not in repr(result)
