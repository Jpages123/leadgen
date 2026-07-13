"""Write client.ts + brand.ts into the cloned template."""
from __future__ import annotations

from pathlib import Path


def write_config(
    slug: str,
    client_ts_file: Path,
    brand_ts_file: Path,
    build_dir: str = "/tmp/cc_mockups",
) -> dict:
    """Copy the LLM-generated client.ts and brand.ts into the template.

    The TypeScript skill writes these files via pi's native `write` tool first,
    passing file paths to this helper. We copy (not move) so the LLM can re-edit
    during iteration loops.
    """
    project = Path(build_dir) / slug
    config_dir = project / "src" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    if not client_ts_file.exists():
        raise FileNotFoundError(f"client_ts_file not found: {client_ts_file}")
    if not brand_ts_file.exists():
        raise FileNotFoundError(f"brand_ts_file not found: {brand_ts_file}")

    # Validate TypeScript content (basic sanity)
    client_content = client_ts_file.read_text()
    brand_content = brand_ts_file.read_text()

    if "export const client" not in client_content:
        raise ValueError("client.ts is missing `export const client` declaration")
    if "export const brand" not in brand_content:
        raise ValueError("brand.ts is missing `export const brand` declaration")

    # Check for the Session 10 bug: unquoted logo path
    import re
    # Match `logo: <value>,` and check if value is unquoted
    logo_match = re.search(r"^\s*logo:\s*(.+?),", client_content, re.MULTILINE)
    if logo_match:
        logo_value = logo_match.group(1).strip()
        # A properly quoted string starts with " or '
        if not (logo_value.startswith('"') or logo_value.startswith("'") or logo_value in ("null", "undefined")):
            raise ValueError(
                f"client.ts has unquoted logo path (Session 10 regression): "
                f"`logo: {logo_value}` — must be a quoted string or null"
            )

    (config_dir / "client.ts").write_text(client_content)
    (config_dir / "brand.ts").write_text(brand_content)

    return {
        "client_ts_bytes": len(client_content),
        "brand_ts_bytes": len(brand_content),
        "wrote_to": str(config_dir),
    }
