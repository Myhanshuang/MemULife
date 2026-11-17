"""Provider-neutral HTTP model gateways; no silent local or caption fallback.

MLLM: POST /tasks {model, task, messages, output_schema, ...} -> task JSON.
Embedding: GET /capabilities?model=...; POST /embed {model, items} ->
{"embeddings": [{"vector": [...], "space_id": ..., "model_version": ...,
                  "preprocess_version": ...}]}.
Media items carry actual base64 bytes and optional original-timeline bounds.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import math
import os
from pathlib import Path
from typing import Protocol

import httpx

from .config import EndpointConfig


class ModelConfigurationError(ValueError):
    pass


class UnsupportedModalityError(ValueError):
    pass


class ModelOutputError(ValueError):
    code = 'model_output_invalid'


class OutputTruncated(ModelOutputError):
    code = 'model_output_truncated'


class MLLM(Protocol):
    def complete(self, request: dict) -> dict: ...


class EmbeddingModel(Protocol):
    capabilities: dict

    def embed(self, items: list[dict]) -> list[dict]: ...


def validate_capabilities(value: dict, *, required_modalities=None) -> dict:
    if not isinstance(value, dict):
        raise ModelConfigurationError("Embedding capabilities must be an object")
    caps = deepcopy(value)
    required = {"modalities", "dimension", "metric", "space_id", "model_version", "preprocess_version"}
    if not required <= caps.keys():
        raise ModelConfigurationError("Embedding gateway omitted required capability metadata")
    modalities = caps["modalities"]
    if not isinstance(modalities, list) or not modalities or any(
            mode not in {"text", "image", "audio", "video"} for mode in modalities):
        raise ModelConfigurationError("Invalid embedding modalities")
    if "text" not in modalities:
        raise ModelConfigurationError("Embedding must support Unit text")
    if required_modalities is not None and not set(required_modalities) <= set(modalities):
        missing = set(required_modalities) - set(modalities)
        raise UnsupportedModalityError(f"Embedding does not support: {', '.join(sorted(missing))}")
    if type(caps["dimension"]) is not int or caps["dimension"] < 1:
        raise ModelConfigurationError("Embedding dimension must be a positive integer")
    if caps["metric"] not in {"cosine", "l2", "inner_product"}:
        raise ModelConfigurationError("Embedding metric is not supported by the vector index")
    for name in ["space_id", "model_version", "preprocess_version"]:
        if not isinstance(caps[name], str) or not caps[name].strip():
            raise ModelConfigurationError(f"Embedding {name} must be a nonblank string")
    limits = caps.get("max_duration_seconds", {})
    if isinstance(limits, (int, float)) and not isinstance(limits, bool):
        limits = {mode: limits for mode in ["audio", "video"] if mode in modalities}
    if not isinstance(limits, dict):
        raise ModelConfigurationError("max_duration_seconds must be a positive number or per-modality object")
    for mode in ["audio", "video"]:
        if mode in modalities:
            maximum = limits.get(mode)
            if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(maximum) or maximum <= 0:
                raise ModelConfigurationError(f"Embedding gateway must declare {mode} maximum duration")
    caps["max_duration_seconds"] = limits
    return caps


def prepare_embedding_items(items: list[dict], capabilities: dict) -> list[dict]:
    prepared = []
    for original in items:
        if not isinstance(original, dict):
            raise ModelOutputError("Embedding item must be an object")
        item = deepcopy(original)
        kind = item.get("kind")
        if kind not in capabilities["modalities"]:
            raise UnsupportedModalityError(f"Embedding model does not support {kind!r}; caption fallback is disabled")
        if kind == "text":
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                raise ModelOutputError("Text embedding requires nonblank text")
            if any(key in item for key in ["path", "data_base64"]):
                raise ModelOutputError("Text embedding item must not contain media bytes")
        else:
            if "text" in item:
                raise ModelOutputError("Native media embedding must not substitute caption text")
            if "path" in item:
                if "data_base64" in item:
                    raise ModelOutputError("Choose media path or bytes, not both")
                item["data_base64"] = base64.b64encode(Path(item.pop("path")).read_bytes()).decode("ascii")
            encoded = item.get("data_base64")
            if not isinstance(encoded, str):
                raise ModelOutputError("Native media embedding requires original bytes")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ModelOutputError("Invalid media base64") from error
            if not decoded:
                raise ModelOutputError("Native media must not be empty")
            mime = item.get("mime_type")
            if not isinstance(mime, str) or not mime.startswith(kind + "/"):
                raise ModelOutputError("Media MIME type must match its modality")
            if kind in {"audio", "video"}:
                start, end = item.get("start"), item.get("end")
                if (isinstance(start, bool) or isinstance(end, bool) or
                    not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or
                    not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
                    raise ModelOutputError("Audio/video requires original-timeline start/end seconds")
                maximum = capabilities.get("max_duration_seconds", {})
                if isinstance(maximum, dict):
                    maximum = maximum.get(kind)
                if maximum is None or end - start > maximum + 1e-9:
                    raise ModelOutputError("Media item exceeds model duration limit; split before encoding")
        prepared.append(item)
    return prepared


def validate_embeddings(items: list[dict], outputs: list[dict], capabilities: dict) -> list[dict]:
    if not isinstance(outputs, list) or len(outputs) != len(items):
        raise ModelOutputError("Embedding response count does not match input count")
    checked = []
    for item, output in zip(items, outputs):
        if not isinstance(output, dict):
            raise ModelOutputError("Embedding output must be an object")
        vector = output.get("vector")
        if not isinstance(vector, list) or len(vector) != capabilities["dimension"]:
            raise ModelOutputError("Embedding dimension does not match advertised capability")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ModelOutputError("Embedding contains a nonfinite or nonnumeric value")
        if capabilities["metric"] == "cosine" and not any(value != 0 for value in vector):
            raise ModelOutputError("Zero embedding has undefined cosine distance")
        for name in ["space_id", "model_version", "preprocess_version"]:
            if output.get(name) != capabilities[name]:
                raise ModelOutputError(f"Embedding {name} differs from active capability")
        if "kind" in output and output["kind"] != item["kind"]:
            raise ModelOutputError("Embedding response encoded a different modality")
        if "metric" in output and output["metric"] != capabilities["metric"]:
            raise ModelOutputError("Embedding metric differs from active capability")
        checked.append({**output, "metric": capabilities["metric"], "vector": [float(value) for value in vector]})
    return checked


class _HTTPGateway:
    def __init__(self, config: EndpointConfig | dict, *, client: httpx.Client | None = None):
        self.config = config if isinstance(config, EndpointConfig) else EndpointConfig.model_validate(config)
        if not self.config.endpoint or not self.config.model or self.config.model == "configured-by-owner":
            raise ModelConfigurationError("Owner must configure model name and gateway endpoint")
        url = httpx.URL(self.config.endpoint)
        if url.scheme not in {"http", "https"} or not url.host or url.username or url.password:
            raise ModelConfigurationError("Model endpoint must be an HTTP(S) URL without embedded credentials")
        self.endpoint = self.config.endpoint.rstrip("/")
        self.headers = {}
        if self.config.api_key_env:
            key = os.environ.get(self.config.api_key_env)
            if not key:
                raise ModelConfigurationError(f"Required credential environment variable is not set: {self.config.api_key_env}")
            self.headers["Authorization"] = f"Bearer {key}"
        # No retry transport: a timed-out task is not silently submitted again.
        self.client = client or httpx.Client(timeout=self.config.timeout_seconds)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self.client.close()

    def _request(self, method, route, **kwargs):
        response = self.client.request(method, self.endpoint + route, headers=self.headers,
                                       timeout=self.config.timeout_seconds, **kwargs)
        response.raise_for_status()
        return response.json()


class HTTPMLLM(_HTTPGateway):
    def complete(self, request: dict) -> dict:
        value = self._request("POST", "/tasks", json={"model": self.config.model, **request})
        if not isinstance(value, dict):
            raise ModelOutputError("MLLM gateway returned a non-object response")
        return value


class HTTPEmbedding(_HTTPGateway):
    def __init__(self, config: EndpointConfig | dict, *, client: httpx.Client | None = None):
        super().__init__(config, client=client)
        try:
            self.capabilities = validate_capabilities(
                self._request("GET", "/capabilities", params={"model": self.config.model}))
        except Exception:
            self.close()
            raise

    def embed(self, items: list[dict]) -> list[dict]:
        prepared = prepare_embedding_items(items, self.capabilities)
        if not prepared:
            return []
        result = self._request("POST", "/embed", json={"model": self.config.model, "items": prepared})
        if not isinstance(result, dict) or set(result) != {"embeddings"}:
            raise ModelOutputError("Embedding gateway must return an embeddings object")
        return validate_embeddings(prepared, result["embeddings"], self.capabilities)
