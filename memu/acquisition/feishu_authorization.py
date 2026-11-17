"""Owner-bound, single-use Feishu authorization; secrets never reach URLs."""

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from .adapters.base import AdapterError
from .adapters.http import SourceHTTP, identifier
from .adapters.oauth import FeishuOAuth, _secret

CALLBACK = "/manage/imports/feishu/oauth/callback"
SCOPES = ("contact:user.base:readonly", "drive:drive:readonly",
          "docx:document:readonly", "wiki:wiki:readonly", "offline_access")


def remote_error(value):
    code = value.get('code')
    if type(code) is int and 0 < code < 10**10:
        return AdapterError('feishu_oauth_' + str(code))
    return AdapterError('feishu_authorization_failed')


class FeishuAuthorizationHTTP(SourceHTTP):
    def _check_status(self, response):
        # Preserve only a numeric platform error. Never expose response text,
        # authorization codes, credentials or request headers in diagnostics.
        if 400 <= response.status_code < 500:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    break
            if len(body) <= 65536:
                try:
                    value = json.loads(body)
                    if isinstance(value, dict) and value.get('code'):
                        raise remote_error(value)
                except (ValueError, UnicodeDecodeError):
                    pass
        super()._check_status(response)


class FeishuAuthorization:
    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.lock = threading.Lock()

    def start(self, owner, body, origin):
        self.service.owner(owner)
        client_id, secret, name = body.get("client_id"), body.get("client_secret"), body.get("name")
        if not isinstance(client_id, str) or not re.fullmatch(r"cli_[A-Za-z0-9_]{1,128}", client_id):
            raise AdapterError("feishu_app_invalid")
        _secret(secret)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise AdapterError("source_namespace_required")
        parsed = urlsplit(origin or "")
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password
                or body.get("redirect_uri") != origin + CALLBACK):
            raise AdapterError("feishu_redirect_invalid")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self.lock:
            self.pending = {key: row for key, row in self.pending.items() if row["expires"] > time.monotonic()}
            if len(self.pending) >= 16:
                raise AdapterError("feishu_authorization_capacity")
            self.pending[state] = {"owner": owner, "client_id": client_id, "client_secret": secret,
                "name": name.strip(), "redirect_uri": body["redirect_uri"], "verifier": verifier,
                "expires": time.monotonic() + 600}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return {"state": state, "authorization_url": "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": body["redirect_uri"],
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "consent"})}

    def take(self, owner, state):
        self.service.owner(owner)
        with self.lock:
            row = self.pending.get(state) if isinstance(state, str) else None
            if not row or row["owner"] != owner:
                raise AdapterError("feishu_authorization_expired")
            del self.pending[state]
        if row["expires"] <= time.monotonic():
            raise AdapterError("feishu_authorization_expired")
        return row

    def cancel(self, owner, state):
        self.take(owner, state)
        return {"cancelled": True}

    def complete(self, owner, body):
        from ..events import event
        try:
            result = self._complete(owner, body)
            event('feishu_authorization_completed')
            return result
        except AdapterError as error:
            event('feishu_authorization_failed', error_code=error.code)
            raise

    def _complete(self, owner, body):
        row = self.take(owner, body.get("state"))
        if body.get("error"):
            raise AdapterError("feishu_authorization_denied")
        code = _secret(body.get("code"))
        # v3 code exchange currently rejects valid PKCE on Feishu self-built
        # apps (larksuite/oapi-sdk-go#230). Keep PKCE and use the paired v2
        # exchange; v3 refresh explicitly accepts tokens issued by v2.
        http = FeishuAuthorizationHTTP("https://open.feishu.cn/open-apis", max_bytes=65536)
        try:
            token, _ = http.json("POST", "/authen/v2/oauth/token", headers={"Content-Type": "application/json; charset=utf-8"}, body={
                "grant_type": "authorization_code", "client_id": row["client_id"],
                "client_secret": row["client_secret"], "code": code,
                "code_verifier": row["verifier"], "redirect_uri": row["redirect_uri"]})
        except AdapterError as error:
            if error.code in {"auth_required", "source_access_denied", "source_request_rejected"}:
                raise AdapterError("feishu_authorization_failed") from None
            raise
        finally:
            http.close()
        if token.get("code", 0) != 0 or token.get("error"):
            raise remote_error(token)
        if not token.get("refresh_token"):
            raise AdapterError("feishu_authorization_scopes")
        http = FeishuAuthorizationHTTP("https://open.feishu.cn/open-apis", max_bytes=65536)
        token_path = None
        registered = False
        try:
            user, _ = http.json("GET", "/authen/v1/user_info", headers={"Authorization": "Bearer " + _secret(token.get("access_token"))})
            if user.get("code") != 0:
                raise remote_error(user)
            principal = identifier(user.get("data", {}).get("open_id"), "account_binding_mismatch")
            credential_id = str(uuid4())
            token_path = self.service.root / "feishu-oauth" / (credential_id + ".json")
            token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            binding = {"adapter_id": "feishu.documents", "source_profile": "docx-v1", "source_namespace": row["name"],
                "config": {"principal_id": principal, "browse_accessible": True,
                    "oauth": {"credential_file": str(token_path), "protocol": "oauth_v3"}}}
            credentials = {"oauth.client_id_ref": row["client_id"], "oauth.client_secret_ref": row["client_secret"]}
            temporary, private = self.service.credentials.prepare(credential_id, binding, credentials)
            with self.service.credentials.temporary(private):
                FeishuOAuth(temporary["config"]["oauth"], http, principal).initialize(token)
            result = self.service.register(owner, binding, credentials=credentials)
            registered = True
            return result
        finally:
            http.close()
            if token_path is not None and not registered:
                token_path.unlink(missing_ok=True)
