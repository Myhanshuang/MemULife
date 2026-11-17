import hashlib
import json
import sqlite3

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.qq import QQAdapter
from memu.acquisition.adapters.wechat import WeChatAdapter


def qq(monkeypatch, responses=None, conversations=None):
    monkeypatch.setenv("QQ_TEST_TOKEN", "private-test-token")
    called = []

    def handler(request):
        called.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer private-test-token"
        action = request.url.path.rsplit("/", 1)[-1]
        data = (responses or {}).get(
            action,
            {"user_id": "123"} if action == "get_login_info" else {"online": True},
        )
        if isinstance(data, httpx.Response):
            return data
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    adapter = QQAdapter(
        {
            "base_url": "http://127.0.0.1:3000",
            "account_id": "123",
            "token_env": "QQ_TEST_TOKEN",
            "conversations": conversations or [{"type": "private", "id": "456"}],
            "history_order": "newest_first",
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return adapter, called


def message(native, short=7, text="text", **extra):
    return {
        "self_id": "123",
        "post_type": "message",
        "message_type": "private",
        "target_id": "456",
        "user_id": "123",
        "sender": {"user_id": "123", "nickname": "me"},
        "time": 1700000000,
        "raw": {"msgId": str(native)},
        "message_id": short,
        "message": [{"type": "text", "data": {"text": text}}],
        **extra,
    }


def test_qq_scope_self_private_and_long_ids(monkeypatch):
    adapter, called = qq(monkeypatch)
    page = adapter.event(message(900719925474099312345))
    record = page.records[0]
    assert record.object_id == "123:private:456:900719925474099312345"
    assert record.metadata["actor"]["role"] == "self"
    assert record.metadata["conversation_id"] == "private:456"
    assert json.loads(record.raw)["raw"]["msgId"] == "900719925474099312345"
    with pytest.raises(AdapterError, match="conversation_scope_denied"):
        adapter.event(message("other", target_id="unauthorized"))
    with pytest.raises(AdapterError, match="action_not_allowed"):
        adapter.rpc("send_private_msg", {"user_id": "456", "message": "wrong"})
    assert not called


def test_qq_short_id_collision_retains_both_and_ambiguous_recall(monkeypatch):
    adapter, _ = qq(monkeypatch)
    first = adapter.event(message("native-one", short=44))
    second = adapter.event(message("native-two", short=44), first.cursor)
    assert first.records[0].object_id != second.records[0].object_id
    recall = adapter.event(
        {
            "self_id": "123",
            "post_type": "notice",
            "notice_type": "friend_recall",
            "user_id": "456",
            "message_id": 44,
        },
        second.cursor,
    )
    assert recall.warnings == ["recall_identity_unresolved"]
    assert not recall.records[0].deleted
    assert recall.records[0].metadata["unresolved_recall"]


def test_qq_unambiguous_recall_tombstone_and_unknown_parts(monkeypatch):
    adapter, _ = qq(monkeypatch)
    original = message(
        "native", message=[{"type": "unknown_part", "data": {"opaque": "preserved"}}]
    )
    first = adapter.event(original)
    recalled = adapter.event(
        {
            "self_id": "123",
            "post_type": "notice",
            "notice_type": "friend_recall",
            "user_id": "456",
            "message_id": 7,
        },
        first.cursor,
    )
    assert recalled.records[0].object_id == first.records[0].object_id
    assert (
        recalled.records[0].deleted
        and recalled.records[0].metadata["reason"] == "revoked"
    )
    assert b"opaque" in first.records[0].raw


def test_qq_history_opaque_anchor_not_max_short_id_and_loop_rejected(monkeypatch):
    response = {"messages": [message("new", short=1), message("old", short=999)]}
    adapter, called = qq(monkeypatch, {"get_friend_msg_history": response})
    page = adapter.read("backfill", limit=2)
    assert page.cursor["history"]["private:456"]["anchor"] == "old"
    assert not page.scope_complete
    assert all(
        record.attachments[-1].attachment_id == "source-page" for record in page.records
    )
    with pytest.raises(AdapterError, match="pagination_loop"):
        adapter.read("backfill", page.cursor, limit=2)
    assert page.cursor["history"]["private:456"]["anchor"] == "old"


def test_qq_error_empty_history_not_success(monkeypatch):
    adapter, _ = qq(
        monkeypatch,
        {
            "get_friend_msg_history": httpx.Response(
                200, json={"status": "failed", "retcode": 1200, "data": None}
            )
        },
    )
    with pytest.raises(AdapterError, match="upstream_read_failed"):
        adapter.read("backfill")


def test_qq_probe_account_mismatch_and_missing_auth(monkeypatch):
    adapter, _ = qq(monkeypatch, {"get_login_info": {"user_id": "wrong"}})
    with pytest.raises(AdapterError, match="account_binding_mismatch"):
        adapter.probe()
    monkeypatch.delenv("QQ_TEST_TOKEN")
    with pytest.raises(AdapterError, match="auth_required"):
        adapter.rpc("get_login_info")


def test_qq_discovery_persists_only_authorized_contacts(monkeypatch):
    adapter, _ = qq(
        monkeypatch,
        {
            "get_friend_list": [
                {"user_id": "456", "remark": "allowed"},
                {"user_id": "private-other", "remark": "do-not-store"},
            ],
            "get_group_list": [],
        },
    )
    page = adapter.read("discover")
    assert len(page.records) == 1
    assert b"do-not-store" not in page.records[0].raw
    assert not page.scope_complete


def create_wechat(tmp_path, rows_by_shard, fresh=True):
    conversation = "group@chatroom"
    table = "Msg_" + hashlib.md5(conversation.encode()).hexdigest()
    descriptors = []
    connections = []
    for number, rows in enumerate(rows_by_shard):
        name = f"message_{number}.db"
        connection = sqlite3.connect(tmp_path / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.executemany(
            "INSERT INTO Name2Id(user_name) VALUES (?)",
            [(f"sender-{number}",), ("wxid-self",)],
        )
        connection.execute(
            f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,sort_seq INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,packed_info_data BLOB)'
        )
        connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)', rows)
        connection.commit()
        connections.append(connection)
        descriptors.append(
            {
                "shard_id": name,
                "path": name,
                "db_generation": "g1",
                "wal_applied": fresh,
                "used_previous_snapshot": not fresh,
                "freshness_state": "fresh" if fresh else "stale",
                "validation_status": "ok",
            }
        )
    (tmp_path / "snapshots.json").write_text(
        json.dumps(
            {
                "format": "memulife.wechat-snapshots.v1",
                "account_id": "wxid-self",
                "shards": descriptors,
            }
        )
    )
    adapter = WeChatAdapter(
        {
            "root": str(tmp_path),
            "account_id": "wxid-self",
            "conversations": [conversation],
        }
    )
    return adapter, connections, table


def row(local_id, seq=9007199254740993, server=0, sender=1, content="text", kind=1):
    return (local_id, server, seq, kind, sender, 1700000000, content, b"\x00\xff")


def test_wechat_speakers_survive_contact_lookup_and_material_projection(tmp_path):
    from memu.acquisition.sources.chat_content import group_records
    from memu.memulife import snapshot_material

    adapter, connections, _ = create_wechat(tmp_path, [[
        row(1, content='周五见'), row(2, sender=2, content='好的'), row(3, sender=99),
    ]])
    with sqlite3.connect(tmp_path / 'contact.db') as contacts:
        contacts.execute('CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT)')
        contacts.executemany('INSERT INTO contact VALUES (?,?,?,?)', [
            ('sender-0', '小明\n同事', '昵称', 'alias'),
            ('wxid-self', '', '小明', ''),
            ('group@chatroom', '', '项目群', ''),
        ])
    manifest = tmp_path / 'snapshots.json'
    value = json.loads(manifest.read_text())
    value['databases'] = [{'source_path': 'contact/contact.db', 'path': 'contact.db'}]
    manifest.write_text(json.dumps(value))
    try:
        records = adapter.read('backfill').records
        assert records[0].metadata['actor']['display_name'] == '小明 同事'
        assert records[1].metadata['actor'] == {
            'source_actor_id': 'wxid-self', 'role': 'self', 'display_name': '小明'}
        assert records[2].metadata['actor']['role'] == 'unknown'
        projected = group_records(records)[0]
        material = snapshot_material({
            'adapter_id': 'wechat.localdb', 'source_profile': 'wechat',
            'source_instance_id': 'fixture', 'source_namespace': 'wechat:fixture'}, projected)
        text = material['text']
        assert '# 微信会话：项目群' in text
        assert '发言人：小明 同事 [微信 ID：sender-0]' in text
        assert '发言人：小明（我自己） [微信 ID：wxid-self]' in text
        assert '发言人：身份未知' in text
        assert material['metadata']['projection']['generation'] == 4
        lines = text.splitlines()
        for message in material['metadata']['content']['message_layout']:
            assert lines[message['start_line'] - 1].startswith('## 发言人：')
        # A carried page can be regrouped without duplicating speaker headers.
        assert group_records([projected])[0]['text'] == projected['text']
    finally:
        for connection in connections:
            connection.close()


def test_wechat_shards_same_seq_same_local_id_correct_sender_wal_visible(tmp_path):
    adapter, connections, _ = create_wechat(tmp_path, [[row(1), row(2)], [row(1)]])
    records, cursor = [], None
    for _ in range(5):
        page = adapter.read("backfill", cursor, limit=1)
        records.extend(page.records)
        cursor = page.cursor
        if not page.has_more:
            break
    assert len(records) == 3
    assert len({record.object_id for record in records}) == 3
    assert [record.metadata["actor"]["source_actor_id"] for record in records] == [
        "sender-0",
        "sender-0",
        "sender-1",
    ]
    assert all(
        json.loads(record.raw)["row"]["sort_seq"] == "9007199254740993"
        for record in records
    )
    assert json.loads(records[0].raw)["row"]["packed_info_data"] == {
        "encoding": "base64",
        "data": "AP8=",
    }
    for connection in connections:
        connection.close()


def test_wechat_stale_snapshot_not_complete_and_no_foreground(tmp_path):
    adapter, connections, _ = create_wechat(tmp_path, [[row(1, kind=3)]], fresh=False)
    assert adapter.probe()["status"] == "stale_snapshot"
    page = adapter.read("poll")
    assert not page.scope_complete
    assert "stale_snapshot" in page.warnings
    assert (
        page.records[0].attachments[0].metadata["reason"] == "not_downloaded_on_source"
    )
    assert adapter.health()["foreground_actions"] is False
    for connection in connections:
        connection.close()


def test_wechat_reconcile_old_row_updates_and_self_mapping(tmp_path):
    adapter, connections, table = create_wechat(tmp_path, [[row(1, sender=2), row(2)]])
    first = adapter.read("poll")
    assert first.records[0].metadata["actor"]["role"] == "self"
    connections[0].execute(
        f'UPDATE "{table}" SET message_content=? WHERE local_id=1', ("edited",)
    )
    connections[0].commit()
    second = adapter.read("reconcile", first.cursor)
    assert second.records[0].object_id == first.records[0].object_id
    assert second.records[0].text == "edited"
    connections[0].close()


def test_wechat_account_mismatch_and_partial_shard_error_not_tombstone(tmp_path):
    adapter, connections, _ = create_wechat(tmp_path, [[row(1)], [row(1)]])
    for connection in connections:
        connection.close()
    (tmp_path / "message_1.db").unlink()
    with pytest.raises(AdapterError, match="stale_snapshot"):
        adapter.read("poll")
    manifest = json.loads((tmp_path / "snapshots.json").read_text())
    manifest["account_id"] = "other-account"
    (tmp_path / "snapshots.json").write_text(json.dumps(manifest))
    with pytest.raises(AdapterError, match="account_binding_mismatch"):
        adapter.probe()


def test_qq_reconnect_subscription_requires_reconcile_and_retains_candidate_cursor(
    monkeypatch,
):
    from websockets.sync import client as websocket_client

    adapter, _ = qq(monkeypatch)
    adapter.config["ws_url"] = "ws://127.0.0.1:3001"

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            return iter(
                [
                    json.dumps({"self_id": "123", "post_type": "meta_event"}),
                    json.dumps(message("one")),
                ]
            )

    monkeypatch.setattr(websocket_client, "connect", lambda *args, **kwargs: Socket())
    pages = adapter.subscribe()
    first = next(pages)
    assert first.warnings == ["subscription_reconcile_required"]
    assert not first.scope_complete
    heartbeat = next(pages)
    assert not heartbeat.records
    second = next(pages)
    assert second.records[0].text == "text"
    assert "private:456:7" in second.cursor["aliases"]
    with pytest.raises(StopIteration):
        next(pages)


def test_chat_config_schemas_deny_writes():
    import jsonschema

    for cls in (QQAdapter, WeChatAdapter):
        jsonschema.Draft202012Validator.check_schema(cls.config_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "root": "/tmp",
                "account_id": "wx",
                "conversations": ["c"],
                "allow_foreground_actions": True,
            },
            WeChatAdapter.config_schema,
        )
