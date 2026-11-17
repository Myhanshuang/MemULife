"""HTTP(S) MCP client for diagnostics; URL is independent of the server bind address."""
import json
from .core_client import connection, endpoint


class HTTPConnection:
    def __init__(self, environment, identity=None):
        self.identity = identity or environment.get('MEMU_MCP_IDENTITY', 'agent1')
        self.url = endpoint(environment.get('MEMU_MCP_URL', 'http://127.0.0.1:5071/mcp'))
        self.token = json.loads(environment.get('MEMU_MCP_TOKENS', '{}')).get(self.identity)
        if not self.token:
            raise ValueError('No MCP token is configured for the selected identity')
        self.protocol = '2025-06-18'

    def send(self, request):
        client = connection(self.url, 900)
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                   'MCP-Protocol-Version': self.protocol}
        try:
            client.request('POST', self.url.path or '/mcp', body=json.dumps(request).encode(), headers=headers)
            response = client.getresponse()
            body = response.read()
            if response.status not in {200, 202}:
                raise RuntimeError(f'MCP HTTP {response.status}; no automatic retry')
            result = json.loads(body) if body else None
            if request.get('method') == 'initialize' and result and 'result' in result:
                self.protocol = result['result']['protocolVersion']
            return result
        finally:
            client.close()


def check_connection(environment, identity=None):
    client = HTTPConnection(environment, identity)
    response = client.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': client.protocol, 'capabilities': {},
        'clientInfo': {'name': 'memu-check', 'version': '1'}}})
    if not response or 'error' in response:
        raise RuntimeError('MCP initialization failed')
    client.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    response = client.send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    if not response or 'error' in response:
        raise RuntimeError('Core operation discovery failed')
    return {'ready': True, 'transport': 'streamable-http',
            'tools': [item['name'] for item in response['result']['tools']]}
