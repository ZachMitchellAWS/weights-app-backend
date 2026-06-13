"""
Unit tests for the email blacklist guard.

The guard itself is one pure function — `is_email_blocked()` — wired into
three handler sites in `handlers/auth.py`. Those call sites are trivial
(`if is_email_blocked(x): return static_response`), so all the logic worth
testing lives in this module. We do not integration-test the handlers here
because the real `handlers/auth.py` has heavy module-load dependencies
(Sentry init, boto3 DynamoDB resource, SSM-backed JWT signing) and the
existing test scaffold deliberately avoids them — see `test_handlers.py`,
which targets the `login.py`/`register.py` stubs instead.

Per `blacklist.py`'s docstring, the sets store REVERSED strings. Tests
construct reversed values inline with `"forward-form"[::-1]` so the reader
can see the human-readable address being blocked.

To run:
    pytest services/auth/tests/unit/test_blacklist.py -v
"""

import sys
from pathlib import Path

import pytest

lambda_path = Path(__file__).parent.parent.parent / "lambda"
sys.path.insert(0, str(lambda_path))

from utils import blacklist
from utils.blacklist import is_email_blocked


def rev(s: str) -> str:
    return s[::-1]


@pytest.fixture(autouse=True)
def reset_blacklist(monkeypatch):
    """Each test starts with all sets empty, then adds what it needs."""
    monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", set())
    monkeypatch.setattr(blacklist, "BLOCKED_EMAILS", set())
    monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", set())


class TestEmptyBlacklist:
    def test_allowed_email_passes(self):
        assert is_email_blocked("user@example.com") is False


class TestMissingOrMalformed:
    def test_none_returns_false(self):
        assert is_email_blocked(None) is False

    def test_empty_string_returns_false(self):
        assert is_email_blocked("") is False

    def test_no_at_sign_returns_false(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("example.com")})
        assert is_email_blocked("not-an-email") is False


class TestBlockedDomains:
    def test_exact_domain_match(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("baddomain.test")})
        assert is_email_blocked("user@baddomain.test") is True

    def test_allowed_domain_passes(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("baddomain.test")})
        assert is_email_blocked("user@gooddomain.test") is False

    def test_subdomain_is_not_blocked(self, monkeypatch):
        """Exact match only — subdomain matching is out of scope per plan."""
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("baddomain.test")})
        assert is_email_blocked("user@mail.baddomain.test") is False

    def test_parent_domain_is_not_blocked_by_child(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("mail.baddomain.test")})
        assert is_email_blocked("user@baddomain.test") is False


class TestBlockedEmails:
    def test_exact_email_match(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_EMAILS", {rev("spammer@gmail.com")})
        assert is_email_blocked("spammer@gmail.com") is True

    def test_allowed_email_at_same_domain(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_EMAILS", {rev("spammer@gmail.com")})
        assert is_email_blocked("legit@gmail.com") is False


class TestNormalization:
    def test_case_insensitive_email(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_EMAILS", {rev("spammer@gmail.com")})
        assert is_email_blocked("Spammer@Gmail.com") is True
        assert is_email_blocked("SPAMMER@GMAIL.COM") is True

    def test_case_insensitive_domain(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("baddomain.test")})
        assert is_email_blocked("User@BADDOMAIN.TEST") is True

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("baddomain.test")})
        assert is_email_blocked("  user@baddomain.test  ") is True

    def test_case_variant_bypass_blocked(self, monkeypatch):
        """The bypass attempt described in the plan's smoke test #6."""
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("blocked.test")})
        assert is_email_blocked("Test@BLOCKED.test") is True


class TestBlockedSubstrings:
    def test_substring_in_local_part(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("spam")})
        assert is_email_blocked("spammer@example.com") is True

    def test_substring_in_domain(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("sketchy")})
        assert is_email_blocked("user@sketchy.test") is True

    def test_substring_match_is_broad_by_design(self, monkeypatch):
        """`nospam` is intentionally caught — substring is intentionally broad."""
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("spam")})
        assert is_email_blocked("nospam@example.com") is True

    def test_no_substring_match_passes(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("spam")})
        assert is_email_blocked("clean@example.com") is False

    def test_substring_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("spam")})
        assert is_email_blocked("SPAMMER@EXAMPLE.COM") is True

    def test_substring_can_span_at_sign(self, monkeypatch):
        """Substring check runs against the full normalized address."""
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("er@ex")})
        assert is_email_blocked("user@example.com") is True


class TestReverseConvention:
    def test_forward_form_does_NOT_match(self, monkeypatch):
        """If someone forgets to reverse the entry, it must NOT match —
        otherwise the obfuscation convention is silently broken."""
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {"baddomain.test"})  # NOT reversed
        assert is_email_blocked("user@baddomain.test") is False

    def test_palindrome_substring_matches_either_way(self, monkeypatch):
        """A palindrome reversed is itself, so it matches either form. This is
        the one case where forgetting to reverse won't bite — documented to
        prevent surprise."""
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {"aba"})
        assert is_email_blocked("ababa@example.com") is True


class TestEdgeCases:
    def test_quoted_local_part_with_at(self, monkeypatch):
        """`rsplit("@", 1)` handles legal `@` in quoted local parts."""
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("example.com")})
        assert is_email_blocked('"weird@local"@example.com') is True

    def test_all_three_lists_active(self, monkeypatch):
        monkeypatch.setattr(blacklist, "BLOCKED_DOMAINS", {rev("other.test")})
        monkeypatch.setattr(blacklist, "BLOCKED_EMAILS", {rev("user@example.com")})
        monkeypatch.setattr(blacklist, "BLOCKED_SUBSTRINGS", {rev("banned")})
        assert is_email_blocked("user@example.com") is True
        assert is_email_blocked("user@other.test") is True
        assert is_email_blocked("banned-keyword@allowed.test") is True
        assert is_email_blocked("clean@allowed.test") is False
