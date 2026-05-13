"""Discord webhook notifications."""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)


class DiscordEmbed(BaseModel):
    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: list[dict] | None = None
    footer: str | None = None


async def send_alert(
    content: str | None = None,
    embed: DiscordEmbed | None = None,
) -> bool:
    """Send a message to the configured Discord webhook."""
    settings = get_settings()
    if not settings.discord_webhook_url:
        log.warning("discord_webhook_url not configured — skipping alert")
        return False

    payload: dict = {"content": content, "embeds": []}
    if embed:
        embed_dict = embed.model_dump(exclude_none=True)
        payload["embeds"].append(embed_dict)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.discord_webhook_url,
                json=payload,
            )
            response.raise_for_status()
            log.info("discord_alert_sent", content=content)
            return True
    except Exception as exc:
        log.error("discord_alert_failed", error=str(exc), content=content)
        return False


def send_digest_sync(
    discovered: int,
    outreach_sent: int,
    replies: int,
    interested: int,
    in_crm: int,
) -> bool:
    """Send a daily digest. Call from sync context (cron scripts)."""
    import asyncio
    content = (
        f"📊 **Lead Gen Daily Digest**\n"
        f"- Discovered: {discovered}\n"
        f"- Outreach sent: {outreach_sent}\n"
        f"- Replies: {replies}\n"
        f"- Interested: {interested}\n"
        f"- In CRM: {in_crm}"
    )
    return asyncio.run(send_alert(content=content))