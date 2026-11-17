"""Feishu v2/v3 credential rotation with a durable unknown-outcome barrier.

The broker operates only on explicitly supplied OAuth credentials. Interactive
initial authorization and account consent remain outside source collection.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from ..durability import sync_dir
from .base import AdapterError
from .http import SourceHTTP, environment_secret, json_bytes

OAUTH_CONFIG_SCHEMA = {
    "type": "object",
    "required": ["credential_file", "client_id_ref", "client_secret_ref"],
    "additionalProperties": False,
    "properties": {
        "protocol": {"enum": ["authen_v2", "oauth_v3"]},
        "credential_file": {"type": "string", "minLength": 1},
        "client_id_ref": {"type": "string", "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$"},
        "client_secret_ref": {
            "type": "string",
            "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$",
        },
    },
}
FORMAT = "memulife.feishu-oauth.v1"


def _secret(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32768
        or "\r" in value
        or "\n" in value
    ):
        raise AdapterError("auth_required")
    return value


def _check_path(path: Path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise AdapterError("credential_path_unsafe")


def _private_read(path: Path) -> dict:
    _check_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise AdapterError("credential_permissions_invalid")
        raw = source.read(65537)
    if len(raw) > 65536:
        raise AdapterError("credential_file_invalid")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise AdapterError("credential_file_invalid") from None
    if not isinstance(value, dict):
        raise AdapterError("credential_file_invalid")
    return value


def _private_write(path: Path, value: dict):
    _check_path(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as target:
            target.write(json_bytes(value))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class FeishuOAuth:
    def __init__(
        self, config: dict, http: SourceHTTP, principal_id: str, *, now=time.time
    ):
        self.config = config
        self.http = http
        self.principal_id = principal_id
        self.path = Path(config["credential_file"]).expanduser().absolute()
        self.pending = self.path.with_name(self.path.name + ".refresh-pending")
        self.now = now
        self.protocol = config.get("protocol", "authen_v2")
        self.token_http = (SourceHTTP("https://accounts.feishu.cn/oauth/v3", client=http.client)
                           if self.protocol == "oauth_v3" else http)
        self.token_path = "/token" if self.protocol == "oauth_v3" else "/authen/v2/oauth/token"
        self.last_report = {
            "auth_protocol": self.protocol,
            "background_refresh": False,
            "refresh_probe_result": "not_probed",
        }

    @contextmanager
    def _locked(self):
        _check_path(self.path)
        lock_path = self.path.with_name(self.path.name + ".lock")
        _check_path(lock_path)
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+b") as lock:
                info = os.fstat(lock.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()
                ):
                    raise AdapterError("credential_permissions_invalid")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
        except OSError:
            raise AdapterError("credential_storage_unavailable") from None

    def _load(self):
        value = _private_read(self.path)
        if (
            value.get("format") != FORMAT
            or value.get("auth_protocol", "authen_v2") != self.protocol
            or value.get("principal_id") != self.principal_id
            or value.get("issuer") != self.http.base_url
            or value.get("client_id")
            != environment_secret(self.config["client_id_ref"])
        ):
            raise AdapterError("credential_binding_mismatch")
        if type(value.get("generation")) is not int or value["generation"] < 1:
            raise AdapterError("credential_file_invalid")
        if (
            not isinstance(value.get("token_type"), str)
            or value["token_type"].lower() != "bearer"
            or not isinstance(value.get("scope"), str)
        ):
            raise AdapterError("auth_required")
        for key in ("access_token", "refresh_token"):
            _secret(value.get(key))
        for key in ("access_expires_at", "refresh_expires_at"):
            if type(value.get(key)) not in {int, float} or not 0 < value[key] < 10**12:
                raise AdapterError("auth_required")
        return value

    def _record(self, response: dict, generation: int):
        if (
            not isinstance(response, dict)
            or response.get("error")
            or response.get("error_description")
            or (
                "code" in response
                and (type(response["code"]) is not int or response["code"] != 0)
            )
        ):
            raise AdapterError("auth_required")
        token_type = response.get("token_type")
        if (
            not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or not isinstance(response.get("scope"), str)
        ):
            raise AdapterError("auth_required")
        for key in ("expires_in", "refresh_token_expires_in"):
            if type(response.get(key)) is not int or not 0 < response[key] <= 2**31 - 1:
                raise AdapterError("auth_required")
        stamp = self.now()
        return {
            "format": FORMAT,
            "auth_protocol": self.protocol,
            "generation": generation,
            "principal_id": self.principal_id,
            "client_id": environment_secret(self.config["client_id_ref"]),
            "issuer": self.http.base_url,
            "access_token": _secret(response.get("access_token")),
            "refresh_token": _secret(response.get("refresh_token")),
            "token_type": "Bearer",
            "scope": response["scope"],
            "issued_at": stamp,
            "access_expires_at": stamp + response["expires_in"],
            "refresh_expires_at": stamp + response["refresh_token_expires_in"],
            "refresh_probe_result": "passed" if generation > 1 else "not_probed",
        }

    def initialize(self, authorization_response: dict):
        """Store an already-authorized response; performs no network operation."""
        with self._locked():
            if self.path.exists() or self.pending.exists():
                raise AdapterError("credential_already_initialized")
            _private_write(self.path, self._record(authorization_response, 1))

    def _remove_pending(self):
        self.pending.unlink(missing_ok=True)
        sync_dir(self.path.parent)

    def _report(self, value):
        self.last_report = {
            "auth_protocol": self.protocol,
            "token_endpoint": self.token_http.base_url + self.token_path,
            "token_type": "Bearer",
            "granted_scopes": value["scope"].split(),
            "refresh_token_present": True,
            "access_expiry_present": True,
            "refresh_expiry_present": True,
            "refresh_probe_result": value.get("refresh_probe_result", "not_probed"),
            "background_refresh": value.get("refresh_probe_result") == "passed"
            and value["refresh_expires_at"] > self.now(),
        }

    def access_token(self) -> str:
        with self._locked():
            current = self._load()
            if self.pending.exists():
                pending = _private_read(self.pending)
                if (
                    type(pending.get("generation")) is not int
                    or current["generation"] <= pending["generation"]
                ):
                    raise AdapterError("refresh_outcome_unknown")
                # Rotation was committed before a crash while removing its intent.
                self._remove_pending()
            margin = min(
                30,
                max(
                    0,
                    (
                        current["access_expires_at"]
                        - current.get("issued_at", current["access_expires_at"])
                    )
                    / 10,
                ),
            )
            if current["access_expires_at"] > self.now() + margin:
                self._report(current)
                return current["access_token"]
            if current["refresh_expires_at"] <= self.now():
                raise AdapterError("auth_required")
            body = {
                "grant_type": "refresh_token",
                "client_id": environment_secret(self.config["client_id_ref"]),
                "client_secret": environment_secret(self.config["client_secret_ref"]),
                "refresh_token": current["refresh_token"],
            }
            _private_write(
                self.pending,
                {
                    "generation": current["generation"],
                    "attempt_id": str(uuid4()),
                    "started_at": self.now(),
                },
            )
            try:
                response, _ = self.token_http.json(
                    "POST",
                    self.token_path,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    body=body,
                    max_bytes=65536,
                )
            except AdapterError as error:
                if error.code in {
                    "auth_required",
                    "source_access_denied",
                    "source_request_rejected",
                    "throttled",
                }:
                    self._remove_pending()
                    raise AdapterError(
                        "auth_required" if error.code != "throttled" else "throttled",
                        error.retryable,
                    ) from None
                # The request may have rotated a single-use refresh token. Keep
                # the original private file and intent; never blindly resend it.
                raise AdapterError("refresh_outcome_unknown") from None
            if (
                response.get("error")
                or response.get("error_description")
                or response.get("code", 0) != 0
            ):
                self._remove_pending()
                raise AdapterError("auth_required")
            updated = self._record(response, current["generation"] + 1)
            if self._load()["generation"] != current["generation"]:
                raise AdapterError("credential_generation_conflict")
            _private_write(self.path, updated)
            self._remove_pending()
            self._report(updated)
            return updated["access_token"]
