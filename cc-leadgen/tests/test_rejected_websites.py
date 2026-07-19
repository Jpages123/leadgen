"""Unit tests for rejected_websites utility."""
import pytest
from app.utils.rejected_websites import _normalise, is_rejected, refresh, add, remove, list_all
from app.models import RejectedWebsite
from app.db.sync_session import sync_session_scope
import sqlalchemy as sa


class TestNormalise:
    def test_https_strips_scheme(self):
        assert _normalise("https://example.com") == "example.com"

    def test_http_strips_scheme(self):
        assert _normalise("http://example.com") == "example.com"

    def test_www_stripped(self):
        assert _normalise("https://www.example.com") == "example.com"

    def test_trailing_slash_stripped(self):
        assert _normalise("https://example.com/") == "example.com"

    def test_subpath_keeps_domain(self):
        assert _normalise("https://example.com/franchise/location") == "example.com"

    def test_no_scheme_preserved(self):
        assert _normalise("example.com") == "example.com"

    def test_lowercase(self):
        assert _normalise("HTTPS://WWW.EXAMPLE.COM/") == "example.com"

    def test_strips_whitespace(self):
        assert _normalise("  https://example.com  ") == "example.com"

    def test_empty_returns_empty(self):
        assert _normalise("") == ""
        assert _normalise("   ") == ""


class TestIsRejectedIntegration:
    """Real DB tests — require a live DB (leadgen)."""

    def _cleanup(self):
        from app.db.sync_session import sync_session_scope
        from app.models import RejectedWebsite
        with sync_session_scope() as s:
            s.query(RejectedWebsite).delete()
            s.commit()
        refresh()

    def test_lead_not_rejected_initially(self):
        self._cleanup()
        assert is_rejected("https://newsite.com") is False

    def test_lead_rejected_after_add(self):
        self._cleanup()
        add("franchise.com", "Franchise lead")
        refresh()
        assert is_rejected("https://www.franchise.com") is True
        assert is_rejected("https://other.com") is False
        self._cleanup()

    def test_none_returns_false(self):
        assert is_rejected(None) is False

    def test_empty_returns_false(self):
        assert is_rejected("") is False

    def test_normalisation_is_case_insensitive(self):
        self._cleanup()
        add("FRANCHISENAME.COM", "Franchise")
        refresh()
        assert is_rejected("https://www.franchisename.com") is True
        self._cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
