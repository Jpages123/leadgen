"""Meta Graph API calls for Facebook Page photos + Instagram media publishing.

Flow for one post:
  POST /{page_id}/photos            → fb_post_id
  POST /{ig_user_id}/media          → ig_container_id
  GET  /{container}?fields=status_code  (poll until FINISHED)
  POST /{ig_user_id}/media_publish  → ig_media_id
  GET  /{media}?fields=permalink    → ig_permalink
  POST /{fb_post_id}/comments       → fb_comment_id
  POST /{ig_media_id}/comments      → ig_comment_id

The page access token is always sent as form data / a query param and is
never included in raised error messages.
"""
from __future__ import annotations

import time

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
HTTP_TIMEOUT_S = 30
CONTAINER_POLL_INTERVAL_S = 3
CONTAINER_POLL_MAX_S = 60


class MetaError(RuntimeError):
    """Graph API returned an error payload or the request failed."""


def _base(settings) -> str:
    return f"{GRAPH_BASE}/{settings.meta_graph_version}"


def _check(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if resp.status_code >= 400 or err:
        msg = (err or {}).get("message") or resp.text[:200]
        raise MetaError(f"graph {resp.status_code}: {str(msg)[:300]}")
    return data


def _post(client: httpx.Client, settings, path: str, **form) -> dict:
    form["access_token"] = settings.meta_page_access_token
    try:
        resp = client.post(f"{_base(settings)}{path}", data=form, timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise MetaError(f"graph request failed: {type(exc).__name__}: {exc}") from exc
    return _check(resp)


def _get(client: httpx.Client, settings, path: str, **params) -> dict:
    params["access_token"] = settings.meta_page_access_token
    try:
        resp = client.get(f"{_base(settings)}{path}", params=params, timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise MetaError(f"graph request failed: {type(exc).__name__}: {exc}") from exc
    return _check(resp)


def fb_post_photo(client, settings, image_url: str, message: str) -> str:
    data = _post(client, settings, f"/{settings.meta_page_id}/photos",
                 url=image_url, message=message)
    post_id = data.get("post_id") or data.get("id")
    if not post_id:
        raise MetaError("FB photo post returned no id")
    return str(post_id)


def ig_create_container(client, settings, image_url: str, caption: str) -> str:
    data = _post(client, settings, f"/{settings.meta_ig_user_id}/media",
                 image_url=image_url, caption=caption)
    cid = data.get("id")
    if not cid:
        raise MetaError("IG media container returned no id")
    return str(cid)


def ig_container_status(client, settings, container_id: str) -> str:
    data = _get(client, settings, f"/{container_id}", fields="status_code")
    return str(data.get("status_code") or "")


def ig_wait_finished(client, settings, container_id: str, *,
                     sleep=time.sleep, max_s: float = CONTAINER_POLL_MAX_S) -> None:
    """Poll container status_code until FINISHED (≤ ~60s). ERROR → raise."""
    waited = 0.0
    while True:
        status = ig_container_status(client, settings, container_id)
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise MetaError(f"IG container {container_id} status=ERROR")
        if waited >= max_s:
            raise MetaError(
                f"IG container {container_id} not FINISHED after {max_s}s (status={status})"
            )
        sleep(CONTAINER_POLL_INTERVAL_S)
        waited += CONTAINER_POLL_INTERVAL_S


def ig_publish(client, settings, container_id: str) -> str:
    data = _post(client, settings, f"/{settings.meta_ig_user_id}/media_publish",
                 creation_id=container_id)
    media_id = data.get("id")
    if not media_id:
        raise MetaError("IG media_publish returned no id")
    return str(media_id)


def ig_permalink(client, settings, media_id: str) -> str | None:
    data = _get(client, settings, f"/{media_id}", fields="permalink")
    return data.get("permalink")


def fb_comment(client, settings, post_id: str, message: str) -> str | None:
    data = _post(client, settings, f"/{post_id}/comments", message=message)
    return data.get("id")


def ig_comment(client, settings, media_id: str, message: str) -> str | None:
    data = _post(client, settings, f"/{media_id}/comments", message=message)
    return data.get("id")
