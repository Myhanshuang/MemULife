from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading

from memu.config import Config, EmbeddingConfig
from memu.http_server import MCPHTTPServer
from memu.core_api import CoreHTTPServer
from memu.core_client import RemoteCore, CoreUnavailable
import pytest
import http.client
from memu.client import HTTPConnection


def initialize(client):
    response = client.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
    assert 'result' in response
    client.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})


def test_independent_sessions_parallel_reads_and_identity_isolation():
    started, release = threading.Event(), threading.Event()
    config = Config.model_validate({'access': {'whitelist': {'agent1': ['read', 'write'],
                                                           'owner': ['read', 'write']}, 'owners': ['owner']}})

    class Service:
        def memory_read(self, **kwargs):
            return {'source': kwargs['from_']}

        def memory_write(self, **kwargs):
            started.set()
            assert release.wait(5)
            return {'status': 'done'}

    service = Service()
    service.config = config
    tokens = {'agent1': 'a' * 32, 'owner': 'b' * 32}
    # Separate TCP listeners, two interchangeable frontends, and one core owner.
    core = CoreHTTPServer(service, config.core.model_copy(update={'host': '0.0.0.0', 'port': 0}), tokens)
    core_thread = threading.Thread(target=core.run)
    core_thread.start()
    remote = RemoteCore(f'http://localhost:{core.server_port}', tokens)
    servers = [MCPHTTPServer(remote, config.mcp.model_copy(update={'host': '0.0.0.0', 'port': 0}), tokens)
               for _ in range(2)]
    threads = [threading.Thread(target=server.run) for server in servers]
    for thread in threads:
        thread.start()
    server, thread = servers[0], threads[0]
    environment = {'MEMU_MCP_URL': f'http://localhost:{server.server_port}/mcp', 'MEMU_MCP_TOKENS': json.dumps(tokens)}
    first, second, owner = [HTTPConnection(environment, identity) for identity in ['agent1', 'agent1', 'owner']]
    try:
        for client in [first, second, owner]:
            initialize(client)
        assert remote.ready()
        with ThreadPoolExecutor(max_workers=1) as pool:
            write = pool.submit(first.send, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'memory_write', 'arguments': {'from': 'agent1', 'text': 'fixture'}}})
            assert started.wait(5)
            read = second.send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'memory_read', 'arguments': {'from': 'agent1'}}})
            assert read['result']['structuredContent']['source'] == 'agent1'
            assert not write.done()
            release.set()
            assert write.result()['result']['isError'] is False
        denied = second.send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'memory_read', 'arguments': {'from': 'owner'}}})
        assert denied['result']['structuredContent']['error']['code'] == 'access_denied'
        # Move an already initialized client to a different frontend without another handshake.
        from memu.core_client import endpoint
        second.url = endpoint(f'http://localhost:{servers[1].server_port}/mcp')
        server.request_stop()
        thread.join(5)
        server.close()
        servers.remove(server)
        assert core_thread.is_alive()
        assert 'result' in second.send({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})
        owner.url = second.url
        assert 'result' in owner.send({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})
        # Authentication and identity checks also hold when bypassing MCP.
        denied = remote.call('agent1', 'memory_read', {'from': 'owner'})
        assert denied['result']['error']['code'] == 'access_denied'
        wrong = RemoteCore(f'http://localhost:{core.server_port}', {'agent1': 'wrong-token'})
        with pytest.raises(CoreUnavailable):
            wrong.tools('agent1')
        conn = http.client.HTTPConnection('localhost', servers[0].server_port)
        conn.request('POST', '/mcp', body='{}', headers={'Host': 'untrusted.example',
            'Authorization': 'Bearer ' + tokens['agent1']})
        assert conn.getresponse().status == 403
        conn.close()
        # Core loss leaves the frontend live; requests report failure instead of empty success.
        core.request_stop()
        core_thread.join(5)
        core.close()
        assert not remote.ready()
        failed = second.send({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'memory_read', 'arguments': {'from': 'agent1'}}})
        assert failed['result']['structuredContent']['error']['code'] == 'core_unavailable'

    finally:
        release.set()
        for server in servers:
            server.request_stop()
        for thread in threads:
            thread.join(5)
        for server in servers:
            server.close()
        if core_thread.is_alive():
            core.request_stop()
            core_thread.join(5)
            core.close()
    assert not any(thread.is_alive() for thread in threads)


def test_embedding_worker_shared_and_fully_exits_on_idle(monkeypatch, tmp_path):
    from memu import lazy_embedding
    caps = {'modalities': ['text'], 'dimension': 2, 'metric': 'cosine',
            'space_id': 'fixture', 'model_version': 'fixture', 'preprocess_version': 'fixture'}
    monkeypatch.setattr(lazy_embedding, 'describe_model', lambda path: caps)
    monkeypatch.setattr(lazy_embedding, 'memory_snapshot', lambda: {'available_bytes': 10 * 1024 ** 3})
    real_popen = lazy_embedding.subprocess.Popen
    worker = 'import sys,json\nfor line in sys.stdin:\n request=json.loads(line)\n print(json.dumps({"embeddings":[dict(vector=[1.,0.],space_id="fixture",model_version="fixture",preprocess_version="fixture") for _ in request["items"]]}),flush=True)\n'
    monkeypatch.setattr(lazy_embedding.subprocess, 'Popen', lambda command, **kwargs:
                        real_popen([sys.executable, '-u', '-c', worker], **kwargs))
    adapter = lazy_embedding.LazyGemmaEmbedding(EmbeddingConfig(model_path=str(tmp_path), idle_seconds=1.0))
    try:
        assert adapter._child is None
        adapter.embed([{'kind': 'text', 'text': 'one'}])
        child, finished = adapter._child, adapter._finished
        adapter.embed([{'kind': 'text', 'text': 'two', 'purpose': 'query'}])
        assert adapter._child is child
        assert finished.wait(5)
        assert adapter._child is None and child.returncode == 0
    finally:
        adapter.close()
