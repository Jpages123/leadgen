"""Load lead context — for the TypeScript skill to inspect before generating."""
from __future__ import annotations

import json
from datetime import datetime, date
from typing import Any

from app.db.sync_session import sync_session_scope
from app.models.lead import Lead
from sqlalchemy import select


def _jsonable(value: Any) -> Any:
    """Convert datetime/date/UUID to JSON-friendly types."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "hex"):  # UUID
        return str(value)
    return value


def load_lead(lead_id: str) -> dict:
    """Return everything the skill needs to know about a lead."""
    with sync_session_scope() as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"lead {lead_id} not found")

        # Pull all the fields we care about
        snapshot = {
            "id": str(lead.id),
            "business_name": lead.business_name,
            "business_type": lead.business_type,
            "city": lead.city,
            "website": lead.website,
            "phone": lead.phone,
            "email": lead.email,
            "whatsapp_number": lead.whatsapp_number,
            "website_platform": lead.website_platform,
            "pagespeed_mobile": lead.pagespeed_mobile,
            "pagespeed_desktop": lead.pagespeed_desktop,
            "pagespeed_seo": lead.pagespeed_seo,
            "pagespeed_a11y": lead.pagespeed_a11y,
            "site_copyright_year": getattr(lead, "site_copyright_year", None),
            "web_pitch_score": lead.web_pitch_score,
            "web_audit_generated_at": lead.web_audit_generated_at,
            "web_audit_screenshot_path": lead.web_audit_screenshot_path,
            "web_audit_pdf_path": getattr(lead, "web_audit_pdf_path", None),
            "google_rating": float(lead.google_rating) if lead.google_rating else None,
            "google_review_count": lead.google_review_count,
            "facebook_url": lead.facebook_url,
            "instagram_url": lead.instagram_url,
            # Scraped assets
            "scraped_brand_color": getattr(lead, "scraped_brand_color", None),
            "scraped_logo_path": getattr(lead, "scraped_logo_path", None),
            "scraped_hero_path": getattr(lead, "scraped_hero_path", None),
            "scraped_gallery_paths": getattr(lead, "scraped_gallery_paths", None),
            "scraped_about_path": getattr(lead, "scraped_about_path", None),
            "needs_url_review": getattr(lead, "needs_url_review", False),
            "url_quality_issue": getattr(lead, "url_quality_issue", None),
            # Mockup state
            "mockup_status": lead.mockup_status,
            "mockup_url": lead.mockup_url,
        }

        # Make everything JSON-friendly
        return {k: _jsonable(v) for k, v in snapshot.items()}
