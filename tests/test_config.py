import json
from pathlib import Path

import pytest

from browser_local_ai_bridge.config import ConfigError, load_repo_allowlist, resolve_repo


def test_allowlist_resolves_known_repo(tmp_path: Path):
    checkout = tmp_path / "repo"
    allowlist_path = tmp_path / "repos.json"
    allowlist_path.write_text(json.dumps({"version": 1, "repos": {"example/demo": str(checkout)}}), encoding="utf-8")
    allowlist = load_repo_allowlist(allowlist_path)
    target = resolve_repo("example/demo", allowlist)
    assert target.logical_name == "example/demo"
    assert target.checkout_path == checkout.resolve()


def test_unknown_repo_fails_closed(tmp_path: Path):
    allowlist_path = tmp_path / "repos.json"
    allowlist_path.write_text(json.dumps({"version": 1, "repos": {}}), encoding="utf-8")
    allowlist = load_repo_allowlist(allowlist_path)
    with pytest.raises(ConfigError):
        resolve_repo("unknown/repo", allowlist)


def test_unknown_allowlist_version_fails_closed(tmp_path: Path):
    allowlist_path = tmp_path / "repos.json"
    allowlist_path.write_text(json.dumps({"version": 2, "repos": {}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_repo_allowlist(allowlist_path)
