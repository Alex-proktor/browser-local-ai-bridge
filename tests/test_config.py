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


@pytest.mark.parametrize("recipe", [
    None, [], {}, {"argv": []}, {"argv": "python"},
    {"argv": ["python", "-V"]}, {"argv": [""]},
    {"argv": ["/tool", None]}, {"argv": ["/tool", "nul\x00"]},
    {"argv": ["/tool"], "shell": True},
])
def test_invalid_recipes_fail_closed(tmp_path, recipe):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"version": 1, "repos": {"repo": {
        "checkout_path": str(tmp_path), "recipes": {"check": recipe},
    }}}))
    with pytest.raises(ConfigError):
        load_repo_allowlist(path)


@pytest.mark.parametrize("field,value", [
    ("timeout_seconds", True), ("timeout_seconds", 0),
    ("timeout_seconds", 301), ("timeout_seconds", 1.5),
    ("max_output_bytes", False), ("max_output_bytes", 0),
    ("max_output_bytes", 1048577),
])
def test_recipe_bounds(tmp_path, field, value):
    import sys
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"repos": {"repo": {
        "checkout_path": str(tmp_path), "recipes": {
            "check": {"argv": [sys.executable], field: value},
        },
    }}, "version": 1}))
    with pytest.raises(ConfigError):
        load_repo_allowlist(path)


@pytest.mark.parametrize("name", ["../escape", "a b", "", "x" * 81])
def test_invalid_recipe_names(tmp_path, name):
    import sys
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"version": 1, "repos": {"repo": {
        "checkout_path": str(tmp_path), "recipes": {name: {"argv": [sys.executable]}},
    }}}))
    with pytest.raises(ConfigError):
        load_repo_allowlist(path)


@pytest.mark.parametrize("suffix", [".cmd", ".BAT"])
def test_batch_recipe_rejected(tmp_path, suffix):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"version": 1, "repos": {"repo": {
        "checkout_path": str(tmp_path), "recipes": {
            "check": {"argv": [str(tmp_path / ("tool" + suffix))]},
        },
    }}}))
    with pytest.raises(ConfigError, match="non-batch"):
        load_repo_allowlist(path)
