"""Run inside the MCP image with Compose's environment; never print secrets."""
import os
import ipaddress
from memu.config import MCPConfig
from memu.core_client import RemoteCore, endpoint
from memu.environment import environment_config
from memu.http_transport import load_tokens


def validate(environment):
    tokens = load_tokens(environment)
    if any('REPLACE' in token.upper() or 'CHANGE_ME' in token.upper() for token in tokens.values()):
        raise ValueError('Replace token placeholders with existing core identity tokens')
    if environment.get('MEMU_MCP_IDENTITY', 'agent1') not in tokens:
        raise ValueError('MEMU_MCP_IDENTITY must be one of the configured identities')
    forbidden = [key for key in environment if key.startswith(('MLLM_', 'EMB_', 'MEMU_ACCESS_'))
                 or key in {'MEMU_MEMORY_ROOT', 'MEMU_DATABASE_URL', 'MEMU_ENV_FILE'}]
    if forbidden:
        raise ValueError('Use the frontend-only mcp.env template, not the core .env')
    selected = {key: value for key, value in environment.items() if key.startswith('MEMU_MCP_')}
    settings = MCPConfig.model_validate(environment_config(selected).get('mcp', {}))
    if settings.host != '127.0.0.1' or settings.port != 5071:
        raise ValueError('This Compose deployment requires MCP on 127.0.0.1:5071')
    public = endpoint(environment['MEMU_MCP_URL'])
    if (public.scheme != 'https' or public.path != '/mcp' or public.port not in {None, 443}
            or public.hostname not in settings.allowed_hosts):
        raise ValueError('Set a matching public HTTPS hostname and /mcp URL')
    if public.hostname in {'example.com', 'localhost'} or public.hostname.endswith(('.example.com', '.invalid')):
        raise ValueError('Replace the example public hostname in .env')
    core = RemoteCore(environment['MEMU_CORE_URL'], tokens,
                      environment.get('MEMU_CORE_TIMEOUT_SECONDS', '600'))
    try:
        core_ip = ipaddress.ip_address(core.url.hostname)
        tailnet_ip = (core_ip in ipaddress.ip_network('100.64.0.0/10') or
                      core_ip in ipaddress.ip_network('fd7a:115c:a1e0::/48'))
    except ValueError:
        tailnet_ip = False
    if (core.url.scheme != 'https' and core.url.hostname not in {'127.0.0.1', '::1', 'localhost'}
            and not tailnet_ip):
        raise ValueError('Use a verified Tailscale IP, loopback, or a core HTTPS URL')
    if not 0 < core.timeout < 900:
        raise ValueError('Core timeout must stay below the proxy/client 900-second timeout')


if __name__ == '__main__':
    try:
        validate(dict(os.environ))
    except (KeyError, ValueError, TypeError):
        raise SystemExit('Configuration invalid. Check host, tokens, identity, core URL and timeout; '
                         'use the frontend-only templates. No values were printed.') from None
    print('Frontend configuration valid; core token equality and network reachability still require check.')
