"""_str_field is the coercion layer between model-emitted JSON and the
downstream `.strip()` / format-string callers. The schema declares string
fields, but a misbehaving model can emit list/dict/int/bool — passing those
through would crash the pipeline before any artifact is written. This
contract belongs in unit-test territory: every change to the helper must
preserve the "non-string → empty string" guarantee."""
from shadow.main import _str_field


def test_str_passthrough():
    assert _str_field("hi") == "hi"


def test_empty_str_passthrough():
    """Empty string is still a string; don't replace with anything."""
    assert _str_field("") == ""


def test_bool_returns_empty():
    """`bool` is a subclass of `int`, NOT of `str`. Lock the test so a
    refactor that adds `int` coercion doesn't silently start emitting
    "True"/"False" into review bodies."""
    assert _str_field(True) == ""
    assert _str_field(False) == ""


def test_int_returns_empty():
    assert _str_field(42) == ""


def test_zero_returns_empty():
    """0 is falsy but the same coercion path applies — must return ""
    (not None, not 0)."""
    assert _str_field(0) == ""


def test_none_returns_empty():
    assert _str_field(None) == ""


def test_list_returns_empty():
    assert _str_field([1, 2, 3]) == ""


def test_dict_returns_empty():
    assert _str_field({"a": 1}) == ""


def test_float_returns_empty():
    assert _str_field(3.14) == ""


def test_multiline_str_passthrough():
    """Newlines, control chars, etc. are NOT scrubbed here — that's a
    different layer's job. _str_field only enforces the type contract."""
    assert _str_field("a\nb\tc") == "a\nb\tc"
