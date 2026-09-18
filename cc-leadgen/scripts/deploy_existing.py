from app.utils.cloudflare_deploy import deploy_pages, create_dns_record
from app.config import get_settings

s = get_settings()
url = deploy_pages(
    dist_dir='/tmp/cc_mockups/limelight-event-hire/dist',
    project_name='limelight-event-hire-demo',
    cf_token=s.cloudflare_api_token,
)
print('pages url:', url)
demo = create_dns_record(slug='limelight-event-hire', cf_token=s.cloudflare_api_token)
print('demo url:', demo)
