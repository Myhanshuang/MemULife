"""Acquisition service and explicit source management commands."""
import json
import os
from pathlib import Path
import shlex
import sys

from ..config import Config, CoreConfig
from ..core_client import RemoteCore
from ..http_transport import load_tokens, run_server
from ..persistence import private_save


def add_parser(commands):
    serve = commands.add_parser('acquisition', help='Run source-selection UI and raw acquisition jobs')
    serve.add_argument('--root', type=Path)
    serve.add_argument('--host')
    serve.add_argument('--port', type=int)
    serve.add_argument('--allowed-host', action='append', default=[])
    source = commands.add_parser('sources', help='Manage migrated raw-data adapters')
    sub = source.add_subparsers(dest='source_command', required=True)
    sub.add_parser('adapters')
    manifest = sub.add_parser('manifest')
    manifest.add_argument('adapter')
    for name in ['list', 'add', 'candidates', 'plan', 'submit', 'jobs']:
        child = sub.add_parser(name)
        child.add_argument('--from', dest='identity', required=True)
        child.add_argument('--url')
        if name == 'add':
            child.add_argument('--source', type=Path, required=True)
            child.add_argument('--source-identity')
        if name in {'candidates', 'plan', 'submit'}:
            child.add_argument('--source-id', required=True)
        if name == 'candidates':
            child.add_argument('--parent', default='.')
            child.add_argument('--cursor', default='')
        if name == 'plan':
            child.add_argument('--selection', type=Path, required=True)
        if name == 'submit':
            child.add_argument('--plan', type=Path, required=True)
    native = commands.add_parser('native-host', help='Chromium native messaging transport')
    native.add_argument('--config', required=True, type=Path)
    native.add_argument('origin', nargs='?')
    manifest = sub.add_parser('native-manifest')
    manifest.add_argument('--root', type=Path)
    manifest.add_argument('--source-id', required=True, action='append')
    manifest.add_argument('--extension-id', required=True)
    manifest.add_argument('--output', required=True, type=Path)
    manifest.add_argument('--url')


def acquisition_root(args, environment):
    return Path(getattr(args, 'root', None) or environment.get('MEMU_ACQUISITION_ROOT', 'adapter-state')).expanduser().resolve()


def acquisition_settings(args, environment):
    host = args.host or environment.get('MEMU_ACQUISITION_HOST', '127.0.0.1')
    port = args.port or int(environment.get('MEMU_ACQUISITION_PORT', '5073'))
    configured = json.loads(environment.get('MEMU_ACQUISITION_ALLOWED_HOSTS', '[]'))
    if not isinstance(configured, list):
        raise ValueError('MEMU_ACQUISITION_ALLOWED_HOSTS must be a JSON array')
    allowed = list(dict.fromkeys(['127.0.0.1', 'localhost', host, *configured, *args.allowed_host]))
    origins = (json.loads(environment['MEMU_ACQUISITION_ALLOWED_ORIGINS'])
               if 'MEMU_ACQUISITION_ALLOWED_ORIGINS' in environment else [f'http://{name}:{port}' for name in allowed])
    return CoreConfig(host=host, port=port, allowed_hosts=allowed, allowed_origins=origins,
                      max_body_bytes=24 * 1024 * 1024)


def main(args, environment):
    if args.command == 'native-host':
        from .adapters.native import serve
        return serve(json.loads(args.config.read_bytes()), origin=args.origin)
    if args.command == 'acquisition':
        from .service import AcquisitionService
        from .http_api import AcquisitionHTTPServer
        if not environment.get('MEMU_DATABASE_URL'):
            raise ValueError('MEMU_DATABASE_URL is required for acquisition control')
        tokens = load_tokens(environment)
        core = RemoteCore(environment.get('MEMU_CORE_URL', 'http://127.0.0.1:5072'), tokens,
                          environment.get('MEMU_CORE_TIMEOUT_SECONDS', '600'))
        settings = acquisition_settings(args, environment)
        service = AcquisitionService(acquisition_root(args, environment), core, Config.from_environment(environment),
            workers=int(environment.get('MEMU_ACQUISITION_WORKERS', '2')),
            database_url=environment.get('MEMU_DATABASE_URL'))
        try:
            server = AcquisitionHTTPServer(service, settings, tokens)
            service.start()
            run_server(server)
        finally:
            service.close()
        return 0
    name = args.source_command
    if name == 'adapters':
        from .sources import sources
        result = {'adapters': [{'adapter_id': source.adapter_id, 'profiles': source.profiles, **source.profile()}
                              for source in sources()]}
    elif name == 'manifest':
        from .adapters.registry import manifest
        result = manifest(args.adapter)
    elif name == 'native-manifest':
        result = native_manifest(args, environment)
    else:
        from urllib.parse import urlencode
        tokens = load_tokens(environment)
        if args.identity not in tokens:
            raise ValueError('Unknown identity')
        client = RemoteCore(args.url or environment.get('MEMU_ACQUISITION_URL', 'http://127.0.0.1:5073'), tokens)
        prefix = '/v1/manage/imports/'
        if name in {'list', 'jobs'}:
            result = client.request('GET', prefix + ('sources' if name == 'list' else 'jobs'), args.identity)
        elif name == 'add':
            result = client.request('POST', prefix + 'sources', args.identity, {
                'source': json.loads(args.source.read_bytes()), 'source_identity': args.source_identity})
        elif name == 'candidates':
            result = client.request('GET', prefix + 'sources/' + args.source_id + '/candidates?' +
                                    urlencode({'parent': args.parent, 'cursor': args.cursor}), args.identity)
        elif name == 'plan':
            result = client.request('POST', prefix + 'plan', args.identity, {
                'source_id': args.source_id, 'selection': json.loads(args.selection.read_bytes())})
        else:
            value = json.loads(args.plan.read_bytes())
            if value['source_id'] != args.source_id:
                raise ValueError('Plan source mismatch')
            result = client.request('POST', prefix + 'jobs', args.identity, value)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def native_manifest(args, environment):
    import re
    if not re.fullmatch('[a-p]{32}', args.extension_id):
        raise ValueError('Invalid Chromium extension id')
    root = acquisition_root(args, environment)
    from .control_store import load_control
    state = load_control(root, environment.get('MEMU_DATABASE_URL'))
    origin = 'chrome-extension://' + args.extension_id + '/'
    directory = root / 'native'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    routes = []
    for source_id in args.source_id:
        source = state['sources'][source_id]
        if source['adapter_id'] != 'browser.ai-history':
            raise ValueError('Browser source required')
        source_path = directory / (source_id + '.json')
        private_save(source_path, {**source, 'extension_origin': origin,
                                  'center_url': args.url or environment.get('MEMU_ACQUISITION_URL', 'http://127.0.0.1:5073')})
        routes.append(str(source_path))
    source_path, launcher = directory / 'router.json', directory / 'host.sh'
    private_save(source_path, {'routes': routes, 'extension_origin': origin})
    # The script contains paths only, no token or source secret.
    env_path = environment.get('MEMU_ENV_FILE')
    lines = ['#!/bin/sh']
    if env_path:
        lines.append('export MEMU_ENV_FILE=' + shlex.quote(env_path))
    lines.append('cd ' + shlex.quote(str(Path(__file__).resolve().parents[2])))
    lines.append('exec ' + shlex.quote(sys.executable) + ' -m memu native-host --config ' + shlex.quote(str(source_path)) + ' "$@"')
    launcher.write_text('\n'.join(lines) + '\n')
    os.chmod(launcher, 0o700)
    output = args.output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    private_save(output, {'name': 'org.memu.acquisition', 'description': 'MemULife selected raw-data acquisition',
                          'path': str(launcher), 'type': 'stdio', 'allowed_origins': [origin]})
    return {'manifest': str(output), 'source_ids': args.source_id, 'installed': False,
            'next_step': 'Install this manifest in the browser NativeMessagingHosts directory'}
