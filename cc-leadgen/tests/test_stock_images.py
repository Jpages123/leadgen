"""Tests for the Pexels stock-photo fallback (Phase P, docs/PHASE_P_PLAN.md).

Covers the failure modes that matter for a fallback tier that must never
crash the mockup build: missing API key, empty search results, network
errors, and undersized/broken downloads. All Pexels calls are mocked — no
real network access.

Run with:
    cd ~/installedApps/leadgen/cc-leadgen && source .venv/bin/activate \\
        && pytest -c /dev/null tests/test_stock_images.py -v \\
            --no-header -p no:cacheprovider
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from app.workers.mockup_helpers import stock_images


def _fake_jpeg_bytes(width=1200, height=800, color=(120, 80, 40)) -> bytes:
    """A real, decodable, non-placeholder-looking JPEG for mocked downloads."""
    img = Image.new("RGB", (width, height), color=color)
    # Add some variation so the placeholder-detector (greyscale range) doesn't
    # flag it as solid-colour.
    for x in range(0, width, 37):
        for y in range(0, height, 41):
            img.putpixel((x, y), (255, 255, 255))
    buf = BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def _fake_search_response(n=3):
    photos = []
    for i in range(n):
        photos.append({
            "src": {"large": f"https://images.pexels.com/photos/{i}/photo-{i}.jpeg"},
            "photographer": f"Photographer {i}",
            "url": f"https://www.pexels.com/photo/{i}/",
            "alt": f"A nice photo {i}",
        })
    return {"photos": photos, "total_results": n}


class _FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _make_client_factory(search_response, image_bytes_by_url=None, raise_on_search=False):
    image_bytes_by_url = image_bytes_by_url or {}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, *args, **kwargs):
            if raise_on_search and url == stock_images.PEXELS_SEARCH_URL:
                raise RuntimeError("network error")
            if url == stock_images.PEXELS_SEARCH_URL:
                return _FakeResponse(json_data=search_response)
            content = image_bytes_by_url.get(url, _fake_jpeg_bytes())
            return _FakeResponse(content=content)

    return _FakeClient


def _settings_with_key(key: str):
    return SimpleNamespace(pexels_api_key=key)


def test_no_api_key_returns_empty(tmp_path):
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"):
        result = stock_images.search_stock_images("plumber south africa", "acme")
    assert result == []


def test_search_failure_returns_empty_not_raises(tmp_path):
    fake_client = _make_client_factory(_fake_search_response(), raise_on_search=True)
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        result = stock_images.search_stock_images("plumber south africa", "acme")
    assert result == []


def test_empty_results_returns_empty(tmp_path):
    fake_client = _make_client_factory({"photos": [], "total_results": 0})
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        result = stock_images.search_stock_images("extremely obscure query", "acme")
    assert result == []


def test_search_success_downloads_and_records_credits(tmp_path):
    fake_client = _make_client_factory(_fake_search_response(n=3))
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        result = stock_images.search_stock_images("event hire marquee", "acme", per_page=3)

    assert len(result) == 3
    for cand in result:
        assert cand.bytes > stock_images.MIN_IMAGE_BYTES
        assert cand.is_placeholder is False
        assert cand.photographer is not None
        from pathlib import Path
        assert Path(cand.local_path).exists()

    # Credits sidecar written for provenance/licence record-keeping.
    cache_dir = tmp_path / ".cache" / "audits" / "acme"
    credits_files = list(cache_dir.glob("acme-stock-credits.json"))
    assert len(credits_files) == 1


def test_undersized_download_is_skipped(tmp_path):
    search_response = _fake_search_response(n=2)
    tiny_url = search_response["photos"][0]["src"]["large"]
    fake_client = _make_client_factory(
        search_response,
        image_bytes_by_url={tiny_url: b"too-small"},
    )
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        result = stock_images.search_stock_images("event hire marquee", "acme", per_page=2)

    # Only the non-tiny candidate should survive.
    assert len(result) == 1


def test_orientation_and_per_page_are_clamped(tmp_path):
    fake_client = _make_client_factory(_fake_search_response(n=1))
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        # Invalid orientation falls back to landscape; per_page is clamped to MAX_PER_PAGE.
        result = stock_images.search_stock_images(
            "corporate gala", "acme", orientation="diagonal", per_page=999,
        )
    assert isinstance(result, list)


def test_json_wrapper_shape_on_success(tmp_path):
    fake_client = _make_client_factory(_fake_search_response(n=2))
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("key123")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"), \
         patch("httpx.Client", fake_client):
        result = stock_images.search_stock_images_json("wedding decor", "acme", per_page=2)

    assert result["ok"] is True
    assert result["count"] == 2
    assert len(result["candidates"]) == 2
    for c in result["candidates"]:
        assert "local_path" in c and "photographer" in c and "is_placeholder" in c


def test_json_wrapper_shape_on_failure(tmp_path):
    with patch.object(stock_images, "get_settings", return_value=_settings_with_key("")), \
         patch.object(stock_images, "mockup_project_dir", return_value=tmp_path / ".cache" / "mockups" / "acme"):
        result = stock_images.search_stock_images_json("wedding decor", "acme")

    assert result["ok"] is False
    assert "error" in result
