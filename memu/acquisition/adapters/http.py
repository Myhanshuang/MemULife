"""Small HTTP boundary shared by read-only cloud sources.

Credentials are resolved for each request, never included in errors or raw records.
Redirects, ambient proxy credentials and unbounded downloads are deliberately absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import AdapterError


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def identifier(value: Any, code: str = "incompatible_schema") -> str:
    """Native identifiers must never pass through a lossy float conversion."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise AdapterError(code)
    return value


def environment_secret(reference: str) -> str:
    if not isinstance(reference, str) or not reference.startswith("env:"):
        raise AdapterError("credential_reference_invalid")
    name = reference.removeprefix("env:")
    if not name or not name.replace("_", "a").isalnum():
        raise AdapterError("credential_reference_invalid")
    value = os.environ.get(name)
    if not value or "\r" in value or "\n" in value:
        raise AdapterError("auth_required")
    return value


class SourceHTTP:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        allow_insecure_http: bool = False,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in ("https", "http")
            or (parsed.scheme == "http" and not (loopback or allow_insecure_http))
        ):
            raise AdapterError("source_url_invalid")
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=30.0, follow_redirects=False, trust_env=False
        )
        self.owns_client = client is None
        self.max_bytes = max_bytes
        self.last_error: str | None = None
        self.retry_after_seconds: int | None = None
        self.last_success_at: str | None = None

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict | None = None,
        body: dict | None = None,
        max_bytes: int | None = None,
    ) -> tuple[bytes, str]:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
        ):
            raise AdapterError("source_path_invalid")
        budget = self.max_bytes if max_bytes is None else max_bytes
        try:
            with self.client.stream(
                method,
                self.base_url + path,
                headers=headers,
                params=params,
                json=body,
                follow_redirects=False,
            ) as response:
                self._check_status(response)
                chunks: list[bytes] = []
                length = 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > budget:
                        raise AdapterError("response_too_large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = response.headers.get(
                    "content-type", "application/octet-stream"
                ).split(";", 1)[0]
        except httpx.HTTPError:
            self.last_error = "source_offline"
            raise AdapterError("source_offline", retryable=True) from None
        except AdapterError as error:
            self.last_error = str(error)
            raise
        self.last_success_at = datetime.now(timezone.utc).isoformat()
        self.last_error = None
        return data, mime

    def json(self, method: str, path: str, **kwargs: Any) -> tuple[dict, bytes]:
        raw, _ = self.request(method, path, **kwargs)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AdapterError("incompatible_schema") from None
        if not isinstance(value, dict):
            raise AdapterError("incompatible_schema")
        return value, raw

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            self.retry_after_seconds = None
            return
        if status == 429:
            self.retry_after_seconds = self._retry_after(
                response.headers.get("retry-after")
            )
            raise AdapterError("throttled", retryable=True)
        if status == 401:
            raise AdapterError("auth_required")
        if status == 403:
            raise AdapterError("source_access_denied")
        if status == 404:
            # A missing readable resource is not proof of source deletion.
            raise AdapterError("source_unavailable")
        if status >= 500:
            raise AdapterError("source_offline", retryable=True)
        if 300 <= status < 400:
            raise AdapterError("redirect_refused")
        raise AdapterError("source_request_rejected")

    @staticmethod
    def _retry_after(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            seconds = int(value)
        except ValueError:
            try:
                seconds = int(
                    (
                        parsedate_to_datetime(value) - datetime.now(timezone.utc)
                    ).total_seconds()
                )
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0, min(seconds, 86400))

    def health(self) -> dict:
        return {
            "state": self.last_error
            or ("ready" if self.last_success_at else "not_probed"),
            "last_success_at": self.last_success_at,
            "retry_after_seconds": self.retry_after_seconds,
        }
