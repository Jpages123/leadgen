"""Tests for the mockup contact-info gate (migration 006 — 2026-07-08).

Three layers of defence:
  1. web_audit worker — only queue mockup if pitch ≥ threshold AND has contact
  2. mockup_generator worker — defensive top-of-function check
  3. enrichment workers — re-trigger deferred mockups after contact added

Plus the threshold change (50 → 70 with V2 scoring).

Run with: cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate && pytest -c /dev/null tests/test_contact_gate.py -v --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import get_settings


# ─── Threshold change (50 → 70) ───────────────────────────────────────────────

def test_mockup_pitch_score_threshold_is_70():
    """Threshold bumped from 50 to 70 to match the new V2 scoring meaning."""
    settings = get_settings()
    assert settings.mockup_pitch_score_threshold == 70, (
        f"Expected 70 (V2-aware threshold), got {settings.mockup_pitch_score_threshold}"
    )


# ─── Helper unit tests (mockup_eligible_pending_contact flag logic) ──────────

def test_mockup_eligible_pending_contact_default_false():
    """New column has server_default=false — only set by audit gate."""
    from sqlalchemy import inspect as sa_inspect
    from app.models import Lead

    mapper = sa_inspect(Lead)
    col = mapper.columns["mockup_eligible_pending_contact"]
    # SQLAlchemy `default=False` only fires on INSERT; check the column
    # itself has a default declared, and the migration set server_default
    assert col.default is not None
    assert col.default.arg is False
    assert col.nullable is False


# ─── web_audit gating logic (mocked) ─────────────────────────────────────────

class _FakeLead:
    """Minimal stand-in for the SQLAlchemy Lead model for gating tests."""
    def __init__(self, email=None, phone=None, whatsapp_number=None,
                 pitch_score=80, mockup_status="none"):
        self.id = "00000000-0000-0000-0000-000000000001"
        self.email = email
        self.phone = phone
        self.whatsapp_number = whatsapp_number
        self.web_pitch_score = pitch_score
        self.mockup_status = mockup_status
        self.mockup_eligible_pending_contact = False


def _extract_gate_logic():
    """Extract the gating decision from the worker without running Playwright.

    Returns the inner 'should we queue the mockup?' logic by reimplementing
    it here for unit testing. Mirrors the production logic in
    app/workers/web_audit.py.
    """
    def decide(lead, threshold: int = 70) -> str:
        """Returns one of: 'queue', 'defer', 'no_op'."""
        pitch = lead.web_pitch_score or 0
        if pitch < threshold:
            return "no_op"
        has_contact = bool(lead.email or lead.phone or lead.whatsapp_number)
        if has_contact:
            return "queue"
        return "defer"
    return decide


def test_gate_queues_when_pitch_high_and_has_email():
    decide = _extract_gate_logic()
    lead = _FakeLead(email="info@plumber.co.za", pitch_score=80)
    assert decide(lead) == "queue"


def test_gate_queues_when_pitch_high_and_has_phone():
    decide = _extract_gate_logic()
    lead = _FakeLead(phone="+27821234567", pitch_score=80)
    assert decide(lead) == "queue"


def test_gate_queues_when_pitch_high_and_has_whatsapp():
    decide = _extract_gate_logic()
    lead = _FakeLead(whatsapp_number="+27821234567", pitch_score=80)
    assert decide(lead) == "queue"


def test_gate_defers_when_pitch_high_but_no_contact():
    decide = _extract_gate_logic()
    lead = _FakeLead(pitch_score=80)
    assert decide(lead) == "defer"


def test_gate_no_op_when_pitch_below_threshold():
    decide = _extract_gate_logic()
    lead = _FakeLead(email="info@plumber.co.za", pitch_score=50)
    assert decide(lead) == "no_op"


def test_gate_no_op_when_pitch_below_threshold_no_contact():
    decide = _extract_gate_logic()
    lead = _FakeLead(pitch_score=40)
    assert decide(lead) == "no_op"


def test_gate_at_threshold_boundary():
    """A pitch_score exactly equal to threshold should queue (>= comparison)."""
    decide = _extract_gate_logic()
    lead = _FakeLead(email="info@example.com", pitch_score=70)
    assert decide(lead) == "queue"


# ─── mockup_generator defensive check (mocked) ────────────────────────────────

def test_mockup_generator_skips_lead_with_no_contact():
    """If somehow called with a no-contact lead, bail out cleanly."""
    from app.workers.mockup_generator import generate_mockup

    fake_lead = _FakeLead(pitch_score=80)  # no email/phone/whatsapp

    with patch("app.workers.mockup_generator.sync_session_scope") as mock_scope:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        result = generate_mockup.apply(args=("00000000-0000-0000-0000-000000000001",))

    # .apply() returns EagerResult — .status is the task state, not the
    # function return value. Use .result (== .get()) for the return dict.
    assert result.successful(), f"Task failed: {result.traceback}"
    rv = result.result
    assert rv["status"] == "skipped"
    assert rv["reason"] == "no_contact_info"


def test_mockup_generator_clears_pending_contact_flag_on_skip():
    """If a lead was flagged pending_contact and we somehow got called, clear the flag."""
    from app.workers.mockup_generator import generate_mockup

    fake_lead = _FakeLead(pitch_score=80)
    fake_lead.mockup_eligible_pending_contact = True  # was flagged

    with patch("app.workers.mockup_generator.sync_session_scope") as mock_scope:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        result = generate_mockup.apply(args=("00000000-0000-0000-0000-000000000001",))

    rv = result.result
    assert rv["status"] == "skipped"
    assert rv["reason"] == "no_contact_info"
    # Flag must be cleared so enrichment re-triggers, not loops forever
    assert fake_lead.mockup_eligible_pending_contact is False


def test_mockup_generator_proceeds_when_contact_present():
    """Defensive check should NOT skip leads with contact info."""
    # We can't run the full mockup pipeline in a unit test (it builds + deploys),
    # but we CAN verify that the defensive check passes through. The existing
    # "skipped_already_processed" check further down will catch us before any
    # real Cloudflare work happens.
    from app.workers.mockup_generator import generate_mockup

    fake_lead = _FakeLead(
        email="info@plumber.co.za",
        pitch_score=80,
        mockup_status="approved",  # already approved → hits existing skip check, not no_contact
    )

    with patch("app.workers.mockup_generator.sync_session_scope") as mock_scope:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        result = generate_mockup.apply(args=("00000000-0000-0000-0000-000000000001",))

    # Hits the existing "already_processed" check, NOT no_contact_info
    rv = result.result
    assert rv["status"] == "skipped"
    assert rv["reason"] == "approved"


# ─── enrichment deferred-mockup re-trigger logic ─────────────────────────────

def test_enrichment_helper_triggers_when_flag_set_and_pitch_high():
    """After enrichment fills email, deferred mockup should be queued."""
    from app.workers.enrichment import _maybe_queue_deferred_mockup

    fake_lead = _FakeLead(email="info@plumber.co.za", pitch_score=85)
    fake_lead.mockup_eligible_pending_contact = True

    with patch("app.workers.enrichment.sync_session_scope") as mock_scope, \
         patch("app.workers.mockup_generator.generate_mockup.delay") as mock_delay:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)

        # Flag should be cleared
        assert fake_lead.mockup_eligible_pending_contact is False
        # Mockup should be queued
        mock_delay.assert_called_once()


def test_enrichment_helper_no_op_when_flag_not_set():
    """If the audit didn't flag the lead, enrichment should do nothing."""
    from app.workers.enrichment import _maybe_queue_deferred_mockup

    fake_lead = _FakeLead(email="info@plumber.co.za", pitch_score=85)
    fake_lead.mockup_eligible_pending_contact = False  # never flagged

    with patch("app.workers.enrichment.sync_session_scope") as mock_scope, \
         patch("app.workers.mockup_generator.generate_mockup.delay") as mock_delay:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)

        mock_delay.assert_not_called()


def test_enrichment_helper_no_op_when_pitch_below_threshold():
    """Pitch dropped below threshold since audit — don't queue a mockup."""
    from app.workers.enrichment import _maybe_queue_deferred_mockup

    fake_lead = _FakeLead(email="info@plumber.co.za", pitch_score=50)
    fake_lead.mockup_eligible_pending_contact = True

    with patch("app.workers.enrichment.sync_session_scope") as mock_scope, \
         patch("app.workers.mockup_generator.generate_mockup.delay") as mock_delay:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)

        mock_delay.assert_not_called()


def test_enrichment_helper_idempotent_against_double_call():
    """If enrichment runs twice (crawl + yep_mall both fire), only one mockup queued."""
    from app.workers.enrichment import _maybe_queue_deferred_mockup

    fake_lead = _FakeLead(email="info@plumber.co.za", pitch_score=85)
    fake_lead.mockup_eligible_pending_contact = True

    with patch("app.workers.enrichment.sync_session_scope") as mock_scope, \
         patch("app.workers.mockup_generator.generate_mockup.delay") as mock_delay:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False

        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)
        # Second call: flag is now False, should no-op
        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)

        mock_delay.assert_called_once()  # not twice


# ─── Integration scenarios ───────────────────────────────────────────────────

def test_high_pitch_no_contact_then_enrichment_triggers():
    """End-to-end: audit defers, enrichment triggers."""
    from app.workers.enrichment import _maybe_queue_deferred_mockup

    # Step 1: audit runs, lead has no contact, pitch=85, threshold=70
    fake_lead = _FakeLead(pitch_score=85)
    decide = _extract_gate_logic()
    assert decide(fake_lead) == "defer"

    # Step 2: enrichment fills email
    fake_lead.email = "info@plumber.co.za"

    # Step 3: enrichment helper checks flag and queues
    fake_lead.mockup_eligible_pending_contact = True
    with patch("app.workers.enrichment.sync_session_scope") as mock_scope, \
         patch("app.workers.mockup_generator.generate_mockup.delay") as mock_delay:
        mock_session = MagicMock()
        mock_session.get.return_value = fake_lead
        mock_scope.return_value.__enter__.return_value = mock_session
        mock_scope.return_value.__exit__.return_value = False
        _maybe_queue_deferred_mockup(str(fake_lead.id), fake_lead.web_pitch_score)

    mock_delay.assert_called_once()
    assert fake_lead.mockup_eligible_pending_contact is False


def test_high_pitch_with_contact_from_start():
    """If audit already has contact info, no flag dance needed."""
    fake_lead = _FakeLead(email="info@plumber.co.za", pitch_score=85)
    decide = _extract_gate_logic()
    assert decide(fake_lead) == "queue"
    # No flag set, no deferral, mockup queued directly from audit


def test_low_pitch_never_triggers_mockup():
    """Even with full contact info, low pitch = no mockup."""
    fake_lead = _FakeLead(
        email="info@modern-site.co.za",
        phone="+27821234567",
        whatsapp_number="+27821234567",
        pitch_score=30,
    )
    decide = _extract_gate_logic()
    assert decide(fake_lead) == "no_op"