"""Application configuration — loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Server ───────────────────────────────────────────────────────
    compose_project_name: str = "cc-leadgen"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ── Database ─────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:postgres@localhost:5432/leadgen"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # ── Redis / Celery ───────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # ── Production CRM Sync ──────────────────────────────────────────
    prod_db_url: str = ""
    prod_tailscale_ip: str = ""

    # ── Google Maps ───────────────────────────────────────────────────
    google_maps_api_key: str = ""
    google_places_api_key: str = ""
    google_maps_daily_budget_usd: float = 10.0
    google_maps_rate_limit: int = 2

    # ── SMTP ──────────────────────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from_name: str = "Client Compass"
    smtp_from_email: str = ""
    outreach_domain: str = ""
    email_daily_limit: int = 50
    email_min_gap_hours: int = 72

    # ── Follow-up Sequences (web-revamp leads only) ────────────────────
    # Step 2 sent ~4 days after step 1; step 3 (final) sent ~5 more days
    # after step 2 (~9 days total). Lead marked 'no_response' after step 3
    # if no reply.
    follow_up_step2_gap_hours: int = 96
    follow_up_step3_gap_hours: int = 120

    # ── Business-hours gating (Phase M hardening) ────────────────────────────
    # When enabled, send_follow_up_sequence refuses to send outside business
    # hours in the configured timezone. Candidates remain eligible and will
    # fire on the next eligible beat tick that lands in business hours.
    # SAST is UTC+2 year-round (no DST) so we use a fixed offset rather than
    # an external timezone library.
    follow_up_business_hours_only: bool = True
    follow_up_business_tz_offset_hours: int = 2   # SAST = UTC+2
    follow_up_business_start_hour: int = 8       # 08:00 inclusive
    follow_up_business_end_hour: int = 17        # 17:00 exclusive

    # ── Within-day pacing (Phase N) ──────────────────────────────────────────
    # Spread follow-up sends evenly across today's remaining business hours so a
    # Monday-morning backlog doesn't fire as a single burst. Interval is computed
    # as (end_hour - start_hour) hours / N, clamped by a min-interval floor.
    # Disable with follow_up_pace_across_business_hours=False to restore the old
    # 2-second sleep between sends.
    follow_up_pace_across_business_hours: bool = True
    follow_up_pace_min_interval_seconds: int = 30  # never send faster than this

    # ── Business-hours gating for initial (step-1) outreach ──────────────────
    # send_email_sequence previously had no time-of-day restriction at all —
    # it could (and did) fire in the middle of the night whenever
    # outreach-queue-leads and outreach-send landed on the same beat tick.
    # Reuses the same business-hours window / timezone offset / pacing floor
    # as the follow-up settings above, since it's one company-wide policy.
    outreach_business_hours_only: bool = True
    outreach_pace_across_business_hours: bool = True

    # Kill switch for send_email_sequence (2026-08-28): this is the only
    # outreach path with no operator review — it auto-sends the old
    # WhatsApp-pitch templates to generic (non-web-revamp) leads. Disabled
    # after the pivot to web-revamp-only surfaced a ~130-lead pre-pivot
    # backlog getting emailed with the stale pitch. The `outreach-send`
    # beat entry is also removed in celery_app.py; this flag guards against
    # the task being manually re-triggered. Flip back on only once a
    # reviewed draft flow exists for generic leads.
    outreach_send_enabled: bool = False

    # ── WhatsApp (deferred) ───────────────────────────────────────────
    meta_graph_version: str = "v22.0"
    whatsapp_number: str = "+27740940550"

    # ── Discovery Sources ─────────────────────────────────────────────
    yellsa_max_pages: int = 10
    cylex_max_pages: int = 10

    # ── Scoring Thresholds ────────────────────────────────────────────
    score_outreach_high: int = 60
    score_outreach_medium: int = 40
    score_outreach_low: int = 20

    # ── Discord Alerts ────────────────────────────────────────────────
    discord_webhook_url: str = ""
    discord_alert_on_reply: bool = True
    discord_alert_on_error: bool = True

    # ── Mockup Generation ──────────────────────────────────────────────────────
    api_key: str = Field(default="", validation_alias="CC_LEADGEN_API_KEY")
    cloudflare_api_token: str = ""
    mockup_pitch_score_threshold: int = 70
    mockup_pi_timeout: int = 60
    # Pi subprocess slot pool — bounds concurrent pi invocations across the worker.
    # Pi serializes internally, so >1 concurrent calls just queue and risk 300s timeout.
    # Set to 1 by default; raise only if you have evidence the pi lock is gone.
    pi_max_concurrent: int = Field(default=1, validation_alias="PI_MAX_CONCURRENT")
    # How long a task will wait to acquire a slot before failing. Default 0 =
    # block indefinitely (Redis BLPOP timeout=0). Set a positive int if you want
    # a hard cap; note that the subprocess timeout in mockup_generator.py
    # (DEFAULT_TIMEOUT_S) is the real hang detector for stuck pi invocations.
    pi_slot_timeout_s: int = Field(default=0, validation_alias="PI_SLOT_TIMEOUT_S")

    # ── VPS integration (email asset upload target) ─────────────────────────
    # login-portal URL that hosts the /api/email-assets/upload endpoint.
    # Must be reachable from the laptop — typically login.clientcompass.co.za.
    leadgen_login_base_url: str = Field(
        default="https://login.clientcompass.co.za",
        validation_alias="LEADGEN_LOGIN_BASE_URL",
    )
    mockup_regen_batch_size: int = 50  # sweep size for mockup_regen beat task
    # Immediate auto-retry cap. retry_single_mockup dispatches generate_mockup
    # again after each failure until mockup_retry_count >= this. Set to 0 to
    # disable immediate retries entirely (fall back to the 30-min beat sweep).
    mockup_max_auto_retries: int = Field(default=3, validation_alias="MOCKUP_MAX_AUTO_RETRIES")
    # How long _mark_failed waits before scheduling retry_single_mockup.
    # Gives transient issues (e.g. flaky Cloudflare Pages deploy) time to
    # settle before the retry hits. Bump to 300+ if you're seeing retry
    # storms during partial outages.
    mockup_auto_retry_delay_s: int = Field(default=60, validation_alias="MOCKUP_AUTO_RETRY_DELAY_S")

    # ── Stock image fallback ──────────────────────────────────────────
    pexels_api_key: str = ""

    # ── Unsubscribe JWT ───────────────────────────────────────────────
    unsubscribe_secret: str = "change-me"

    # ── Outreach Safety Gates ────────────────────────────────────────
    # 'test' = only send to TEST_EMAIL_WHITELIST (comma-sep CSV, no real leads)
    # 'live' = send to all outreach_queued leads
    send_mode: Literal["test", "live"] = "test"
    test_email_whitelist_csv: str = "jpages123@gmail.com,jpages123@proton.me"
    blocked_email_domains_csv: str = "gmail.com,proton.me,protonmail.com,yahoo.com,hotmail.com,outlook.com,icloud.com,mail.com,aol.com"
    # Comma-separated website domains/URLs to reject during discovery.
    # Entries are normalised (scheme + www stripped) before matching.
    # Use this as a quick fallback; the preferred path is the rejected_websites DB table.
    rejected_websites_csv: str = ""

    # ── Cron Schedule ─────────────────────────────────────────────────
    discovery_schedule_hour: int = 8
    outreach_schedule_minute: str = "*/30"
    response_poll_interval_minutes: int = 15

    # ── Outreach Safety Gates ────────────────────────────────────────
    # CSV strings parsed into lists via properties below
    send_mode: Literal["test", "live"] = "test"
    test_email_whitelist_csv: str = "jpages123@gmail.com,jpages123@proton.me"
    blocked_email_domains_csv: str = "gmail.com,proton.me,protonmail.com,yahoo.com,hotmail.com,outlook.com,icloud.com,mail.com,aol.com"

    @property
    def test_email_whitelist(self) -> list[str]:
        """Parse comma-separated test email list."""
        raw = self.test_email_whitelist_csv or ""
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def blocked_email_domains(self) -> list[str]:
        """Parse comma-separated blocked domain list."""
        raw = self.blocked_email_domains_csv or ""
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def rejected_websites(self) -> list[str]:
        """Parse comma-separated rejected-website list (CSV config fallback)."""
        raw = getattr(self, "rejected_websites_csv", "") or ""
        return [x.strip() for x in raw.split(",") if x.strip()]

    # ── Mockup post-approval cleanup (Session 25, 2026-08-06) ───────
    # When the operator approves a mockup in /admin/mockup-approvals, the
    # ``app.workers.mockup_cleanup.cleanup_approved_mockup`` task is
    # dispatched from ``sync_approvals``. It removes the build dir +
    # audit cache + local PDF (saves ~150MB per approved mockup) while
    # keeping the live deployed mockup, the approval row, and the email
    # draft trail intact.
    #
    # Set ``CLEANUP_ON_APPROVAL=false`` in .env to opt out globally (e.g.
    # if an operator wants to inspect post-approval build artefacts).
    cleanup_on_approval: bool = Field(default=True, validation_alias="CLEANUP_ON_APPROVAL")

    @property
    def database_url_sync(self) -> str:
        """Sync URL for psycopg2 / sqlalchemy (not asyncpg)."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()