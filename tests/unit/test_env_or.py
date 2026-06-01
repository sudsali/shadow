"""env_or precedence: env (stripped) > yaml (stripped if str) > default."""
from shadow.shadow_config import env_or


def test_env_wins(monkeypatch):
    monkeypatch.setenv("SHADOW_TEST_VAR", "from-env")
    assert env_or("SHADOW_TEST_VAR", "from-yaml", "default") == "from-env"


def test_yaml_when_env_unset(monkeypatch):
    monkeypatch.delenv("SHADOW_TEST_VAR", raising=False)
    assert env_or("SHADOW_TEST_VAR", "from-yaml", "default") == "from-yaml"


def test_default_when_both_unset(monkeypatch):
    monkeypatch.delenv("SHADOW_TEST_VAR", raising=False)
    assert env_or("SHADOW_TEST_VAR", None, "default") == "default"


def test_strips_env_whitespace(monkeypatch):
    monkeypatch.setenv("SHADOW_TEST_VAR", "  spaced  ")
    assert env_or("SHADOW_TEST_VAR", "yaml", "default") == "spaced"


def test_whitespace_env_falls_through_to_yaml(monkeypatch):
    monkeypatch.setenv("SHADOW_TEST_VAR", "   ")
    assert env_or("SHADOW_TEST_VAR", "yaml", "default") == "yaml"


def test_strips_yaml_whitespace(monkeypatch):
    # Symmetric strip: a yaml-quoted "  python  " shouldn't leak whitespace
    # into a downstream consumer that an env var of the same value wouldn't.
    monkeypatch.delenv("SHADOW_TEST_VAR", raising=False)
    assert env_or("SHADOW_TEST_VAR", "  spaced  ", "default") == "spaced"


def test_yaml_int_passes_through(monkeypatch):
    # env_or doesn't coerce types; downstream callers wrap with
    # _str_or_default so an int doesn't crash file-system code.
    monkeypatch.delenv("SHADOW_TEST_VAR", raising=False)
    assert env_or("SHADOW_TEST_VAR", 123, "default") == 123
