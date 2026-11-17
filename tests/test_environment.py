import json

import httpx
import pytest

from memu.config import Config
from memu.environment import read_environment, repository_root
from memu.models import HTTPMLLM


def test_dotenv_precedence_paths_and_literal_credentials(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    secret = 'fixture-only-$HOME-${TOKEN}-$(touch never-created)-`echo no`-#tag'
    env_file.write_text('\n'.join([
        'MLLM_BASE_URL=http://fixture.invalid/v1', 'MLLM_MODEL_NAME=dotenv-model',
        'MLLM_API_KEY=' + json.dumps(secret), 'EMB_API_KEY=',
        'MEMU_MEMORY_ROOT="data with spaces"', 'EMB_MODEL_PATH=./weights',
        'MEMU_ACCESS_WHITELIST=\'{"owner":["read","write"]}\'',
        'MEMU_ACCESS_OWNERS=\'["owner"]\'',
    ]))
    environment = read_environment(env_file=env_file, environ={
        'MLLM_MODEL_NAME': 'process-model', 'MEMU_DATABASE_URL': 'dbname=isolated-smoke'})
    config = Config.from_environment(environment)
    assert config.models.mllm.model == 'process-model'
    assert environment['MEMU_DATABASE_URL'] == 'dbname=isolated-smoke'
    assert config.models.embedding.api_key_env is None
    assert repository_root(environment=environment) == tmp_path / 'data with spaces'
    assert repository_root(tmp_path / 'override', environment=environment) == tmp_path / 'override'
    assert config.models.embedding.model_path == str(tmp_path / 'weights')
    monkeypatch.setenv('MLLM_API_KEY', environment['MLLM_API_KEY'])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        model = HTTPMLLM(config.models.mllm, client=client)
        assert model.headers['Authorization'] == 'Bearer ' + secret
    assert secret not in repr(config.model_dump())
    assert not (tmp_path / 'never-created').exists()
    for malformed in ['MLLM_API_KEY="unfinished', 'MLLM_API_KEY=a\nMLLM_API_KEY=b', 'MLLM_API_KEY']:
        env_file.write_text(malformed)
        with pytest.raises(ValueError):
            read_environment(env_file=env_file, environ={})



def test_cli_initializes_from_env_without_yaml_or_models(tmp_path, monkeypatch):
    import os
    from memu import cli
    env_file = tmp_path / '.env'
    env_file.write_text('MEMU_MEMORY_ROOT=./repository\nMEMU_TIMEZONE=Asia/Shanghai\nMLLM_API_KEY=fixture-secret-only\n')
    monkeypatch.setattr(os, 'environ', {'MEMU_ENV_FILE': str(env_file)})
    assert cli.main(['init']) == 0
    root = tmp_path / 'repository'
    assert (root / '.meta/catalog.tsv').is_file()
    assert not (root / '.meta/config.yaml').exists()
    assert all('fixture-secret-only' not in path.read_text() for path in root.rglob('*') if path.is_file())
