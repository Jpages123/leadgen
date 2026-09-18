from app.config import get_settings
from app.utils.cloudflare_deploy import list_pages_projects
projs = list_pages_projects()
for p in projs[:30]:
    print(p['name'])
print(f"--- total {len(projs)}")
