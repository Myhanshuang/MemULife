"""Network-only core client. Does not import storage, models, or database drivers."""
import http.client
import json
from urllib.parse import urlsplit


class CoreUnavailable(Exception):
    pass


class CoreRejected(CoreUnavailable):
    def __init__(self, status):
        super().__init__('Core rejected the request; no automatic retry')
        self.status = status


def endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Expected an HTTP(S) URL without credentials, query, or fragment')
    parsed.port  # Reject invalid ports at configuration time.
    return parsed


def connection(url, timeout):
    kind = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    return kind(url.hostname, url.port, timeout=timeout)


class RemoteCore:
    def __init__(self, url, tokens, timeout=600):
        self.url = endpoint(url)
        self.tokens = tokens
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError('Core timeout must be positive')

    def request(self, method, path, identity=None, body=None, timeout=None):
        client = connection(self.url, self.timeout if timeout is None else timeout)
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if identity is not None:
            headers['Authorization'] = 'Bearer ' + self.tokens[identity]
        try:
            client.request(method, self.url.path.rstrip('/') + path,
                           body=None if body is None else json.dumps(body).encode(), headers=headers)
            response = client.getresponse()
            if response.status not in {200, 202}:
                raise CoreRejected(response.status)
            return json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError):
            raise CoreUnavailable('Core response unavailable; no automatic retry') from None
        finally:
            client.close()

    def tools(self, identity):
        return self.request('GET', '/v1/tools', identity)

    def call(self, identity, name, arguments):
        return self.request('POST', '/v1/tools/' + name, identity, arguments)

    def write(self, identity, material):
        return self.request('POST', '/v1/memory/write', identity, material)

    def browse(self, identity, arguments, *, detail=False):
        return self.request('POST', '/v1/memory/browse' + ('/detail' if detail else ''), identity, arguments)

    def status(self, identity, request_id):
        return self.request('POST', '/v1/tools/memory_status', identity,
                            {'from': identity, 'request_id': request_id}, timeout=10)

    def statuses(self, identity, request_ids):
        return self.request('POST', '/v1/memory/statuses', identity,
                            {'request_ids': request_ids}, timeout=15)

    def ready(self):
        try:
            return self.request('GET', '/readyz', timeout=2).get('ready') is True
        except CoreUnavailable:
            return False
