"""Generate marketing copy for mockup sites via the Pi LLM harness.

Calls `pi -p <prompt>` as a subprocess (MiniMax model via Pi harness).
Returns structured JSON: tagline + service descriptions for a given business.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger(__name__)

_VERTICAL_SERVICE_DEFAULTS: dict[str, list[str]] = {
    "trades": ["Emergency Repairs", "Installations", "Maintenance", "Inspections", "Residential", "Commercial"],
    "plumbing": ["Emergency Plumbing", "Geyser Installation", "Pipe Repairs", "Drain Cleaning", "Bathroom Renovations", "Water Leak Detection"],
    "electrical": ["Electrical Installations", "Fault Finding", "Distribution Boards", "Solar & Backup", "Lighting", "Certificate of Compliance"],
    "beauty": ["Hair Styling", "Colour & Highlights", "Treatments", "Nail Services", "Makeup", "Bridal Packages"],
    "cleaning": ["Residential Cleaning", "Commercial Cleaning", "Deep Cleaning", "Post-Construction", "Carpet Cleaning", "Window Cleaning"],
    "photography": ["Wedding Photography", "Portraits", "Corporate Events", "Family Sessions", "Product Photography", "Photo Editing"],
    "automotive": ["Car Wash", "Full Valet", "Interior Detailing", "Paint Protection", "Tyre & Rim Clean", "Engine Bay Clean"],
    "event_planning": ["Event Planning", "Venue Coordination", "Decor & Styling", "Catering Arrangements", "Day-of Coordination", "Corporate Events"],
    "construction": ["Residential Builds", "Renovations", "Structural Repairs", "Tiling & Flooring", "Roofing", "Free Quotations"],
}

_VERTICAL_TAGLINE_DEFAULTS: dict[str, str] = {
    "trades": "Fast, Reliable Service You Can Trust",
    "plumbing": "Your Local Plumbing Expert",
    "electrical": "Safe, Certified Electrical Solutions",
    "beauty": "Look & Feel Your Best",
    "cleaning": "Spotless Results, Every Time",
    "photography": "Capturing Moments That Last a Lifetime",
    "automotive": "Your Car Deserves the Best",
    "event_planning": "Unforgettable Events, Flawlessly Executed",
    "construction": "Built to Last, Built for You",
}


def _resolve_pi() -> str:
    """Locate the pi executable.

    Uses os.access() instead of Path.exists() because the latter raises
    PermissionError on inaccessible paths (e.g. /root owned by another user),
    which would mask the working candidate further down the list.
    """
    found = shutil.which("pi")
    if found:
        return found
    candidates = [
        Path.home() / ".npm-global/bin/pi",
        Path("/usr/local/bin/pi"),
        Path("/root/.npm-global/bin/pi"),
        Path.home() / ".nvm/versions/node/v23.1.0/bin/pi",
    ]
    for candidate in candidates:
        try:
            if os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    raise FileNotFoundError("Cannot locate pi CLI")


def _extract_json_object(raw: str) -> dict:
    """Robustly extract the first valid JSON object from `raw`.

    The pi (MiniMax) model is now in "thinking" mode and wraps its output
    in <think>...</think> blocks. The previous extraction used naive
    str.find("{") / str.rfind("}") slicing, which broke when:
      - The think block contained { or } characters (model's example code,
        curly-quote reasoning, etc.)
      - The model appended trailing commentary after the JSON object

    This implementation:
      1. Strips <think>...</think> blocks first so the search starts on
         the actual output, not the chain-of-thought
      2. Uses json.JSONDecoder().raw_decode() which stops at the end of
         the first valid JSON object — naturally tolerating trailing text
      3. Tries each '{' as a potential start, recovering from braces that
         appear inside strings or in example code the model emitted
      4. Validates the result is a dict (not array, not scalar)

    Raises:
        ValueError: if no valid JSON object can be recovered.
    """
    # Step 1: strip the thinking block(s) if present. The model emits
    # <think>...</think> as a single contiguous block at the start.
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Step 2: locate every '{' and try to decode from there. Use raw_decode
    # which returns (value, end_pos) and correctly tolerates trailing text.
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise ValueError(
        f"No valid JSON object found in pi output "
        f"(len={len(raw)}, cleaned_len={len(cleaned)})"
    )


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    pi_path = _resolve_pi()
    pi_bin_dir = str(Path(pi_path).parent)
    npm_global_bin = "/root/.npm-global/bin"
    existing_path = env.get("PATH", "")
    env["PATH"] = ":".join(p for p in [pi_bin_dir, npm_global_bin, existing_path] if p)
    env["TERM"] = "xterm-256color"
    return env


def generate_copy(
    business_name: str,
    vertical: str,
    city: str | None = None,
    existing_services: list[str] | None = None,
    existing_tagline: str | None = None,
    timeout: int = 60,
) -> dict:
    """Call Pi to generate a tagline + 6 service descriptions.

    Returns dict with keys:
        tagline: str
        services: list[dict]  — [{title, description}, ...]

    Falls back to vertical defaults on any failure so the pipeline never blocks.
    """
    location = f"in {city}, South Africa" if city else "in South Africa"
    services_hint = ", ".join(existing_services) if existing_services else "(infer from vertical)"

    prompt = (
        "You are writing marketing copy for a small SA business website mockup.\n"
        f"Business: {business_name}\n"
        f"Vertical: {vertical}\n"
        f"Location: {location}\n"
        f"Known services: {services_hint}\n\n"
        "Return ONLY valid JSON (no markdown, no explanation) with this exact shape:\n"
        "{\n"
        '  "tagline": "Short punchy tagline (max 8 words)",\n'
        '  "services": [\n'
        '    {"title": "Service Name", "description": "One sentence, 10-15 words max"},\n'
        "    (exactly 6 items)\n"
        "  ]\n"
        "}\n"
        "Tagline should be specific to the business if possible. "
        "Descriptions must be punchy, SA-friendly, no fluff. "
        "Output JSON only - no other text."
    )

    # Use /app if running inside Docker, otherwise host project root
    _docker_app = Path("/app")
    cwd = str(_docker_app) if _docker_app.exists() else str(Path.home() / "installedApps" / "leadgen" / "cc-leadgen")

    try:
        pi_path = _resolve_pi()
        result = subprocess.run(
            [pi_path, "-p", prompt],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_build_env(),
            timeout=timeout,
        )

        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"pi exited {result.returncode}: {result.stderr[:200]}")

        data = _extract_json_object(result.stdout)

        # Defensive normalisation: a partial/garbled LLM response might
        # still parse but produce wrong types. Validate before using.
        raw_tagline = data.get("tagline")
        if not isinstance(raw_tagline, str) or not raw_tagline.strip():
            raw_tagline = None
        tagline = (
            (raw_tagline.strip() if raw_tagline else None)
            or existing_tagline
            or _VERTICAL_TAGLINE_DEFAULTS.get(vertical, "Quality Service You Can Trust")
        )
        # Coerce services to a clean list of {title, description} dicts
        services: list[dict] = []
        raw_services = data.get("services")
        if isinstance(raw_services, list):
            for item in raw_services:
                if not isinstance(item, dict):
                    continue
                title = item.get("title")
                desc = item.get("description")
                if not isinstance(title, str) or not isinstance(desc, str):
                    continue
                # Strip control chars that would break client.ts
                title = "".join(c for c in title if c.isprintable() or c == " ").strip()
                desc = "".join(c for c in desc if c.isprintable() or c == " ").strip()
                if not title or not desc:
                    continue
                services.append({"title": title, "description": desc})

        if len(services) < 6:
            defaults = _VERTICAL_SERVICE_DEFAULTS.get(vertical, [])
            for name in defaults[len(services):6]:
                services.append({
                    "title": name,
                    "description": f"Professional {name.lower()} tailored to your needs.",
                })

        log.info("copy_generated", business=business_name, vertical=vertical, tagline=tagline)
        return {"tagline": tagline, "services": services[:6]}

    except Exception as exc:
        log.warning("copy_generation_failed", business=business_name, error=str(exc))
        return _fallback_copy(vertical, existing_tagline, existing_services)


def _fallback_copy(vertical: str, tagline: str | None, services: list[str] | None) -> dict:
    """Return hardcoded defaults when Pi fails — pipeline must never block."""
    t = tagline or _VERTICAL_TAGLINE_DEFAULTS.get(vertical, "Quality Service You Can Trust")
    svc_names = services or _VERTICAL_SERVICE_DEFAULTS.get(vertical, ["Our Services"] * 6)
    return {
        "tagline": t,
        "services": [
            {"title": name, "description": f"Professional {name.lower()} delivered with care and expertise."}
            for name in svc_names[:6]
        ],
    }
