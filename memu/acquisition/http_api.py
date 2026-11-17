"""Authenticated local acquisition UI/API, separate from the memory core."""
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..http_transport import BoundedHTTPServer, JSONHandler
from ..core_client import CoreRejected
from .adapters.base import AdapterError
from .exchange import BrowserExchange
from .pairing import BrowserPairing
from .sources import get_source, sources


class AcquisitionHTTPServer(BoundedHTTPServer):
    def __init__(self, service, settings, tokens):
        self.service, self.exchange = service, BrowserExchange(service)
        self.pairing = BrowserPairing(service)
        super().__init__(settings, tokens, AcquisitionHandler)


class AcquisitionHandler(JSONHandler):
    def end_headers(self):
        if getattr(self, '_browser_origin', None):
            self.send_header('Access-Control-Allow-Origin', self._browser_origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'POST')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        super().end_headers()

    def browser_origin(self):
        origins = self.headers.get_all('Origin', [])
        if self.host_allowed() and len(origins) == 1 and self.server.pairing.allowed_origin(origins[0]):
            self._browser_origin = origins[0]
            return origins[0]
        self.respond(403)
        return None

    def do_OPTIONS(self):
        if self.path not in {'/v1/browser/pair', '/v1/browser/rpc', '/v1/browser/authorize'}:
            self.respond(404)
        elif self.browser_origin():
            self.respond(204)

    def respond(self, status, value=None):
        if value is None and status >= 400:
            value = {'code': 'unauthorized' if status == 401 else 'not_found' if status == 404 else 'invalid_request'}
        super().respond(status, value)

    def static(self, path):
        root = Path(__file__).parent / 'ui'
        pages = {module.slug for module in sources() if module.slug != 'ai'} | {'activity', 'topics', ''}
        pages.update('ai/' + profile for profile in get_source('browser.ai-history').profiles)
        if path == '/manage/imports/feishu/oauth/callback':
            candidate = root / 'feishu-callback.html'
        elif path in {'/', '/manage', '/manage/imports', '/manage/imports/'}:
            candidate = root / 'index.html'
        elif path.startswith('/manage/imports/') and path[len('/manage/imports/'):] in pages:
            candidate = root / 'index.html'
        elif path in {'/manage/memory', '/manage/memory/'} or (path.startswith('/manage/memory/')
                and path[len('/manage/memory/'):] in (pages - {'activity'}) | {'summaries', 'search'}):
            candidate = root / 'index.html'
        elif path.startswith('/manage/assets/imports/'):
            relative = path[len('/manage/assets/imports/'):]
            if any(not part or part.startswith('.') or ':' in part or '\\' in part for part in relative.split('/')):
                return False
            candidate = root / relative
            if candidate.suffix not in {'.css', '.js'} or candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
                return False
        else:
            return False
        if not candidate.is_file():
            return False
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        return True

    def do_GET(self):
        if not self.source_allowed():
            self.respond(403)
            return
        path = urlsplit(self.path).path
        if self.static(path):
            return
        if path == '/healthz':
            self.respond(200, {'alive': True})
            return
        if path == '/readyz':
            ready = not self.server.stopping.is_set() and self.server.service.client.ready()
            self.respond(200 if ready else 503, {'ready': ready})
            return
        self.dispatch('GET')

    def do_POST(self):
        if self.path in {'/v1/browser/pair', '/v1/browser/rpc', '/v1/browser/authorize'}:
            self.browser_dispatch()
            return
        self.dispatch('POST')

    def browser_dispatch(self):
        origin = self.browser_origin()
        if not origin:
            return
        body = self.read_json()
        if body is None:
            return
        try:
            if self.path == '/v1/browser/authorize':
                result = self.server.pairing.authorize(origin, body.get('code'), body.get('provider'))
            elif self.path == '/v1/browser/pair':
                result = self.server.pairing.pair(origin, body.get('code'), body.get('account'))
            else:
                auth = self.headers.get_all('Authorization', [])
                if len(auth) != 1 or not auth[0].startswith('Bearer '):
                    raise AdapterError('scope_denied')
                result = self.server.pairing.dispatch(self.server.exchange, origin, auth[0][7:], body)
            self.respond(200, result)
        except AdapterError as error:
            self.respond(403 if error.code == 'scope_denied' else 422, {'code': error.code})
        except (ValueError, KeyError, TypeError):
            self.respond(422, {'code': 'invalid_request'})
        except Exception:
            self.respond(500, {'code': 'acquisition_failed'})

    def dispatch(self, method):
        identity = self.authorize()
        if identity is None:
            return
        body = self.read_json() if method == 'POST' else {}
        if body is None:
            return
        parsed = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        path, service = parsed.path, self.server.service
        try:
            if path.startswith('/v1/source-imports/'):
                tail = path[len('/v1/source-imports/'):].split('/')
                if len(tail) != 2:
                    raise AdapterError('not_found')
                source, operation = tail
                exchange = self.server.exchange
                if method == 'GET' and operation == 'selection':
                    result = exchange.selection(identity, source, query.get('claim_previews') == 'true')
                elif method == 'POST' and operation in {'catalog', 'capture', 'chunks', 'commit', 'preview', 'progress'}:
                    result = getattr(exchange, {'catalog': 'offer', 'chunks': 'chunk'}.get(operation, operation))(identity, source, body)
                else:
                    raise AdapterError('not_found')
            elif path == '/v1/manage/memory/revise' and method == 'POST':
                service.owner(identity)
                response = service.client.call(identity, 'memory_write',
                    {**body, 'from': identity, 'mode': 'revise'})
                result = response['result']
                if response.get('is_error'):
                    self.respond(409 if result.get('status') == 'conflict' else 422,
                        {'code': 'memory_edit_conflict' if result.get('status') == 'conflict' else 'memory_edit_invalid',
                         'result': result})
                    return
            elif path in {'/v1/manage/memory', '/v1/manage/memory/detail', '/v1/manage/memory/media',
                          '/v1/manage/memory/edit',
                          '/v1/manage/memory/delete-preview', '/v1/manage/memory/delete', '/v1/manage/memory/file'} and method == 'POST':
                service.owner(identity)
                try:
                    if path.endswith('/media'):
                        from .memory_media import memory_media
                        result = memory_media(service, identity, body.get('ref'))
                    elif path.endswith(('/delete', '/delete-preview', '/file', '/edit')):
                        result = service.client.request('POST', '/v1/memory/browse/' + path.rsplit('/', 1)[1], identity, body)
                    else:
                        result = service.client.browse(identity, body, detail=path.endswith('/detail'))
                except CoreRejected as error:
                    if error.status == 409:
                        self.respond(409, {'code': 'memory_delete_changed'})
                        return
                    self.respond(403 if error.status == 403 else 422 if error.status == 422 else 503,
                                 {'code': 'scope_denied' if error.status == 403 else 'memory_query_invalid'
                                  if error.status == 422 else 'memory_unavailable'})
                    return
            elif path == '/v1/manage/topics/retry' and method == 'POST':
                service.owner(identity)
                response = service.client.call(identity, 'memory_manage', {'from': identity,
                    'action': 'retry', 'view': 'topics'})
                if response.get('is_error'):
                    self.respond(422, {'code': 'topic_invalid'})
                    return
                result = response['result']
            elif path == '/v1/manage/topics' and method in {'GET', 'POST'}:
                service.owner(identity)
                arguments = {'from': identity, 'action': 'topics' if method == 'GET' else 'topic'}
                if method == 'POST':
                    arguments.update(scope=body.get('key'), text=body.get('description', ''))
                response = service.client.call(identity, 'memory_manage', arguments)
                if response.get('is_error'):
                    self.respond(422, {'code': 'topic_invalid'})
                    return
                result = response['result']
            elif path.startswith('/v1/manage/imports/'):
                operation = path[len('/v1/manage/imports/'):]
                service.owner(identity)
                if operation == 'sources' and method == 'GET':
                    result = service.inventory(identity)
                elif operation == 'sources' and method == 'POST':
                    result = service.register(identity, body['source'], body.get('source_identity'), body.get('credentials'), body.get('schema_text'))
                elif operation == 'setup' and method == 'GET':
                    from .setup import setup_catalog
                    result = setup_catalog()
                elif operation == 'feishu/authorize' and method == 'POST':
                    result = service.feishu_authorization.start(identity, body, self.headers.get('Origin'))
                elif operation == 'feishu/complete' and method == 'POST':
                    result = service.feishu_authorization.complete(identity, body)
                elif operation == 'feishu/cancel' and method == 'POST':
                    result = service.feishu_authorization.cancel(identity, body.get('state'))
                elif operation == 'browser-pairings' and method == 'POST':
                    result = self.server.pairing.create(identity, body.get('provider'), body.get('name'), body.get('extension_id'))
                elif operation == 'browser-pairings/status' and method == 'POST':
                    result = self.server.pairing.status(identity, body.get('code'))
                elif operation.startswith('sources/'):
                    tail = operation.split('/')
                    source = tail[1]
                    if len(tail) == 2 and method == 'GET':
                        result = service.describe(identity, source)
                    elif len(tail) == 2 and method == 'POST':
                        if set(body) != {'name'}:
                            raise AdapterError('invalid_request')
                        result = service.rename(identity, source, body['name'])
                    elif tail[2:] == ['delete'] and method == 'POST':
                        result = service.delete_source(identity, source)
                        self.server.pairing.revoke(identity, source)
                    elif tail[2:] == ['watch'] and method in {'GET', 'POST'}:
                        service.binding(identity, source)
                        result = (service.watches.public(source) if method == 'GET' else
                            service.watches.configure(identity, source, body.get('enabled'), body.get('interval_seconds', 900)))
                    elif tail[2:] == ['candidates'] and method == 'GET':
                        result = service.candidates(identity, source, parent=query.get('parent', '.'),
                            cursor=query.get('cursor', ''), limit=int(query.get('limit', 100)),
                            query=query.get('query', ''), kind=query.get('kind', ''))
                    elif tail[2:] == ['preview'] and method == 'POST':
                        result = service.preview(identity, source, body['selection'], body.get('cursor', ''), body.get('limit', 30))
                    elif tail[2:] == ['revoke-browser'] and method == 'POST':
                        result = self.server.pairing.revoke(identity, source)
                    else:
                        raise AdapterError('not_found')
                elif operation == 'plan' and method == 'POST':
                    result = service.plan(identity, body['source_id'], body['selection'])
                elif operation == 'plan-batch' and method == 'POST':
                    result = service.plan_batch(identity, body['source_id'], body['selections'])
                elif operation.startswith('jobs/') and method == 'GET':
                    result = service.job_detail(identity, operation.split('/')[1])
                elif operation == 'jobs' and method == 'GET':
                    result = service.jobs(identity)
                elif operation in {'jobs', 'jobs/batch'} and method == 'POST':
                    plans = body['plans'] if operation.endswith('/batch') else [body]
                    result = service.create_batch(identity, body['source_id'], plans)
                    if operation == 'jobs':
                        result = result['jobs'][0]
                elif operation.startswith('jobs/') and method == 'POST':
                    result = service.control(identity, operation.split('/')[1], body['action'])
                elif operation == 'subscriptions' and method == 'GET':
                    result = service.subscriptions(identity)
                elif operation == 'subscriptions' and method == 'POST':
                    result = service.subscribe(identity, body['job_id'], body.get('interval_seconds', 900))
                elif operation.startswith('subscriptions/') and method == 'POST':
                    result = service.control_subscription(identity, operation.split('/')[1], body['action'])
                else:
                    raise AdapterError('not_found')
            else:
                raise AdapterError('not_found')
            self.respond(200, result)
        except AdapterError as error:
            status = 403 if error.code == 'scope_denied' else 404 if error.code == 'not_found' else 422
            self.respond(status, {'code': error.code, 'retryable': error.retryable})
        except (ValueError, KeyError, TypeError, AttributeError):
            self.respond(422, {'code': 'invalid_request'})
        except Exception:
            self.respond(500, {'code': 'acquisition_failed'})
