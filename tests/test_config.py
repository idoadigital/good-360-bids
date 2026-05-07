"""Tests for config.py — env substitution and org filtering."""
import json
from pathlib import Path

import pytest

import config


def test_substitute_resolves_env_placeholder(clean_env):
    clean_env.setenv("FOO", "bar")
    assert config._substitute("${FOO}") == "bar"
    assert config._substitute("prefix-${FOO}-suffix") == "prefix-bar-suffix"


def test_substitute_missing_var_becomes_empty(clean_env):
    assert config._substitute("${DOES_NOT_EXIST}") == ""


def test_substitute_recurses_into_nested(clean_env):
    clean_env.setenv("X", "1")
    data = {"a": "${X}", "b": ["${X}", {"c": "${X}"}]}
    assert config._substitute(data) == {"a": "1", "b": ["1", {"c": "1"}]}


def test_substitute_leaves_non_strings_alone():
    assert config._substitute(42) == 42
    assert config._substitute(True) is True
    assert config._substitute(None) is None


def test_load_orgs_drops_orgs_missing_secrets(tmp_path, clean_env):
    template = {
        "complete": {
            "name": "Complete Org",
            "good360_email": "${E}",
            "good360_password": "${P}",
        },
        "incomplete": {
            "name": "No Secrets",
            "good360_email": "${MISSING_E}",
            "good360_password": "${MISSING_P}",
        },
    }
    path = tmp_path / "orgs.json"
    path.write_text(json.dumps(template))

    clean_env.setenv("E", "a@b.com")
    clean_env.setenv("P", "pw")

    orgs = config.load_orgs(path)
    assert set(orgs.keys()) == {"complete"}
    assert orgs["complete"]["good360_email"] == "a@b.com"


def test_load_orgs_drops_all_when_no_secrets_set(tmp_path, clean_env):
    template = {"x": {"good360_email": "${E}", "good360_password": "${P}"}}
    path = tmp_path / "orgs.json"
    path.write_text(json.dumps(template))
    assert config.load_orgs(path) == {}


def test_env_required_raises(clean_env):
    with pytest.raises(RuntimeError, match="REQUIRED_VAR"):
        config.env("REQUIRED_VAR", required=True)


def test_env_default_returned(clean_env):
    assert config.env("NOPE", default="fallback") == "fallback"


def test_example_template_substitutes_cleanly(clean_env):
    """The shipped good360_orgs_master.example.json must parse and substitute."""
    repo_root = Path(__file__).resolve().parents[1]
    clean_env.setenv("GOOD360_HOPE4HUMANITY_EMAIL", "h@x.com")
    clean_env.setenv("GOOD360_HOPE4HUMANITY_PASSWORD", "pw")
    orgs = config.load_orgs(repo_root / "good360_orgs_master.example.json")
    assert "hope4humanity" in orgs
    assert orgs["hope4humanity"]["good360_email"] == "h@x.com"
    # reviving_homes has no env set → should be dropped
    assert "reviving_homes" not in orgs
