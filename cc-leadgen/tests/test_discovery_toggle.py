"""Tests for the admin-toggleable discovery kill switch.

Added 2026-08-28 when the Google Places API budget started running low and
there was no way to pause `run_daily_discovery` short of editing code and
redeploying. Toggled via `app_settings.discovery_enabled` from the Leadgen
Flow admin page (Discovery card).

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && docker compose exec app pytest tests/test_discovery_toggle.py -v
"""
from __future__ import annotations

import pytest


class TestDiscoveryToggle:
    def test_task_skips_when_disabled(self, monkeypatch):
        """When discovery_enabled=false, the task must short-circuit before
        touching Google Places (or any other scraper)."""
        from app.workers import discovery

        monkeypatch.setattr(discovery, "get_bool_setting", lambda key, default=True: False)

        def _boom(*a, **kw):
            raise AssertionError("scrapers must not run when discovery is disabled")
        monkeypatch.setattr(discovery, "run_google_places_discovery", _boom)
        monkeypatch.setattr(discovery, "refresh", _boom)

        result = discovery.run_daily_discovery.run()
        assert result["new"] == 0
        assert result["reason"] == "disabled_via_admin_toggle"

    def test_task_proceeds_when_enabled(self, monkeypatch):
        """When discovery_enabled=true (default), the gate is skipped and the
        task reaches the scraper call (patched to a recogniser)."""
        from app.workers import discovery

        monkeypatch.setattr(discovery, "get_bool_setting", lambda key, default=True: True)

        def _gate_passed(*a, **kw):
            raise RuntimeError("gate passed")
        monkeypatch.setattr(discovery, "refresh", _gate_passed)

        with pytest.raises(RuntimeError, match="gate passed"):
            discovery.run_daily_discovery.run()
