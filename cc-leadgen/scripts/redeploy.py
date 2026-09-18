"""Rebuild + redeploy Limelight after Footer.astro patch."""
import shutil
from pathlib import Path

proj = Path('/tmp/cc_mockups/limelight-event-hire')
if proj.exists():
    shutil.rmtree(proj)
print(f"cleaned {proj}")
