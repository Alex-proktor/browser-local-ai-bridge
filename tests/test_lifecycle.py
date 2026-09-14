from pathlib import Path

from browser_local_ai_bridge.lifecycle import (
    WorkerIdentity,
    _write_identity,
    autostart_status,
    disable_autostart,
    enable_autostart,
    lifecycle_state_path,
    start_worker,
    stop_worker,
    worker_health,
)
from browser_local_ai_bridge.worker import MailboxSource, WorkerConfig


def _config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        sources=(MailboxSource("sample/mailbox", 1),),
        repos_path=tmp_path / "repos.json",
        db_path=tmp_path / "runtime" / "state.db",
        tasks_root=tmp_path / "runtime" / "tasks",
        lock_root=tmp_path / "runtime" / "locks",
    )


class FakeProcess:
    def __init__(self, pid=123):
        self.pid = pid
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_start_is_single_instance(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    first = start_worker(tmp_path / "worker.json", config, popen_factory=popen, birth_reader=lambda pid: "birth")
    monkeypatch.setattr("browser_local_ai_bridge.lifecycle.process_control.process_alive", lambda pid, token: True)
    second = start_worker(tmp_path / "worker.json", config, popen_factory=popen, birth_reader=lambda pid: "birth")

    assert first["status"] == "RUNNING"
    assert second["already_running"] is True
    assert len(calls) == 1
    assert lifecycle_state_path(config).exists()


def test_health_clears_stale_identity(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    path = lifecycle_state_path(config)
    _write_identity(path, WorkerIdentity(123, "old", str(tmp_path / "worker.json")))
    monkeypatch.setattr("browser_local_ai_bridge.lifecycle.process_control.process_alive", lambda pid, token: False)

    health = worker_health(config)
    assert health["status"] == "STOPPED"
    assert health["stale_state"] is True
    assert not path.exists()


def test_stop_uses_verified_process_identity(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    path = lifecycle_state_path(config)
    _write_identity(path, WorkerIdentity(321, "birth", str(tmp_path / "worker.json")))
    monkeypatch.setattr("browser_local_ai_bridge.lifecycle.process_control.process_alive", lambda pid, token: True)
    seen = []
    monkeypatch.setattr(
        "browser_local_ai_bridge.lifecycle.process_control.terminate_process_tree",
        lambda pid, token: (seen.append((pid, token)) or True, "terminated"),
    )

    result = stop_worker(config)
    assert result["stopped"] is True
    assert seen == [(321, "birth")]
    assert not path.exists()


def test_autostart_enable_status_disable_uses_temp_startup(tmp_path: Path):
    config_path = tmp_path / "worker config.json"
    startup = tmp_path / "Startup"
    enabled = enable_autostart(config_path, startup_dir=startup)
    target = Path(enabled["path"])
    content = target.read_text(encoding="utf-8")
    assert "browser_local_ai_bridge.worker_cli" in content
    assert " start" in content
    assert str(config_path.resolve()) in content
    assert autostart_status(startup_dir=startup)["enabled"] is True
    disabled = disable_autostart(startup_dir=startup)
    assert disabled["removed"] is True
    assert autostart_status(startup_dir=startup)["enabled"] is False
