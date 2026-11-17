import http.client
import json
import threading

import pytest

from memu.config import MCPConfig
from memu.http_server import MCPHTTPServer


def test_cors_preflight_authentication_and_origin_boundaries():
    origin = 'http://localhost:5070'
    token = 'cors-fixture-token-not-a-secret-1234'

    class Core:
        calls = 0

        def tools(self, identity):
            self.calls += 1
            return {'tools': []}

    core = Core()
    settings = MCPConfig(allowed_origins=[origin]).model_copy(update={'port': 0})
    server = MCPHTTPServer(core, settings, {'agent1': token})
    thread = threading.Thread(target=server.run)
    thread.start()

    def request(method, headers, body=None):
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            client.request(method, '/mcp', headers=headers, body=body)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    try:
        preflight = {'Origin': origin, 'Access-Control-Request-Method': 'POST',
                     'Access-Control-Request-Headers': 'authorization, Content-Type, mcp-protocol-version'}
        status, headers, body = request('OPTIONS', preflight)
        assert status == 204 and not body and core.calls == 0
        assert headers['Access-Control-Allow-Origin'] == origin
        assert headers['Access-Control-Allow-Credentials'] == 'true'
        assert 'POST' in headers['Access-Control-Allow-Methods']
        assert 'Authorization' in headers['Access-Control-Allow-Headers']
        assert 'Origin' in headers['Vary']

        for change in [{'Origin': 'https://untrusted.example'}, {'Origin': 'null'},
                       {'Host': 'untrusted.example'}, {'Access-Control-Request-Method': 'DELETE'},
                       {'Access-Control-Request-Headers': 'x-unapproved-header'}]:
            status, headers, _ = request('OPTIONS', {**preflight, **change})
            assert status == 403 and 'Access-Control-Allow-Methods' not in headers
            if 'Origin' in change or 'Host' in change:
                assert 'Access-Control-Allow-Origin' not in headers
        status, headers, _ = request('OPTIONS', {'Access-Control-Request-Method': 'POST'})
        assert status == 403 and 'Access-Control-Allow-Origin' not in headers

        actual = {'Origin': origin, 'Accept': 'application/json, text/event-stream',
                  'Content-Type': 'application/json', 'MCP-Protocol-Version': '2025-06-18'}
        payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        status, headers, _ = request('POST', actual, payload)
        assert status == 401 and core.calls == 0
        assert headers['Access-Control-Allow-Origin'] == origin
        assert 'WWW-Authenticate' in headers['Access-Control-Expose-Headers']
        actual['Authorization'] = 'Bearer ' + token
        status, headers, body = request('POST', actual, payload)
        assert status == 200 and core.calls == 1
        assert headers['Access-Control-Allow-Origin'] == origin
        assert json.loads(body)['result'] == {'tools': []}
        status, headers, _ = request('POST', {**actual, 'Origin': 'https://untrusted.example'}, payload)
        assert status == 403 and core.calls == 1
        assert 'Access-Control-Allow-Origin' not in headers
        actual.pop('Origin')
        status, headers, _ = request('POST', actual, payload)
        assert status == 200 and core.calls == 2
        assert 'Access-Control-Allow-Origin' not in headers
    finally:
        server.request_stop()
        thread.join(5)
        server.close()
    assert not thread.is_alive()


def test_origin_configuration_requires_exact_http_origins():
    from memu.environment import environment_config

    values = environment_config({'MEMU_MCP_ALLOWED_ORIGINS': '["http://LOCALHOST:80","https://app.example:443"]'})
    assert MCPConfig.model_validate(values['mcp']).allowed_origins == ['http://localhost', 'https://app.example']
    for invalid in ['*', 'null', 'localhost:5070', 'http://localhost:5070/',
                    'http://user@localhost:5070', 'http://localhost:5070?x=1',
                    'http://localhost:bad', 'http://localhost\r\nInjected: value']:
        with pytest.raises(ValueError):
            MCPConfig(allowed_origins=[invalid])
