"""Build a source distribution with a file manifest and SHA-256 checksum."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tomllib


PUBLIC_FILES = (
    'README.md', 'compose.yaml', 'AGENTS.md', 'pyproject.toml', 'Dockerfile', '.dockerignore', '.gitignore', '.env.example',
    'ops/install_services.py', 'ops/render_k8s_config.py', 'ops/services.sh', 'ops/memu.sh',
    'ops/requirements-embedding.txt', 'ops/package_release.py', 'ops/bootstrap.py',
    'examples/mcp-http.json', 'examples/acceptance-cases.json',
    'examples/memulife-source.json', 'examples/memulife-selection.json',
    'examples/acquisition-files.json', 'examples/acquisition-browser.json', 'docs/acquisition.md', 'docs/browser-content.md',
    'docs/reliability.md', 'docs/local-ai-content.md', 'docs/memory-revisions.md',
    'docs/project-overview.md', 'docs/core-design-principles.md', 'docs/quick-start.md', 'docs/architecture-review.md',
    'docs/assets/architecture.png', '.github/workflows/ci.yml',
    'browser-extension/package.json', 'browser-extension/package-lock.json', 'browser-extension/tsconfig.json',
    'browser-extension/manifest.json', 'browser-extension/options.html', 'browser-extension/options.css',
    'browser-extension/README.md',
    'tests/ui/package.json', 'tests/ui/package-lock.json',
    'tests/acquisition_ui_fixture.py',
    'deploy/README.md', 'deploy/compose.env.example', 'deploy/postgres-init.sql',
    'deploy/remote-mcp/README.md', 'deploy/remote-mcp/compose.yaml',
    'deploy/remote-mcp/.env.example', 'deploy/remote-mcp/mcp.env.example',
    'deploy/remote-mcp/Caddyfile', 'deploy/remote-mcp/deploy.sh', 'deploy/remote-mcp/validate.py',
    'deploy/remote-mcp/client.example.json',
    'deploy/remote-mcp/core-host/tailscale-serve.sh',
    'deploy/remote-mcp/core-host/tailnet.env.example',
    'deploy/remote-mcp/core-host/memu-core-tailnet.service.example',
)
PUBLIC_GLOBS = ('memu/*.py', 'memu/tasks/*.py', 'memu/tasks/prompts/*.md',
                'memu/sql/*.sql', 'tests/test_*.py', 'deploy/k8s/**/*.yaml',
                'memu/acquisition/**/*.py', 'memu/acquisition/**/*.json',
                'memu/acquisition/ui/**/*.js', 'memu/acquisition/ui/*.css', 'memu/acquisition/ui/*.html',
                'browser-extension/src/**/*.ts', 'browser-extension/tools/*.mjs',
                'browser-extension/tools/*.ts', 'browser-extension/tests/*.mjs', 'tests/ui/*.mjs',
                'native/wayland/*.py', 'native/wayland/*.toml', 'native/windows/*.json',
                'native/windows/CaptureCore/*.cs', 'native/windows/CaptureCore/*.csproj',
                'native/windows/CaptureHelper/*.cs', 'native/windows/CaptureHelper/*.csproj',
                'native/windows/CaptureHelper/*.json', 'native/windows/CaptureHelper/*.manifest',
                'native/windows/tests/*.py', 'native/windows/tests/*.ps1',
                'native/windows/tests/CoreFixtures/*.cs', 'native/windows/tests/CoreFixtures/*.csproj')


def source_files(root):
    root = Path(root).resolve()
    paths = {root / name for name in PUBLIC_FILES}
    for pattern in PUBLIC_GLOBS:
        paths.update(root.glob(pattern))
    for path in paths:
        if not path.is_file():
            raise ValueError(f'Missing release input: {path.relative_to(root)}')
        if path.is_symlink():
            raise ValueError('Release inputs may not be symlinks')
        relative = path.relative_to(root)
        if any((root / parent).is_symlink() for parent in relative.parents):
            raise ValueError('Release input directories may not be symlinks')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Release input escapes source root')
    return sorted(paths)


def build_release(root, output):
    root, output = Path(root).resolve(), Path(output)
    # Collect the explicitly selected distribution inputs.
    files = {str(path.relative_to(root)): path.read_bytes() for path in source_files(root)}
    project = tomllib.loads(files['pyproject.toml'].decode())['project']
    prefix = f"{project['name']}-{project['version']}"
    if '/' in prefix or '\\' in prefix or prefix in {'.', '..'}:
        raise ValueError('Unsafe package name/version')
    manifest = {'name': project['name'], 'version': project['version'],
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files['BUNDLE-MANIFEST.json'] = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally overwriting another artifact.
    with output.open('xb') as raw:
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            for name, data in sorted(files.items()):
                item = tarfile.TarInfo(prefix + '/' + name)
                item.size, item.mode, item.mtime = len(data), 0o644, 0
                item.uid = item.gid = 0
                item.uname = item.gname = ''
                archive.addfile(item, io.BytesIO(data))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + '.sha256').write_text(f'{checksum}  {output.name}\n')
    return len(manifest['files'])


def main():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=root / 'dist' / f"{project['name']}-{project['version']}.tar.gz")
    args = parser.parse_args()
    try:
        count = build_release(root, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Release failed: {error}\n')
    print(f'Exported {count} public files to {args.output}; manifest and SHA-256 included.')


if __name__ == '__main__':
    main()
