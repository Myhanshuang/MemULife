import base64
import json

import httpx
import pytest

from memu.models import (HTTPEmbedding, ModelConfigurationError, ModelOutputError,
                         UnsupportedModalityError, prepare_embedding_items, validate_embeddings)


CAPS = {"modalities": ["text", "image", "audio", "video"], "dimension": 3,
        "metric": "cosine", "space_id": "fixture/shared", "model_version": "fixture/1",
        "preprocess_version": "fixture/1", "max_duration_seconds": {"audio": 10, "video": 10}}


def test_embedding_gateway_sends_native_bytes_with_original_timeline():
    observed = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=CAPS)
        body = json.loads(request.content)
        observed.extend(body["items"])
        return httpx.Response(200, json={"embeddings": [
            {"vector": [1, 0, 0], "space_id": CAPS["space_id"],
             "model_version": CAPS["model_version"], "preprocess_version": CAPS["preprocess_version"]}
            for _ in body["items"]]})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    model = HTTPEmbedding({"endpoint": "http://fixture.invalid", "model": "fixture"}, client=client)
    payload = base64.b64encode(b"synthetic-media-fixture").decode()
    result = model.embed([{"kind": "text", "text": "原文"},
                          {"kind": "video", "data_base64": payload, "mime_type": "video/mp4", "start": 12.0, "end": 18.0}])
    assert len(result) == 2
    assert observed[1]["data_base64"] == payload and "text" not in observed[1]
    assert observed[1]["start"] == 12 and observed[1]["end"] == 18
    client.close()


def test_incompatible_vectors_and_unconfigured_or_unsupported_models_fail():
    with pytest.raises(ModelConfigurationError):
        HTTPEmbedding({})
    with pytest.raises(UnsupportedModalityError):
        prepare_embedding_items([{"kind": "image", "text": "caption"}], {**CAPS, "modalities": ["text"]})
    with pytest.raises(ModelOutputError, match="substitute"):
        prepare_embedding_items([{"kind": "image", "text": "caption"}], CAPS)
    with pytest.raises(ModelOutputError, match="space_id"):
        validate_embeddings([{"kind": "text"}], [{"vector": [1, 0, 0], "space_id": "other",
                            "model_version": CAPS["model_version"], "preprocess_version": CAPS["preprocess_version"]}], CAPS)
