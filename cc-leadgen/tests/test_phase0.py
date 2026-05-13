"""Phase 0 test — verifies the Celery app can be imported and config is correct."""
from __future__ import annotations

import pytest

from app.workers.celery_app import celery_app


def test_celery_app_created():
    """Celery app is instantiated and beat schedule is configured."""
    assert celery_app is not None
    beat_schedule = celery_app.conf.beat_schedule
    # Keys are friendly names; check the task name is in the value
    task_names = [entry["task"] for entry in beat_schedule.values()]
    assert "app.workers.discovery.tasks.run_daily_discovery" in task_names
    assert "app.workers.enrichment.tasks.process_enrichment_queue" in task_names
    assert "app.workers.outreach.tasks.schedule_pending_sequences" in task_names


def test_worker_queues_configured():
    """All worker queues are registered via task_routes wildcards."""
    routes = celery_app.conf.task_routes
    # Routes use wildcard patterns
    assert "app.workers.discovery.*" in routes
    assert "app.workers.enrichment.*" in routes
    assert "app.workers.outreach.*" in routes
    assert "app.workers.response.*" in routes


def test_tz_is_south_africa():
    """Celery is configured to run in SA timezone."""
    assert celery_app.conf.timezone == "Africa/Johannesburg"


def test_task_acks_late_and_reject_on_lost():
    """Workers are configured for reliability."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_celery_app_autodiscover_includes_all_workers():
    """All worker modules are in the include list."""
    includes = celery_app.conf.include
    assert "app.workers.discovery" in includes
    assert "app.workers.enrichment" in includes
    assert "app.workers.scoring" in includes
    assert "app.workers.outreach" in includes
    assert "app.workers.response" in includes


def test_models_import():
    """All SQLAlchemy models import without error."""
    from app.models import Lead, OutreachSequence, OutreachTemplate, LeadEvent, DiscoveryJob
    assert Lead.__tablename__ == "leads"
    assert OutreachSequence.__tablename__ == "outreach_sequences"
    assert OutreachTemplate.__tablename__ == "outreach_templates"
    assert LeadEvent.__tablename__ == "lead_events"
    assert DiscoveryJob.__tablename__ == "discovery_jobs"


def test_config_loads():
    """Settings loads from .env without errors."""
    from app.config import get_settings
    settings = get_settings()
    assert settings.log_level in ("DEBUG", "INFO", "WARNING", "ERROR")
    assert settings.database_url.startswith("postgresql")
    assert settings.redis_url.startswith("redis")