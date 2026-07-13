"""Lead model — central entity for all lead data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey


class Lead(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "leads"

    # Source identification
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # google_maps, yellsa, cylex, instagram, manual
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Business details
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # E.164
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Social presence
    instagram_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    facebook_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Location
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    province: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Business classification
    business_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Yep Mall enrichment
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail_fetched: Mapped[bool] = mapped_column(Boolean, default=False)

    # Google signals
    google_rating: Mapped[Optional[float]] = mapped_column(Numeric(2, 1), nullable=True)
    google_review_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Scoring
    score: Mapped[int] = mapped_column(Integer, default=0)

    # Lifecycle status
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="discovered",
        index=True,
    )
    # Status values:
    # discovered → enriched → outreach_queued
    # → contacted → responded → interested
    # → converted | not_interested | opted_out | invalid | competitor_customer

    # Outreach tracking
    outreach_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # email, whatsapp, both

    # CRM sync
    synced_to_crm: Mapped[bool] = mapped_column(Boolean, default=False)
    crm_lead_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Notes
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps for outreach cadence
    discovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Web audit fields (migration 002)
    website_platform: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    pagespeed_mobile: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pagespeed_desktop: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pagespeed_seo: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pagespeed_a11y: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    site_copyright_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    web_audit_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    web_audit_pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    web_audit_screenshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    web_pitch_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Audit diagnostic trail (added 2026-07-02). Free-form text explaining
    # why an audit was incomplete/failed: pre-flight HTTP errors, Playwright
    # launch errors, PageSpeed strategy timeouts. NULL means audit succeeded.
    audit_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


    # Mockup generation fields (migration 003)
    # mockup_status: none | queued | generating | pending_approval | approved | rejected | failed
    mockup_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mockup_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mockup_status: Mapped[str] = mapped_column(String(30), nullable=False, default="none")

    # Operator-curated flag for the SELECT page (migration 008).
    # When ANY lead has this set TRUE, /admin/mockup-lead-selection shows
    # ONLY those leads (with a "show all" fallback toggle).
    mockup_targeted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Scraped mockup assets (migration 004) — populated by web_audit.py
    # when the scraper visits the lead's actual website (not booking/social).
    scraped_logo_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scraped_hero_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSONB list of paths: e.g. ["/tmp/cc_assets/foo/gallery/1.jpg", ...]
    scraped_gallery_paths: Mapped[Optional[list]] = mapped_column(
        # NOTE: use Text + JSON encoder to avoid the postgres.JSONB type
        # dependency in code. Most DBs will accept JSONB in column type.
        Text, nullable=True
    )
    scraped_brand_color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)

    # URL quality flag (migration 004). Set TRUE by the scraper when the
    # lead.website URL points to a booking platform, social profile, or
    # directory instead of the business's own site. Mockup generation
    # skips leads with needs_url_review = TRUE.
    needs_url_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    url_quality_issue: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    is_https: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_jsonld: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_analytics: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_manifest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Actual <h1> text. Separate from notes so we can run generic-H1 detection
    # without parsing the audit log. NULL = audit didn't capture it.
    h1_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    services_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True if the page contains known template-leftover copy ("OUR DRESSES",
    # "Join our awesome team", "lorem ipsum", etc.) — strong "owner never
    # touches this" signal.
    has_unedited_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Score breakdown for the admin mockup-approval card tooltip. JSONB with
    # keys like {platform_age_score, platform_complexity_score, copyright_bonus,
    # mobile_penalty, modern_stack_dampener, template_leftover_bonus, total}.
    # Lets the reviewer see exactly why a lead was scored the way it was.
    pitch_breakdown: Mapped[Optional[dict]] = mapped_column(
        # NOTE: use Text + JSON encoder to avoid the postgres.JSONB type
        # dependency in code. Column IS JSONB in Postgres.
        Text,
        nullable=True,
    )

    mockup_eligible_pending_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Relationships
    # Relationships
    sequences: Mapped[list["OutreachSequence"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    events: Mapped[list["LeadEvent"]] = relationship(back_populates="lead", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Lead {self.id} {self.business_name} status={self.status}>"
