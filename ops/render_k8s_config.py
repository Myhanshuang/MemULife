"""Render deployment .env into role-scoped Kubernetes settings; secrets never go to stdout."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

from dotenv import dotenv_values


def resources(values):
    required = ('POSTGRES_PASSWORD', 'MEMU_OWNER_TOKEN', 'MEMU_AGENT_TOKEN',
                'MLLM_BASE_URL', 'MLLM_MODEL_NAME', 'EMB_BASE_URL', 'EMB_MODEL_NAME', 'EMB_MODEL_REVISION')
    for name in required:
        if not values.get(name) or values[name].startswith('GENERATE_'):
            raise ValueError(f'Configure {name} first')
    for name in required[:3]:
        if not re.fullmatch(r'[A-Za-z0-9_-]{24,}', values[name]):
            raise ValueError(f'{name} must use at least 24 URL-safe characters; use ops/bootstrap.py')
    if values['MEMU_OWNER_TOKEN'] == values['MEMU_AGENT_TOKEN']:
        raise ValueError('Owner and agent tokens must differ')
    tokens = json.dumps({'owner': values['MEMU_OWNER_TOKEN'], 'agent1': values['MEMU_AGENT_TOKEN']})
    access = {'MEMU_ACCESS_WHITELIST': '{"owner":["read","write"],"agent1":["read","write"]}',
              'MEMU_ACCESS_OWNERS': '["owner"]'}
    dsn = 'postgresql://memu:' + quote(values['POSTGRES_PASSWORD'], safe='') + '@memu-postgres:5432/memu?connect_timeout=5'
    core = {**access, 'MEMU_CORE_HOST': '0.0.0.0', 'MEMU_MEMORY_ROOT': '/data/memory',
            'MEMU_TIMEZONE': values.get('MEMU_TIMEZONE', 'Asia/Shanghai'),
            'MEMU_CORE_ALLOWED_HOSTS': '["localhost","127.0.0.1","memu-core"]',
            'MEMU_CORE_MAX_BODY_BYTES': '100663296'}
    for name in ('MLLM_BASE_URL', 'MLLM_MODEL_NAME', 'MLLM_PROTOCOL', 'MLLM_TIMEOUT_SECONDS',
                 'EMB_BACKEND', 'EMB_BASE_URL', 'EMB_MODEL_NAME', 'EMB_MODEL_REVISION'):
        if values.get(name):
            core[name] = values[name]
    frontend = {'MEMU_CORE_URL': 'http://memu-core:5072', 'MEMU_CORE_TIMEOUT_SECONDS': '600'}
    mcp = {**frontend, 'MEMU_MCP_HOST': '0.0.0.0',
           'MEMU_MCP_ALLOWED_HOSTS': values.get('MEMU_MCP_ALLOWED_HOSTS', '["localhost","127.0.0.1","memu-mcp"]'),
           'MEMU_MCP_ALLOWED_ORIGINS': values.get('MEMU_MCP_ALLOWED_ORIGINS', '[]'),
           'MEMU_MCP_WRITE_ENABLED': values.get('MEMU_MCP_WRITE_ENABLED', 'true')}
    acquisition = {**frontend, **access, 'MEMU_ACQUISITION_HOST': '0.0.0.0',
                   'MEMU_ACQUISITION_ROOT': '/data/acquisition',
                   'MEMU_ACQUISITION_ALLOWED_HOSTS': values.get('MEMU_ACQUISITION_ALLOWED_HOSTS', '["localhost","127.0.0.1","memu-acquisition"]'),
                   'MEMU_ACQUISITION_ALLOWED_ORIGINS': values.get('MEMU_ACQUISITION_ALLOWED_ORIGINS', '["http://localhost:5073","http://127.0.0.1:5073"]')}
    groups = {'memu-core-config': (False, core), 'memu-mcp-config': (False, mcp),
              'memu-acquisition-config': (False, acquisition),
              'memu-auth': (True, {'MEMU_MCP_TOKENS': tokens}),
              'memu-core-secrets': (True, {'MEMU_DATABASE_URL': dsn,
                  'MLLM_API_KEY': values.get('MLLM_API_KEY', ''), 'EMB_API_KEY': values.get('EMB_API_KEY', '')}),
              'memu-acquisition-secrets': (True, {'MEMU_DATABASE_URL': dsn}),
              'memu-postgres-auth': (True, {'POSTGRES_PASSWORD': values['POSTGRES_PASSWORD']})}
    items = []
    for name, (secret, data) in groups.items():
        item = {'apiVersion': 'v1', 'kind': 'Secret' if secret else 'ConfigMap',
                'metadata': {'name': name, 'namespace': 'memu'}, 'stringData' if secret else 'data': data}
        if secret:
            item['type'] = 'Opaque'
        items.append(item)
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    value = resources(dotenv_values(args.env_file, interpolate=False))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation also rejects symlinks and accidental secret overwrite.
    with args.output.open('x', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f'Rendered {args.output} (0600, contains secrets).')


if __name__ == '__main__':
    main()
