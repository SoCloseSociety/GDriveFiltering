"""Tests for audit-hardening: long names, account sanitization, progress race."""
from gdrivefilter.dashboard import _clean_account
from gdrivefilter.drive_client import _MAX_COMPONENT, _safe


def test_safe_truncates_long_names_keeping_extension():
    name = "a" * 300 + ".pdf"
    out = _safe(name)
    assert len(out) <= _MAX_COMPONENT
    assert out.endswith(".pdf")


def test_safe_strips_illegal_and_control_chars():
    assert _safe('a:b/c*?.txt') == "a_b_c__.txt"
    assert _safe("trailing.  ") == "trailing"
    assert _safe("") == "unnamed"


def test_clean_account_blocks_traversal():
    assert _clean_account("../../etc/passwd") == "etcpasswd"
    assert _clean_account("perso") == "perso"
    assert _clean_account("") == "default"
    assert "/" not in _clean_account("a/b/c") and ".." not in _clean_account("..")
