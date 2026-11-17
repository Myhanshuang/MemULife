"""Native EmbeddingGemma 2 through an existing vLLM pooling server.

Uses /v1/embeddings with text input or multimodal messages. No Torch imports,
model subprocesses, caption substitution, prompt truncation, or HTTP retries.
The owner supplies the checkpoint revision because /v1/models exposes aliases,
not immutable weight identities. Changing preprocessing requires an index rebuild.
"""

import hashlib
import json
import math
import re

from .config import EmbeddingConfig
from .models import (_HTTPGateway, ModelConfigurationError, ModelOutputError,
                     prepare_embedding_items, validate_capabilities, validate_embeddings)


TEXT_PREFIXES = {"query": "task: search result | query: ", "document": "title: none | text: "}


class VLLMGemmaEmbedding(_HTTPGateway):
    def __init__(self, config, *, client=None):
        config = config if isinstance(config, EmbeddingConfig) else EmbeddingConfig.model_validate(config)
        if not config.model_revision.strip():
            raise ModelConfigurationError("vLLM Gemma requires an immutable model_revision")
        super().__init__(config, client=client)
        # Accept either the server root or its conventional /v1 base URL.
        if self.endpoint.endswith("/v1"):
            self.endpoint = self.endpoint[:-3]
        try:
            models = self._request("GET", "/v1/models")
            if not isinstance(models, dict) or not any(
                isinstance(card, dict) and card.get("id") == self.config.model
                for card in models.get("data", [])
            ):
                raise ModelConfigurationError("Configured embedding model is not served by vLLM")
            version = self._request("GET", "/version").get("version")
            if not isinstance(version, str) or not version:
                raise ModelConfigurationError("vLLM omitted its server version")
            # Same release and preprocessing on a CPU build keeps the vector space.
            # Preserve development/custom suffixes; only hardware build tags match.
            version = re.sub(r'\+(?:cpu|cu[0-9]+)$', '', version)
            preprocessing = {
                "adapter": "memu-vllm-embeddinggemma2-v1", "vllm": version,
                "text_prefixes": TEXT_PREFIXES, "media_io": "server-configured",
                "dimension": 768, "normalization": "l2", "native_media": True,
                "audio_max_seconds": 30, "video_max_seconds": 10,
            }
            pre_hash = hashlib.sha256(json.dumps(preprocessing, sort_keys=True).encode()).hexdigest()
            model_version = "embeddinggemma2:" + config.model_revision
            self.capabilities = validate_capabilities({
                "modalities": ["text", "image", "audio", "video"], "dimension": 768,
                "metric": "cosine", "model_version": model_version,
                "space_id": model_version + ":vllm:" + pre_hash,
                "preprocess_version": "vllm-gemma2:" + pre_hash,
                "max_duration_seconds": {"audio": 30, "video": 10},
            })
        except Exception:
            self.close()
            raise

    def _payload(self, item):
        purpose = item.get("purpose", "document")
        if purpose not in TEXT_PREFIXES:
            raise ModelOutputError("Embedding purpose must be query or document")
        # Native dimension is validated on response. vLLM rejects a dimensions
        # override for this model, including an override equal to its native size.
        payload = {"model": self.config.model, "encoding_format": "float", "use_activation": True}
        kind = item["kind"]
        if kind == "text":
            payload["input"] = TEXT_PREFIXES[purpose] + item["text"]
        else:
            # native_item supplies already-cut media bytes. start/end describe the
            # original timeline and must not be applied as offsets a second time.
            data_url = "data:" + item["mime_type"] + ";base64," + item["data_base64"]
            field = {"image": "image_url", "audio": "audio_url", "video": "video_url"}[kind]
            payload["messages"] = [{"role": "user", "content": [
                {"type": field, field: {"url": data_url}},
            ]}]
            # Let the trusted server apply its media preprocessing. vLLM blocks
            # per-request media_io_kwargs unless an explicit server flag allows it.
        return payload

    def embed(self, items):
        prepared = prepare_embedding_items(items, self.capabilities)
        # Validate the entire call before issuing any inference requests.
        payloads = [self._payload(item) for item in prepared]
        outputs = []
        # Serial single-item requests bound GPU activation memory for media.
        for item, payload in zip(prepared, payloads):
            result = self._request("POST", "/v1/embeddings", json=payload)
            rows = result.get("data") if isinstance(result, dict) else None
            if (not isinstance(rows, list) or len(rows) != 1 or
                    not isinstance(rows[0], dict) or type(rows[0].get("index")) is not int or
                    rows[0]["index"] != 0):
                raise ModelOutputError("vLLM must return exactly one indexed embedding per item")
            if result.get("model") != self.config.model:
                raise ModelOutputError("vLLM returned a different embedding model")
            output = {name: self.capabilities[name] for name in
                      ("space_id", "model_version", "preprocess_version", "metric")}
            output.update(kind=item["kind"], vector=rows[0].get("embedding"))
            output = validate_embeddings([item], [output], self.capabilities)[0]
            norm = math.hypot(*output["vector"])
            if not math.isfinite(norm) or norm == 0:
                raise ModelOutputError("vLLM returned an invalid embedding norm")
            output["vector"] = [value / norm for value in output["vector"]]
            outputs.append(output)
        return validate_embeddings(prepared, outputs, self.capabilities)
