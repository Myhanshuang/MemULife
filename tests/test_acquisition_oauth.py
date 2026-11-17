"""Token rotation fixtures never contact a real account or use user credentials."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import stat
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.feishu import FeishuAdapter
from memu.acquisition.adapters.http import SourceHTTP
from memu.acquisition.adapters.oauth import FeishuOAuth
from test_acquisition_pipeline import acquisition


def token_response(**overrides):
    return {
        "code": 0,
        "access_token": "access-private-initial",
        "refresh_token": "refresh-private-initial",
        "expires_in": 60,
        "refresh_token_expires_in": 6000,
        "token_type": "Bearer",
        "scope": "offline_access docx:document:readonly",
        **overrides,
    }


def broker(tmp_path, monkeypatch, handler, clock=None):
    monkeypatch.setenv("FEISHU_APP_ID_TEST", "app-test")
    monkeypatch.setenv("FEISHU_APP_SECRET_TEST", "app-private-secret")
    config = {
        "credential_file": str(tmp_path / "oauth.json"),
        "client_id_ref": "env:FEISHU_APP_ID_TEST",
        "client_secret_ref": "env:FEISHU_APP_SECRET_TEST",
    }
    http = SourceHTTP(
        "https://open.feishu.cn/open-apis",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return FeishuOAuth(config, http, "user-1", now=lambda: clock[0] if clock else 1000)


@pytest.mark.parametrize("protocol", ["authen_v2", "oauth_v3"])
def test_refresh_uses_exact_protocol_and_commits_rotation_before_return(
    tmp_path, monkeypatch, protocol
):
    clock = [1000]
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == ("https://accounts.feishu.cn/oauth/v3/token" if protocol == "oauth_v3"
                               else "https://open.feishu.cn/open-apis/authen/v2/oauth/token")
        assert request.headers["content-type"] == "application/json; charset=utf-8"
        assert json.loads(request.content) == {
            "grant_type": "refresh_token",
            "client_id": "app-test",
            "client_secret": "app-private-secret",
            "refresh_token": "refresh-private-initial",
        }
        return httpx.Response(
            200,
            json=token_response(
                access_token="access-rotated",
                refresh_token="refresh-rotated",
                expires_in=1234,
                refresh_token_expires_in=4321,
            ),
        )

    oauth = broker(tmp_path, monkeypatch, handler, clock)
    oauth = FeishuOAuth({**oauth.config, "protocol": protocol}, oauth.http, "user-1", now=lambda: clock[0])
    oauth.initialize(token_response())
    before = json.loads(oauth.path.read_text())
    assert before["generation"] == 1
    assert oauth.access_token() == "access-private-initial"
    assert not requests
    clock[0] = 1100
    assert oauth.access_token() == "access-rotated"
    saved = json.loads(oauth.path.read_text())
    assert saved["generation"] == 2
    assert saved["refresh_token"] == "refresh-rotated"
    assert saved["access_expires_at"] == 2334
    assert saved["refresh_expires_at"] == 5421
    assert stat.S_IMODE(oauth.path.stat().st_mode) == 0o600
    assert not oauth.pending.exists()
    assert oauth.last_report["background_refresh"] is True
    assert "access-rotated" not in json.dumps(oauth.last_report)
    assert "refresh-rotated" not in json.dumps(oauth.last_report)


def test_web_authorization_binds_owner_exchanges_code_once_and_stores_refresh_privately(acquisition, monkeypatch):
    from memu.acquisition.feishu_authorization import CALLBACK, SCOPES
    from memu.acquisition.adapters.http import environment_secret
    service, _, _, client = acquisition
    calls = []
    native_client = httpx.Client
    def handler(request):
        calls.append(request)
        if request.url.path == '/open-apis/authen/v2/oauth/token':
            body = json.loads(request.content)
            assert body['grant_type'] == 'authorization_code' and body['code'] == 'one-time-code'
            assert body['redirect_uri'] == 'http://localhost:5073' + CALLBACK
            assert len(body['code_verifier']) >= 43
            import base64, hashlib
            assert base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).decode().rstrip('=') == query['code_challenge'][0]
            return httpx.Response(200, json=token_response(scope=' '.join(SCOPES)))
        assert request.url.path == '/open-apis/authen/v1/user_info'
        assert request.headers['Authorization'] == 'Bearer access-private-initial'
        return httpx.Response(200, json={'code': 0, 'data': {'open_id': 'user-1'}})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: native_client(transport=httpx.MockTransport(handler), **kwargs))
    start = {'name': '飞书', 'client_id': 'cli_test', 'client_secret': 'private-app-secret',
             'redirect_uri': 'http://localhost:5073' + CALLBACK}
    with pytest.raises(AdapterError, match='feishu_redirect_invalid'):
        service.feishu_authorization.start('owner', start, 'https://other.example')
    ticket = service.feishu_authorization.start('owner', start, 'http://localhost:5073')
    query = parse_qs(urlsplit(ticket['authorization_url']).query)
    assert query['code_challenge_method'] == ['S256'] and query['scope'] == [' '.join(SCOPES)]
    assert not calls and not service.state['sources'] and 'private-app-secret' not in json.dumps(ticket)
    with pytest.raises(AdapterError, match='scope_denied'):
        service.feishu_authorization.complete('reader', {'state': ticket['state'], 'code': 'one-time-code'})
    result = service.feishu_authorization.complete('owner', {'state': ticket['state'], 'code': 'one-time-code'})
    binding = service.binding('owner', result['source_id'])
    assert binding['config']['browse_accessible'] and binding['config']['principal_id'] == 'user-1'
    assert binding['config']['oauth']['protocol'] == 'oauth_v3'
    assert environment_secret(binding['config']['oauth']['client_secret_ref']) == 'private-app-secret'
    from pathlib import Path
    credential = Path(binding['config']['oauth']['credential_file'])
    assert json.loads(credential.read_text())['refresh_token'] == 'refresh-private-initial'
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert not service.state['jobs'] and not client.calls and len(calls) == 2
    assert 'private-app-secret' not in service.path.read_text()
    assert 'access-private-initial' not in service.path.read_text()
    with pytest.raises(AdapterError, match='feishu_authorization_expired'):
        service.feishu_authorization.complete('owner', {'state': ticket['state'], 'code': 'one-time-code'})
    assert len(calls) == 2


def test_authorization_preserves_numeric_error_without_secret_text(monkeypatch):
    from memu.acquisition.feishu_authorization import FeishuAuthorizationHTTP
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400,
        json={'code': 20002, 'error_description': 'PRIVATE_SECRET_DO_NOT_DISPLAY'})))
    http = FeishuAuthorizationHTTP('https://accounts.feishu.cn/oauth/v3', client=client)
    try:
        with pytest.raises(AdapterError, match='^feishu_oauth_20002$'):
            http.json('POST', '/token', headers={}, body={})
    finally:
        client.close()


def test_concurrent_refresh_reads_latest_generation_instead_of_reusing_token(
    tmp_path, monkeypatch
):
    clock = [1000]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=token_response(
                access_token="fresh-access",
                refresh_token="fresh-refresh",
                expires_in=900,
            ),
        )

    first = broker(tmp_path, monkeypatch, handler, clock)
    second = broker(tmp_path, monkeypatch, handler, clock)
    first.initialize(token_response())
    clock[0] = 1100
    barrier = threading.Barrier(2)

    def refresh(instance):
        barrier.wait()
        return instance.access_token()

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(refresh, [first, second]))
    assert results == ["fresh-access", "fresh-access"]
    assert len(calls) == 1
    assert json.loads(first.path.read_text())["generation"] == 2


def test_unknown_outcome_preserves_credentials_and_never_resubmits_single_use_token(
    tmp_path, monkeypatch
):
    clock = [1000]
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("could contain private upstream details")

    oauth = broker(tmp_path, monkeypatch, handler, clock)
    oauth.initialize(token_response())
    original = oauth.path.read_bytes()
    clock[0] = 1100
    for _ in range(2):
        with pytest.raises(AdapterError, match="refresh_outcome_unknown") as error:
            oauth.access_token()
        assert not error.value.retryable
        assert "private" not in str(error.value)
    assert len(calls) == 1
    assert oauth.path.read_bytes() == original
    assert oauth.pending.exists()
    assert b"refresh-private" not in oauth.pending.read_bytes()


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_in": 0},
        {"expires_in": True},
        {"refresh_token_expires_in": None},
        {"refresh_token": None},
        {"token_type": "DPoP"},
        {"scope": None},
    ],
)
def test_invalid_refresh_success_is_not_accepted_and_token_file_unchanged(
    tmp_path, monkeypatch, overrides
):
    clock = [1000]
    oauth = broker(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(200, json=token_response(**overrides)),
        clock,
    )
    oauth.initialize(token_response())
    original = oauth.path.read_bytes()
    clock[0] = 1100
    with pytest.raises(AdapterError, match="auth_required"):
        oauth.access_token()
    assert oauth.path.read_bytes() == original
    assert (
        oauth.pending.exists()
    )  # malformed success may still have consumed refresh token


@pytest.mark.parametrize(
    "status, body",
    [
        (400, {"error": "invalid_grant"}),
        (200, {"code": 20064, "error_description": "refresh-private-initial"}),
    ],
)
def test_explicit_rejection_never_overwrites_or_logs_credentials(
    tmp_path, monkeypatch, status, body
):
    clock = [1000]
    oauth = broker(
        tmp_path, monkeypatch, lambda request: httpx.Response(status, json=body), clock
    )
    oauth.initialize(token_response())
    original = oauth.path.read_bytes()
    clock[0] = 1100
    with pytest.raises(AdapterError, match="auth_required") as error:
        oauth.access_token()
    assert "refresh-private" not in str(error.value)
    assert oauth.path.read_bytes() == original
    assert not oauth.pending.exists()


def test_expired_refresh_does_not_contact_server(tmp_path, monkeypatch):
    calls = []
    clock = [1000]
    oauth = broker(tmp_path, monkeypatch, lambda request: calls.append(request), clock)
    oauth.initialize(token_response(refresh_token_expires_in=70))
    clock[0] = 1100
    with pytest.raises(AdapterError, match="auth_required"):
        oauth.access_token()
    assert not calls


def test_private_file_and_no_symlink_enforced(tmp_path, monkeypatch):
    oauth = broker(
        tmp_path, monkeypatch, lambda request: pytest.fail("no network expected")
    )
    oauth.initialize(token_response())
    oauth.path.chmod(0o644)
    with pytest.raises(AdapterError, match="credential_permissions_invalid"):
        oauth.access_token()
    oauth.path.chmod(0o600)
    original = oauth.path.with_name("original.json")
    oauth.path.rename(original)
    oauth.path.symlink_to(original)
    with pytest.raises(AdapterError, match="credential_path_unsafe"):
        oauth.access_token()
    assert original.exists()


def test_rotation_committed_before_crash_recovers_pending_intent_without_request(
    tmp_path, monkeypatch
):
    oauth = broker(
        tmp_path, monkeypatch, lambda request: pytest.fail("no repeat refresh")
    )
    oauth.initialize(token_response())
    record = json.loads(oauth.path.read_text())
    record.update(
        generation=2,
        access_token="already-committed",
        refresh_token="already-rotated",
        refresh_probe_result="passed",
    )
    oauth.path.write_text(json.dumps(record))
    oauth.pending.write_text(json.dumps({"generation": 1}))
    oauth.pending.chmod(0o600)
    assert oauth.access_token() == "already-committed"
    assert not oauth.pending.exists()


def test_feishu_integration_preserves_principal_check_and_existing_external_token(
    tmp_path, monkeypatch
):
    clock = [1000]

    def handler(request):
        if request.url.path.endswith("/authen/v1/user_info"):
            assert request.headers["authorization"] == "Bearer access-private-initial"
            return httpx.Response(
                200, json={"code": 0, "data": {"open_id": "wrong-user"}}
            )
        pytest.fail("unexpected request")

    oauth = broker(tmp_path, monkeypatch, handler, clock)
    oauth.initialize(token_response(expires_in=2000000000))
    adapter = FeishuAdapter(
        {
            "oauth": deepcopy(oauth.config),
            "principal_id": "user-1",
            "folder_tokens": ["root"],
        },
        client=oauth.http.client,
    )
    with pytest.raises(AdapterError, match="account_scope_mismatch"):
        adapter.probe()
    with pytest.raises(AdapterError, match="config_invalid"):
        FeishuAdapter(
            {
                "oauth": oauth.config,
                "access_token_ref": "env:TOKEN",
                "principal_id": "user-1",
                "folder_tokens": ["root"],
            }
        )
