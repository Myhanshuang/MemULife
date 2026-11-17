"""Focused deployment boundaries; no real source credentials or memory writes."""
from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
from memu.acquisition.cli import acquisition_settings

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_and_role_scoped_configuration(tmp_path):
    from dotenv import dotenv_values
    path = tmp_path / '.env'
    subprocess.run([sys.executable, str(ROOT / 'ops/bootstrap.py'), '--output', str(path)], check=True)
    values = dotenv_values(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert values['MEMU_OWNER_TOKEN'] != values['MEMU_AGENT_TOKEN']
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(ROOT / 'ops/bootstrap.py'), '--output', str(path)], capture_output=True)
    assert result.returncode and path.read_bytes() == before
    spec = importlib.util.spec_from_file_location('deployment', ROOT / 'ops/render_k8s_config.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    values.update(MLLM_API_KEY='fixture-model-secret', EMB_API_KEY='fixture-embedding-secret')
    objects = {v['metadata']['name']: v for v in module.resources(values)['items']}
    assert 'MEMU_DATABASE_URL' not in objects['memu-mcp-config']['data']
    assert 'MLLM_API_KEY' not in objects['memu-acquisition-secrets']['stringData']
    assert objects['memu-core-secrets']['stringData']['MLLM_API_KEY'] == 'fixture-model-secret'
    assert json.loads(objects['memu-auth']['stringData']['MEMU_MCP_TOKENS'])['owner'] == values['MEMU_OWNER_TOKEN']
    core = (ROOT / 'deploy/k8s/base/core.yaml').read_text().split('containers:')[0]
    assert 'memu-core-secrets' in core
    with pytest.raises(ValueError):
        module.resources({**values, 'MEMU_OWNER_TOKEN': values['MEMU_AGENT_TOKEN']})


def test_acquisition_proxy_hosts_and_https_origin():
    args = Namespace(host=None, port=None, allowed_host=[])
    config = acquisition_settings(args, {'MEMU_ACQUISITION_HOST': '0.0.0.0',
        'MEMU_ACQUISITION_ALLOWED_HOSTS': '["manage.example.test"]',
        'MEMU_ACQUISITION_ALLOWED_ORIGINS': '["https://manage.example.test"]'})
    assert 'manage.example.test' in config.allowed_hosts
    assert config.allowed_origins == ['https://manage.example.test']
    with pytest.raises(ValueError):
        acquisition_settings(args, {'MEMU_ACQUISITION_ALLOWED_HOSTS': '"*"'})
