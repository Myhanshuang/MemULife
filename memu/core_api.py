"""Authenticated core API; sole owner of repository, models and maintenance."""
from concurrent.futures import ThreadPoolExecutor

from .http_transport import BoundedHTTPServer, JSONHandler, load_tokens, run_server
from .operations import CoreApplication, TOOL_MODELS
from .errors import RevisionConflict


class CoreHTTPServer(BoundedHTTPServer):
    def __init__(self, service, settings, tokens):
        if any(identity not in service.config.access.whitelist for identity in tokens):
            raise ValueError('Every core token identity must be whitelisted')
        self.application = CoreApplication(service)
        self.readers = ThreadPoolExecutor(max_workers=3, thread_name_prefix='memu-read')
        self.admissions = ThreadPoolExecutor(max_workers=4, thread_name_prefix='memu-admit')
        self.writers = ThreadPoolExecutor(max_workers=1, thread_name_prefix='memu-write')
        super().__init__(settings, tokens, CoreHandler)

    def close(self):
        super().close()  # Drain accepted requests before closing the repository.
        self.readers.shutdown(wait=True)
        self.admissions.shutdown(wait=True)
        self.writers.shutdown(wait=True)


class CoreHandler(JSONHandler):
    def do_GET(self):
        if self.path == '/healthz':
            self.respond(200, {'alive': True})
            return
        if self.path == '/readyz':
            status = self.server.application.service.readiness()
            status['ready'] = status['ready'] and not self.server.stopping.is_set()
            self.respond(200 if status['ready'] else 503, status)
            return
        identity = self.authorize()
        if identity is None:
            return
        if self.path != '/v1/tools':
            self.respond(404)
            return
        try:
            self.respond(200, self.server.application.tools(identity))
        except PermissionError:
            self.respond(403)

    def do_POST(self):
        identity = self.authorize()
        if identity is None:
            return
        prefix = '/v1/tools/'
        name = self.path[len(prefix):] if self.path.startswith(prefix) else ''
        common_write = self.path == '/v1/memory/write'
        import_result = self.path == '/v1/memory/import-result'
        resource = self.path == '/v1/memory/resource'
        statuses = self.path == '/v1/memory/statuses'
        browse = self.path in {'/v1/memory/browse', '/v1/memory/browse/detail',
                              '/v1/memory/browse/edit',
                              '/v1/memory/browse/delete-preview', '/v1/memory/browse/delete', '/v1/memory/browse/file',
                              '/v1/memory/browse/cleanup-audits'}
        if not common_write and not import_result and not resource and not statuses and not browse and name not in TOOL_MODELS:
            self.respond(404)
            return
        arguments = self.read_json()
        if arguments is None:
            return
        if browse:
            action = self.path.removeprefix('/v1/memory/browse').strip('/')
            operation = getattr(self.server.application.service, 'memory_browse' +
                                ('_' + action.replace('-', '_') if action else ''))
            try:
                pool = self.server.writers if action.startswith(('delete', 'cleanup')) else self.server.readers
                result = pool.submit(operation, identity, **arguments).result()
            except PermissionError:
                self.respond(403); return
            except RevisionConflict:
                self.respond(409); return
            except (ValueError, TypeError, KeyError):
                self.respond(422); return
        elif statuses:
            try:
                result = self.server.readers.submit(self.server.application.service.memory_statuses,
                    from_=identity, request_ids=arguments.get('request_ids')).result()
            except PermissionError:
                self.respond(403); return
            except ValueError:
                self.respond(422); return
        elif resource:
            try:
                result = self.server.readers.submit(self.server.application.service.media_resource,
                                                    identity, arguments.get('uri')).result()
            except (ValueError, PermissionError):
                self.respond(422)
                return
        elif import_result:
            try:
                result = self.server.readers.submit(self.server.application.service.import_result, identity,
                    arguments.get('source_instance_id'), arguments.get('object_ids')).result()
            except PermissionError:
                self.respond(403); return
            except ValueError:
                self.respond(422); return
        elif common_write:
            result = self.server.admissions.submit(self.server.application.write, identity, arguments).result()
        else:
            pool = (self.server.admissions if name == 'memory_write' else
                    self.server.writers if name == 'memory_manage' else self.server.readers)
            result = pool.submit(self.server.application.call, identity, name, arguments).result()
        self.respond(202 if common_write and result.get('result', {}).get('status') == 'accepted' else 200, result)


def serve_core(service, config, environment):
    run_server(CoreHTTPServer(service, config.core, load_tokens(environment)))
