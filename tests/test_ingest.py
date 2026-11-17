import http.client
import json
import threading

from memu.core_api import CoreHTTPServer
from memu.core_client import RemoteCore
from memu.http_server import MCPHTTPServer
from test_service import service_fixture


def test_common_ingress_runs_pipeline_and_shares_mcp_idempotency(tmp_path):
    service, model, embedding, index = service_fixture(tmp_path)
    tokens = {'owner': 'fixture-owner-' + 'a' * 32, 'reader': 'fixture-reader-' + 'b' * 32}
    core = CoreHTTPServer(service, service.config.core.model_copy(update={'port': 0}), tokens)
    remote = RemoteCore(f'http://localhost:{core.server_port}', tokens)
    origin = 'http://localhost:5070'
    front = MCPHTTPServer(remote, service.config.mcp.model_copy(update={
        'port': 0, 'allowed_origins': [origin]}), tokens)
    threads = [threading.Thread(target=server.run) for server in (core, front)]
    for thread in threads:
        thread.start()

    def request(method='POST', body=None, token=tokens['owner'], extra=None):
        connection = http.client.HTTPConnection('localhost', front.server_port, timeout=10)
        headers = {'Content-Type': 'application/json', 'Origin': origin}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            connection.request(method, '/v1/memory/write', json.dumps(body) if body is not None else None,
                               {**headers, **(extra or {})})
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    try:
        material = {'text': '原始来源说明：项目仍在验证中。', 'idempotency_key': 'shared-ingress'}
        status, headers, response = request(body=material)
        assert status == 200 and response['is_error'] is False
        assert headers['Access-Control-Allow-Origin'] == origin
        result = response['result']
        assert result['status'] == 'done' and result['units']
        assert model.calls['unit'] == model.calls['relate'] == 1
        assert index.search_calls == 1 and embedding.calls
        # MCP's existing operation contract, common frontend and common core share one commit.
        for replay in (remote.call('owner', 'memory_write', {'from': 'owner', **material}),
                       remote.write('owner', material), request(body=material)[2]):
            assert replay['result']['units'] == result['units']
        assert model.calls['unit'] == 1
        assert request(body=material, token=None)[0] == 401
        denied = request(body={**material, 'from': 'reader'})[2]
        assert denied['result']['error']['code'] == 'access_denied'
        denied = request(body=material, token=tokens['reader'])[2]
        assert denied['is_error'] is True
        assert request(body={**material, 'text': 'different'})[2]['is_error'] is True
        assert request(body={**material, 'units': []})[2]['result']['error']['code'] == 'invalid_arguments'
        assert request(body=material, extra={'Origin': 'https://untrusted.example'})[0] == 403
        assert request('OPTIONS', token=None, extra={'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type'})[0] == 204
        front.application.write_enabled = False
        assert request(body=material)[2]['result']['error']['code'] == 'tool_disabled'
    finally:
        for server in (front, core):
            server.request_stop()
        for thread in threads:
            thread.join(10)
        for server in (front, core):
            server.close()


def test_common_frontend_reports_unknown_outcome():
    from memu.config import MCPConfig
    from memu.core_client import CoreUnavailable

    class Offline:
        def write(self, *args):
            raise CoreUnavailable()

    server = MCPHTTPServer(Offline(), MCPConfig().model_copy(update={'port': 0}), {'owner': 'x' * 32})
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        remote = RemoteCore(f'http://localhost:{server.server_port}', {'owner': 'x' * 32})
        result = remote.write('owner', {'text': 'fixture', 'idempotency_key': 'key'})
        assert result['is_error'] is True
        assert result['result']['error']['code'] == 'core_unavailable'
    finally:
        server.request_stop()
        thread.join(5)
        server.close()
