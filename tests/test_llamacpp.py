import base64
import json

import httpx
import pytest

from memu.config import MLLMConfig
from memu.llamacpp import LlamaCppMLLM
from memu.models import ModelOutputError
from memu.types import UnitsOutput


def test_native_media_and_scoped_read_envelope():
    encoded = base64.b64encode(b'fixture-only-image').decode()
    material = {'ref': 'a1', 'media': {'mime_type': 'image/png', 'data_base64': encoded}}

    def handler(request):
        assert request.url.path == '/v1/chat/completions'
        body = json.loads(request.content)
        assert [m['role'] for m in body['messages']].count('system') == 1
        assert body['messages'][0]['role'] == 'system'
        assert 'task policy' in body['messages'][0]['content'] and 'common policy' in body['messages'][0]['content']
        assert encoded not in body['messages'][0]['content']
        assert body['messages'][-1]['content'][-1]['image_url']['url'].endswith(encoded)
        schema = body['response_format']['json_schema']['schema']
        assert 'Time' in schema['$defs'] and len(schema['anyOf']) == 2
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'tool_calls': [{'name': 'memory_read', 'arguments': {'ref': 'c1:1'}}]})}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = LlamaCppMLLM(MLLMConfig(endpoint='http://fixture.invalid/v1', model='fixture',
                                        protocol='llamacpp'), client=client)
        result = model.complete({'task': 'unit', 'messages': [
                                    {'role': 'system', 'content': 'common policy'},
                                    {'role': 'system', 'content': 'task policy'},
                                    {'role': 'user', 'content': json.dumps(material)}],
                                 'output_schema': UnitsOutput.model_json_schema(), 'tools': [{'name': 'memory_read'}]})
        assert result['tool_calls'][0]['arguments']['ref'] == 'c1:1'


def test_truncated_model_output_is_not_committed_or_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length',
                                                     'message': {'content': '{"units":[]}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = LlamaCppMLLM({'endpoint': 'http://fixture.invalid/v1', 'model': 'fixture'}, client=client)
        with pytest.raises(ModelOutputError):
            model.complete({'task': 'unit', 'messages': [], 'output_schema': UnitsOutput.model_json_schema()})
    assert len(calls) == 1
