"""Tests for business-hours gating on the initial (step-1) outreach send.

Incident (2026-08-27): send_email_sequence had no time-of-day restriction at
all, unlike send_follow_up_sequence (Phase N). The outreach-queue-leads
(every 2h) and outreach-send (every 30 min) beat ticks landed on the same
00:00 UTC tick and cold-emailed 5 real prospects at 02:00 SAST. This adds
the same is_business_time() short-circuit used by the follow-up worker.

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && docker compose exec app pytest tests/test_outreach_business_hours.py -v
"""
from __future__ import annotations

import pytest


class TestOutreachSendKillSwitch:
    def test_task_disabled_by_default(self, monkeypatch):
        """send_email_sequence (2026-08-28): no-review auto-send of the old
        WhatsApp-pitch templates, disabled after the pivot to web-revamp-only.
        Must short-circuit before even reaching the business-hours gate."""
        from app.workers import outreach

        monkeypatch.setattr(outreach.settings, "outreach_send_enabled", False)

        def _boom(*a, **kw):
            raise AssertionError("DB/SMTP must not be touched while outreach_send_enabled=False")
        monkeypatch.setattr(outreach, "sync_session_scope", _boom)

        result = outreach.send_email_sequence()
        assert result["sent"] == 0
        assert result["reason"] == "disabled_post_pivot"


class TestOutreachBusinessHoursGating:
    """These exercise the business-hours gate that runs once the kill switch
    (outreach_send_enabled) is explicitly re-enabled — see
    TestOutreachSendKillSwitch above for the default-disabled behavior."""

    def test_task_skips_outside_business_hours(self, monkeypatch):
        """With outreach_business_hours_only=True and is_business_time returning
        False, the task must short-circuit before touching DB or SMTP."""
        from app.workers import outreach

        monkeypatch.setattr(outreach.settings, "outreach_send_enabled", True)
        monkeypatch.setattr(outreach, "is_business_time", lambda now=None: False)
        monkeypatch.setattr(outreach.settings, "outreach_business_hours_only", True)

        def _boom(*a, **kw):
            raise AssertionError("DB/SMTP must not be touched when outside business hours")
        monkeypatch.setattr(outreach, "sync_session_scope", _boom)

        result = outreach.send_email_sequence()
        assert result["sent"] == 0
        assert result["reason"] == "outside_business_hours"

    def test_task_proceeds_when_disabled(self, monkeypatch):
        """When outreach_business_hours_only=False, the gate is skipped even
        outside business hours. We short-circuit at sync_session_scope so the
        test is independent of DB fixtures; reaching it proves the gate was passed."""
        from app.workers import outreach

        monkeypatch.setattr(outreach.settings, "outreach_send_enabled", True)
        monkeypatch.setattr(outreach, "is_business_time", lambda now=None: False)
        monkeypatch.setattr(outreach.settings, "outreach_business_hours_only", False)

        def _gate_passed(*a, **kw):
            raise RuntimeError("gate passed")
        monkeypatch.setattr(outreach, "sync_session_scope", _gate_passed)

        with pytest.raises(RuntimeError, match="gate passed"):
            outreach.send_email_sequence()

    def test_task_proceeds_in_business_hours(self, monkeypatch):
        """When is_business_time returns True, the gate doesn't block; we
        reach the DB layer (patched to recogniser)."""
        from app.workers import outreach

        monkeypatch.setattr(outreach.settings, "outreach_send_enabled", True)
        monkeypatch.setattr(outreach, "is_business_time", lambda now=None: True)
        monkeypatch.setattr(outreach.settings, "outreach_business_hours_only", True)

        def _gate_passed(*a, **kw):
            raise RuntimeError("gate passed")
        monkeypatch.setattr(outreach, "sync_session_scope", _gate_passed)

        with pytest.raises(RuntimeError, match="gate passed"):
            outreach.send_email_sequence()
