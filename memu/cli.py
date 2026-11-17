"""Independent MCP and core processes, with dotenv/env-only configuration."""
import argparse
import fcntl
import json
import sys

from .config import Config, MCPConfig
from .environment import environment_config, load_environment, repository_root



def main(argv=None):
    from .events import configure_logging, event
    from .retry import error_code
    configure_logging()
    parser = argparse.ArgumentParser(prog='memulife', description='MemULife personal memory service')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['init', 'core', 'mcp', 'check']:
        child = sub.add_parser(command)
        if command in {'init', 'core'}:
            child.add_argument('--root', help='Override MEMU_MEMORY_ROOT')
        elif command == 'check':
            child.add_argument('--from', dest='identity', help='Identity for the authenticated health check')
    from .memulife import add_parser
    add_parser(sub)
    from .acquisition.cli import add_parser as add_acquisition_parser
    add_acquisition_parser(sub)
    args = parser.parse_args(argv)
    resources, service_lock = [], None
    try:
        environment = load_environment()
        if args.command in {'acquisition', 'sources', 'native-host'}:
            from .acquisition.cli import main as acquisition_main
            return acquisition_main(args, environment)
        if args.command in {'collect', 'import-memulife'}:
            from .memulife import BridgeError, run_import
            try:
                result = run_import(args, environment)
            except BridgeError as error:
                print(json.dumps({'status': 'blocked', 'error_code': str(error)}))
                return 1
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'done' else 2
        if args.command == 'check':
            from .client import check_connection
            print(json.dumps(check_connection(environment, args.identity), ensure_ascii=False))
            return 0
        if args.command == 'mcp':
            from .http_server import serve_mcp
            selected = {key: value for key, value in environment.items() if key.startswith('MEMU_MCP_')}
            settings = MCPConfig.model_validate(environment_config(selected).get('mcp', {}))
            serve_mcp(settings, environment)
            return 0
        config = Config.from_environment(environment)
        root = repository_root(args.root, environment=environment)
        if args.command == 'init':
            from .postgres_repository import PostgresRepository
            if (root / '.meta/postgres.json').is_file():
                print(json.dumps({'root': str(root), 'initialized': False}, ensure_ascii=False))
                return 0
            if root.exists() and any(root.iterdir()) and not (root / '.meta/catalog.tsv').is_file():
                raise ValueError('Repository initialization requires a new or empty directory')
            if not environment.get(config.database.dsn_env):
                raise ValueError('MEMU_DATABASE_URL is required')
            (root / '.meta').mkdir(parents=True, exist_ok=True)
            service_lock = (root / '.meta/service.lock').open('a+')
            try:
                fcntl.flock(service_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Stop core before migrating its repository') from error
            repository = PostgresRepository(root, environment[config.database.dsn_env], config.repository.timezone)
            repository.close()
            print(json.dumps({'root': str(root), 'initialized': True}, ensure_ascii=False))
            return 0
        if not any((root / '.meta' / name).is_file() for name in ('catalog.tsv', 'postgres.json')):
            raise ValueError('Repository is not initialized; run memu init first')
        service_lock = (root / '.meta/service.lock').open('a+')
        try:
            fcntl.flock(service_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('A service process already owns this repository') from error
        from .runtime import _build
        service, resources = _build(root, config)
        from .core_api import serve_core
        from .worker import MaintenanceWorker
        if service.index is not None:
            with service.repo.lock():
                if service.repo.state['index_dirty']:
                    service.repo.state['index_retry_at'] = 0
                    service.repo.save_state()
        worker = MaintenanceWorker(service).start()
        try:
            serve_core(service, config, environment)
        finally:
            worker.stop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        event('command_failed', stage=args.command, error_code=error_code(error))
        print(f'memu: {error_code(error)} ({type(error).__name__})', file=sys.stderr)
        return 1
    finally:
        for resource in reversed(resources):
            resource.close()
        if service_lock is not None:
            fcntl.flock(service_lock.fileno(), fcntl.LOCK_UN)
            service_lock.close()
