"""Bounded, event-driven HTTP primitives shared by two independent services."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import selectors
import signal
import socket
import threading
from .events import event
from urllib.parse import urlsplit


def load_tokens(environment):
    try:
        tokens = json.loads(environment.get('MEMU_MCP_TOKENS', '{}'))
        if (not isinstance(tokens, dict) or not tokens or
                any(not identity or not isinstance(token, str) or len(token) < 24 or
                    not token.isascii() or any(c.isspace() for c in token)
                    for identity, token in tokens.items()) or len(set(tokens.values())) != len(tokens)):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('MEMU_MCP_TOKENS must assign distinct tokens to identities') from None
    return tokens


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, settings, tokens, handler):
        self.settings, self.tokens = settings, tokens
        self.connections = threading.BoundedSemaphore(settings.max_requests)
        self.stopping = threading.Event()
        self.wake_read, self.wake_write = socket.socketpair()
        if ':' in settings.host:
            self.address_family = socket.AF_INET6
        super().__init__((settings.host, settings.port), handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        event('http_handler_failed', error_code='unhandled_request')

    def request_stop(self):
        if not self.stopping.is_set():
            self.stopping.set()
            self.wake_write.send(b'x')

    def run(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.socket, selectors.EVENT_READ)
            selector.register(self.wake_read, selectors.EVENT_READ)
            address = os.environ.get('NOTIFY_SOCKET')
            if address:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                    notify.connect('\0' + address[1:] if address.startswith('@') else address)
                    notify.sendall(b'READY=1')
            while not self.stopping.is_set():
                for key, _ in selector.select():
                    if key.fileobj is self.socket and not self.stopping.is_set():
                        self._handle_request_noblock()

    def close(self):
        self.request_stop()
        self.server_close()
        self.wake_read.close()
        self.wake_write.close()


class JSONHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'memu'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def respond(self, status, value=None):
        event('http_response', http_status=status)
        body = b'' if value is None else json.dumps(value, ensure_ascii=False, default=str).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            if body:
                self.send_header('Content-Type', 'application/json')
            if status == 401:
                self.send_header('WWW-Authenticate', 'Bearer realm="memu"')
            self.end_headers()
            self.close_connection = True
            if body:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def host_allowed(self):
        try:
            hosts = self.headers.get_all('Host', [])
            host = urlsplit('http://' + hosts[0]) if len(hosts) == 1 else None
            if (host is None or host.username or host.password or host.path or host.query or host.fragment or
                    host.hostname not in self.server.settings.allowed_hosts):
                raise ValueError
        except ValueError:
            return False
        return True

    def source_allowed(self):
        """Shared Host/Origin checks, also usable before preflight authentication."""
        origins = self.headers.get_all('Origin', [])
        return (self.host_allowed() and len(origins) <= 1 and
                (not origins or origins[0] in self.server.settings.allowed_origins))

    def authorize(self):
        if not self.source_allowed():
            self.respond(403)
            return None
        authorization = self.headers.get_all('Authorization', [])
        if len(authorization) == 1 and authorization[0].startswith('Bearer '):
            for identity, token in self.server.tokens.items():
                if secrets.compare_digest(authorization[0][7:].encode(), token.encode()):
                    return identity
        self.respond(401)
        return None

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            self.respond(415)
            return None
        lengths = self.headers.get_all('Content-Length', [])
        try:
            if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= self.server.settings.max_body_bytes:
                self.respond(413)
                return None
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, OSError):
            self.respond(400, {'error': 'invalid_json'})
            return None


def run_server(server):
    previous = {signum: signal.signal(signum, lambda *_: server.request_stop())
                for signum in [signal.SIGTERM, signal.SIGINT]}
    try:
        server.run()
    finally:
        server.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
