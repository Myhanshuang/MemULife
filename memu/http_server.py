"""Stateless MCP HTTP frontend. No repository, database, models or background jobs."""
from .core_client import RemoteCore, CoreUnavailable
from .http_transport import BoundedHTTPServer, JSONHandler, load_tokens, run_server
from .mcp import MCPApplication, PROTOCOL_VERSION, _error, log_tool_error


class MCPHTTPServer(BoundedHTTPServer):
    # Frontend shutdown must not wait for or stop work owned by the remote core.
    daemon_threads = True
    block_on_close = False

    def __init__(self, core, settings, tokens):
        self.application = MCPApplication(core, write_enabled=settings.write_enabled)
        super().__init__(settings, tokens, MCPHandler)


class MCPHandler(JSONHandler):
    api_paths = {'/mcp', '/v1/memory/write'}
    cors_methods = ('GET', 'POST')
    cors_headers = ('Authorization', 'Content-Type', 'Accept', 'MCP-Protocol-Version',
                    'Mcp-Session-Id', 'Last-Event-ID')

    def cors_origin(self):
        if self.path in self.api_paths and self.source_allowed():
            return self.headers.get('Origin')
        return None

    def end_headers(self):
        if self.path in self.api_paths:
            vary = 'Origin'
            if self.command == 'OPTIONS':
                vary += ', Access-Control-Request-Method, Access-Control-Request-Headers'
            self.send_header('Vary', vary)
            origin = self.cors_origin()
            if origin is not None:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Expose-Headers', 'WWW-Authenticate')
                if getattr(self, 'preflight_accepted', False):
                    self.send_header('Access-Control-Allow-Methods', ', '.join(self.cors_methods))
                    self.send_header('Access-Control-Allow-Headers', ', '.join(self.cors_headers))
                    self.send_header('Access-Control-Max-Age', '600')
        super().end_headers()

    def do_OPTIONS(self):
        if self.path not in self.api_paths:
            self.respond(404)
            return
        if self.cors_origin() is None:
            self.respond(403)
            return
        methods = self.headers.get_all('Access-Control-Request-Method', [])
        if len(methods) != 1:
            self.respond(400)
            return
        if methods[0] not in self.cors_methods:
            self.respond(403)
            return
        requested = self.headers.get_all('Access-Control-Request-Headers', [])
        names = [name.strip().lower() for value in requested for name in value.split(',')]
        if any(name not in {header.lower() for header in self.cors_headers} for name in names):
            self.respond(403)
            return
        # Fetch preflights carry no bearer token. Only the subsequent request is authenticated.
        self.preflight_accepted = True
        self.respond(204)

    def do_GET(self):
        if self.path == '/healthz':
            self.respond(200, {'alive': True})
            return
        if self.path == '/readyz':
            ready = self.server.application.core.ready()
            self.respond(200 if ready else 503, {'ready': ready})
            return
        if self.authorize() is not None:
            self.respond(405 if self.path in self.api_paths else 404)

    def do_POST(self):
        if self.path not in self.api_paths:
            self.respond(404)
            return
        identity = self.authorize()
        if identity is None:
            return
        if self.path == '/v1/memory/write':
            material = self.read_json()
            if material is None:
                return
            if not self.server.application.write_enabled:
                self.respond(200, {'is_error': True, 'result': {'error': {
                    'code': 'tool_disabled', 'message': 'Memory writes are temporarily disabled'}}})
                return
            try:
                from .mcp import external_write_error
                response = external_write_error(material) or self.server.application.core.write(identity, material)
            except CoreUnavailable:
                response = {'is_error': True, 'result': {'error': {
                    'code': 'core_unavailable',
                    'message': 'Outcome may be unknown; retain the original idempotency_key. No automatic retry.'}}}
            log_tool_error('memory_write', response)
            self.respond(202 if response.get('result', {}).get('status') == 'accepted' else 200, response)
            return
        if not all(kind in self.headers.get('Accept', '') for kind in ['application/json', 'text/event-stream']):
            self.respond(406)
            return
        request = self.read_json()
        if request is None:
            return
        version = self.headers.get('MCP-Protocol-Version')
        if request.get('method') != 'initialize' and version is not None and version != PROTOCOL_VERSION:
            self.respond(400)
            return
        if 'method' not in request and ('result' in request or 'error' in request):
            self.respond(202)
            return
        try:
            response = self.server.application.handle(request, identity)
        except Exception:
            response = _error(request.get('id'), -32603, 'MCP request failed')
        self.respond(202 if response is None else 200, response)


def serve_mcp(settings, environment):
    from .events import configure_logging
    configure_logging()
    tokens = load_tokens(environment)
    core = RemoteCore(environment.get('MEMU_CORE_URL', 'http://127.0.0.1:5072'), tokens,
                      environment.get('MEMU_CORE_TIMEOUT_SECONDS', '600'))
    run_server(MCPHTTPServer(core, settings, tokens))
