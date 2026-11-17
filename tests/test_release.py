import hashlib
import json
from pathlib import Path
import runpy
import tarfile

import pytest

from ops.package_release import PUBLIC_FILES, build_release, source_files


def public_tree(root):
    for name in PUBLIC_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('public fixture\n')
    (root / 'pyproject.toml').write_text('[project]\nname="memulife"\nversion="1.3.0"\n')
    (root / 'memu').mkdir()
    (root / 'memu/__init__.py').write_text('__version__ = "1.3.0"\n')


def test_release_excludes_private_files_and_clears_archive_ownership(tmp_path):
    root = tmp_path / 'source'
    public_tree(root)
    private = [ '.env', '.env.production', 'deploy/remote-mcp/mcp.env',
                'deploy/remote-mcp/.env', 'deploy/remote-mcp/core-host/ssh_config',
                '.ssh/id_ed25519', '.local/postgres/data/private', 'memory/source.txt',
                'reports/runtime.json', 'ops/postgres/pg_ident.conf', 'ops/memu-postgres.service',
                'RAW/design.md', 'memory-system-design-v1.md', '.git/config', '.idea/workspace.xml']
    for name in private:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('PRIVATE_FIXTURE_DO_NOT_EXPORT')
    output = tmp_path / 'public.tar.gz'
    count = build_release(root, output)
    with tarfile.open(output) as archive:
        members = archive.getmembers()
        exported = {member.name.split('/', 1)[1]: archive.extractfile(member).read() for member in members}
        assert all(member.isfile() and member.uid == member.gid == 0 and not member.uname and not member.gname
                   for member in members)
    assert not set(private).intersection(exported)
    assert all(b'PRIVATE_FIXTURE_DO_NOT_EXPORT' not in data for data in exported.values())
    manifest = json.loads(exported.pop('BUNDLE-MANIFEST.json'))
    assert len(exported) == count
    assert manifest['files'] == {name: hashlib.sha256(data).hexdigest() for name, data in exported.items()}
    assert output.with_name(output.name + '.sha256').read_text().split()[0] == hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        build_release(root, output)


@pytest.mark.parametrize('directory', [False, True])
def test_release_rejects_symlink_inputs(tmp_path, directory):
    root = tmp_path / 'source'
    public_tree(root)
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'payload.py').write_text('private_material = True')
    if directory:
        (root / 'memu/__init__.py').unlink()
        (root / 'memu').rmdir()
        (root / 'memu').symlink_to(outside, target_is_directory=True)
    else:
        (root / 'memu/payload.py').symlink_to(outside / 'payload.py')
    with pytest.raises(ValueError, match='symlink'):
        source_files(root)


def frontend_environment():
    return {'MEMU_MCP_TOKENS': json.dumps({'agent1': 'fixture-$-token-' + 'a' * 32}),
            'MEMU_MCP_IDENTITY': 'agent1', 'MEMU_MCP_HOST': '127.0.0.1', 'MEMU_MCP_PORT': '5071',
            'MEMU_MCP_URL': 'https://memory.example.test/mcp',
            'MEMU_MCP_ALLOWED_HOSTS': '["127.0.0.1","memory.example.test"]',
            'MEMU_CORE_URL': 'http://127.0.0.1:15072', 'MEMU_CORE_TIMEOUT_SECONDS': '600'}


def validator():
    path = Path(__file__).resolve().parents[1] / 'deploy/remote-mcp/validate.py'
    return runpy.run_path(str(path))['validate']


def test_remote_config_accepts_frontend_only_identity_subset():
    validator()(frontend_environment())


@pytest.mark.parametrize('url', ['http://100.64.0.10:15072', 'http://[fd7a:115c:a1e0::1]:15072'])
def test_remote_config_accepts_tailnet_ip(url):
    validator()({**frontend_environment(), 'MEMU_CORE_URL': url})


@pytest.mark.parametrize('changes', [
    {'MEMU_MCP_TOKENS': '{}'},
    {'MEMU_MCP_TOKENS': json.dumps({'agent1': 'REPLACE_WITH_REAL_TOKEN_FROM_CORE'})},
    {'MEMU_MCP_IDENTITY': 'owner'},
    {'MEMU_MCP_HOST': '0.0.0.0'},
    {'MEMU_CORE_URL': 'http://core.example.test:5072'},
    {'MEMU_CORE_URL': 'http://100.128.0.1:15072'},
    {'MEMU_CORE_URL': 'http://192.168.1.10:15072'},
    {'MEMU_CORE_URL': 'http://[fd00::1]:15072'},
    {'MEMU_CORE_TIMEOUT_SECONDS': '900'},
    {'MEMU_DATABASE_URL': 'fixture-db'},
    {'MLLM_API_KEY': 'fixture-secret'},
    {'MEMU_MCP_URL': 'https://memory.example.com/mcp',
     'MEMU_MCP_ALLOWED_HOSTS': '["memory.example.com"]'},
])
def test_remote_config_rejects_unconfigured_or_leaking_environment(changes):
    with pytest.raises(ValueError):
        validator()({**frontend_environment(), **changes})
