import base64
import json
import math

import httpx
import pytest

from memu.config import Config
from memu.models import ModelConfigurationError, ModelOutputError
from memu.vllm_embedding import VLLMGemmaEmbedding


CONFIG = {"endpoint": "http://fixture.invalid/v1", "model": "embeddinggemma-2",
          "backend": "vllm_gemma", "model_revision": "fixture-checkpoint"}


def client_for(observed, response=None, version="0.33.0", model="embeddinggemma-2"):
    def handle(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model}]})
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": version})
        assert request.url.path == "/v1/embeddings"
        observed.append(json.loads(request.content))
        return response or httpx.Response(200, json={"model": model, "data": [
            {"index": 0, "embedding": [3.0, 4.0] + [0.0] * 766}]})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_vllm_native_routes_prefixes_order_and_normalization():
    observed = []
    encoded = base64.b64encode(b"original-clip-bytes").decode()
    items = [{"kind": "text", "text": "查询", "purpose": "query"},
             {"kind": "text", "text": "原文"},
             {"kind": "image", "mime_type": "image/png", "data_base64": encoded},
             {"kind": "audio", "mime_type": "audio/wav", "data_base64": encoded, "start": 60, "end": 62},
             {"kind": "video", "mime_type": "video/mp4", "data_base64": encoded, "start": 120, "end": 124}]
    with client_for(observed) as client:
        adapter = VLLMGemmaEmbedding(CONFIG, client=client)
        results = adapter.embed(items)
        assert adapter.embed([]) == []
    assert len(observed) == len(results) == 5
    assert observed[0]["input"] == "task: search result | query: 查询"
    assert observed[1]["input"] == "title: none | text: 原文"
    for index, field in [(2, "image_url"), (3, "audio_url"), (4, "video_url")]:
        part = observed[index]["messages"][0]["content"][0]
        assert part == {"type": field, field: {"url": "data:" + items[index]["mime_type"] + ";base64," + encoded}}
        assert "input" not in observed[index] and "start" not in observed[index]
    assert all(not {"truncate_prompt_tokens", "dimensions", "media_io_kwargs"}.intersection(request)
               for request in observed)
    assert [result["kind"] for result in results] == [item["kind"] for item in items]
    assert all(math.hypot(*result["vector"]) == pytest.approx(1) for result in results)
    assert results[0]["vector"][:2] == [0.6, 0.8]


def test_hardware_build_tag_preserves_space_but_release_or_custom_build_does_not():
    spaces = {}
    for version in ('0.33.0', '0.33.0+cpu', '0.33.0+cu130', '0.34.0', '0.33.0+custom'):
        with client_for([], version=version) as client:
            spaces[version] = VLLMGemmaEmbedding(CONFIG, client=client).capabilities['space_id']
    assert spaces['0.33.0'] == spaces['0.33.0+cpu'] == spaces['0.33.0+cu130']
    assert spaces['0.34.0'] != spaces['0.33.0'] != spaces['0.33.0+custom']


@pytest.mark.parametrize("response", [
    {"data": []},
    {"data": [{"index": 1, "embedding": [1] * 768}]},
    {"data": [{"index": 0, "embedding": [1] * 767}]},
    {"data": [{"index": 0, "embedding": [0] * 768}]},
    {"data": [{"index": 0, "embedding": [True] * 768}]},
    {"model": "wrong-model", "data": [{"index": 0, "embedding": [1] * 768}]},
])
def test_bad_vectors_are_rejected_without_retry(response):
    observed = []
    response = {"model": CONFIG["model"], **response}
    with client_for(observed, httpx.Response(200, json=response)) as client:
        adapter = VLLMGemmaEmbedding(CONFIG, client=client)
        with pytest.raises(ModelOutputError):
            adapter.embed([{"kind": "text", "text": "test"}])
    assert len(observed) == 1


def test_server_failure_is_not_retried_or_replaced_by_local_model():
    observed = []
    with client_for(observed, httpx.Response(503)) as client:
        adapter = VLLMGemmaEmbedding(CONFIG, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            adapter.embed([{"kind": "text", "text": "test"}])
    assert len(observed) == 1


def test_invalid_input_fails_before_any_inference():
    observed = []
    with client_for(observed) as client:
        adapter = VLLMGemmaEmbedding(CONFIG, client=client)
        with pytest.raises(ModelOutputError, match="purpose"):
            adapter.embed([{"kind": "text", "text": "valid"},
                           {"kind": "text", "text": "invalid", "purpose": "invalid"}])
        with pytest.raises(ModelOutputError, match="duration"):
            adapter.embed([{"kind": "video", "mime_type": "video/mp4",
                            "data_base64": "YQ==", "start": 0, "end": 11}])
    assert not observed


def test_backend_config_and_space_change_with_revision_or_server_version():
    cfg = Config.from_environment({"EMB_BACKEND": "vllm_gemma", "EMB_MODEL_REVISION": "checkpoint-a"})
    assert cfg.models.embedding.model_revision == "checkpoint-a"
    with pytest.raises(ValueError, match="model_revision"):
        Config.from_environment({"EMB_BACKEND": "vllm_gemma"})
    spaces = set()
    for revision, version in [("a", "0.33.0"), ("b", "0.33.0"), ("a", "0.34.0")]:
        with client_for([], version=version) as client:
            adapter = VLLMGemmaEmbedding({**CONFIG, "model_revision": revision}, client=client)
            spaces.add(adapter.capabilities["space_id"])
    assert len(spaces) == 3
    with client_for([], model="wrong-model") as client:
        with pytest.raises(ModelConfigurationError, match="not served"):
            VLLMGemmaEmbedding(CONFIG, client=client)
