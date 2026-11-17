"""Pre-import browsing never writes inboxes, originals or user source files."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.sources.selection import Selection
from memu.acquisition.sources.browsing import browse, preview
from test_acquisition_chat_sources import create_wechat, message, qq, row


def binding(adapter):
    return {
        "adapter_id": adapter.adapter_id,
        "source_profile": "onebot11-napcat"
        if adapter.adapter_id == "qq.napcat"
        else "readonly-sqlite",
        "config": adapter.config,
        "tenant_id": "test-tenant",
        "source_instance_id": "test-source",
    }


def inject_qq(monkeypatch, adapter):
    monkeypatch.setattr(
        "memu.acquisition.sources.browsing.QQAdapter", lambda config: adapter
    )


def test_qq_browse_names_only_filters_allowed_peers_and_paginates(monkeypatch):
    adapter, calls = qq(
        monkeypatch,
        {
            "get_friend_list": [
                {"user_id": "456", "nickname": "小陈", "remark": "项目同事"},
                {"user_id": "secret", "nickname": "NEVER RETURN"},
            ],
            "get_group_list": [
                {"group_id": "789", "group_name": "讨论组", "member_count": 7}
            ],
        },
        conversations=[
            {"type": "private", "id": "456"},
            {"type": "group", "id": "789"},
        ],
    )
    inject_qq(monkeypatch, adapter)
    first = browse(binding(adapter), limit=1)
    assert first["items"] == [
        {
            "id": "group:789",
            "kind": "group",
            "name": "讨论组",
            "member_count": 7,
            "message_count": None,
            "first_at": None,
            "last_at": None,
        }
    ]
    second = browse(binding(adapter), cursor=first["next_cursor"], limit=1)
    assert second["items"][0]["name"] == "项目同事" and second["next_cursor"] is None
    assert "NEVER RETURN" not in json.dumps(first | second)
    assert set(calls) == {"/get_login_info", "/get_friend_list", "/get_group_list"}
    assert not first["content_persisted"]
    assert browse(binding(adapter), query="项目")["items"][0]["id"] == "private:456"
    assert browse(binding(adapter), kind="group")["items"][0]["id"] == "group:789"


def test_qq_account_failure_uses_configured_ids_without_reading_contacts(monkeypatch):
    adapter, calls = qq(monkeypatch, {"get_login_info": {"user_id": "other"}})
    inject_qq(monkeypatch, adapter)
    result = browse(binding(adapter))
    assert result["items"][0]["name"] == "456"
    assert result["metadata_available"] is False
    assert result["warnings"] == ["account_binding_mismatch"]
    assert calls == ["/get_login_info"]


def test_wechat_browse_only_aggregates_metadata_without_reading_message_columns(
    tmp_path, monkeypatch
):
    adapter, writers, _ = create_wechat(
        tmp_path, [[row(1, content="PRIVATE ORIGINAL"), row(2)]]
    )
    original = adapter.access.sqlite
    reads = []

    @contextmanager
    def guarded(path):
        with original(path) as connection:

            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ:
                    reads.append(column)
                    if column in {
                        "message_content",
                        "packed_info_data",
                        "compress_content",
                    }:
                        raise AssertionError("metadata browser opened original content")
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            yield connection

    monkeypatch.setattr(adapter.access, "sqlite", guarded)
    monkeypatch.setattr(
        "memu.acquisition.sources.browsing.WeChatAdapter", lambda config: adapter
    )
    result = browse(binding(adapter))
    assert result["metadata_available"]
    assert result["items"][0]["message_count"] == 2
    assert (
        result["items"][0]["first_at"]
        == datetime.fromtimestamp(1700000000, timezone.utc).isoformat()
    )
    assert "PRIVATE ORIGINAL" not in json.dumps(result)
    assert "create_time" in reads and "message_content" not in reads
    for writer in writers:
        writer.close()


def test_wechat_catalog_discovers_chats_but_reader_requires_explicit_selection(tmp_path):
    from memu.acquisition.adapters.wechat import WeChatAdapter
    from memu.acquisition.sources.wechat import source

    adapter, writers, table = create_wechat(tmp_path, [[row(1, content='selected body')]])
    db = writers[0]
    peer = 'wxid-other'
    other_table = 'Msg_' + hashlib.md5(peer.encode()).hexdigest()
    db.execute(f'CREATE TABLE "{other_table}" AS SELECT * FROM "{table}"')
    db.execute(f'UPDATE "{other_table}" SET message_content=?', ('unselected body',))
    db.executemany('INSERT INTO Name2Id(user_name) VALUES (?)', [('group@chatroom',), (peer,)])
    db.commit()
    with sqlite3.connect(tmp_path / 'contact.db') as contacts:
        contacts.execute('CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT, description TEXT)')
        contacts.execute('INSERT INTO contact VALUES (?,?,?,?)',
                         (peer, '项目同事', '昵称', 'not part of the directory'))
    manifest = json.loads((tmp_path / 'snapshots.json').read_text())
    manifest['databases'] = [{'source_path': 'contact/contact.db', 'path': 'contact.db'}]
    (tmp_path / 'snapshots.json').write_text(json.dumps(manifest))
    config = binding(adapter)
    page = browse(config)
    assert {r['id'] for r in page['items']} == {'conversations:group@chatroom', 'conversations:' + peer}
    assert browse(config, query='项目')['items'][0]['id'] == 'conversations:' + peer
    assert 'unselected body' not in json.dumps(page)
    chosen = source.narrow_config(config['config'], Selection(scopes=['conversations:group@chatroom']))
    assert chosen['conversations'] == ['group@chatroom']
    assert [r.text for r in WeChatAdapter(chosen).read('backfill').records] == ['selected body']
    assert source.narrow_config(config['config'], Selection(scopes=['conversations:' + peer]))[
        'conversations'] == [peer]  # The old connection allowlist no longer hides this chat.
    with pytest.raises(AdapterError, match='import_scope_denied'):
        source.narrow_config(config['config'], Selection(scopes=['conversations:unknown']))
    unselected = {key: value for key, value in config['config'].items() if key != 'conversations'}
    assert len(browse({**config, 'config': unselected})['items']) == 2
    with pytest.raises(AdapterError, match='conversation_scope_required'):
        WeChatAdapter(unselected).read('backfill')
    db.close()


def test_qq_preview_selected_dates_text_only_no_durable_content(monkeypatch, tmp_path):
    current = 1700000000
    adapter, calls = qq(
        monkeypatch,
        {
            "get_friend_msg_history": {
                "messages": [
                    message("new", text="new outside range", time=current + 100),
                    message("selected", text="chosen source text", time=current),
                    message("old", text="old outside range", time=current - 100),
                ]
            }
        },
    )
    inject_qq(monkeypatch, adapter)
    config = binding(adapter) | {"data_dir": str(tmp_path / "must-not-exist")}
    selection = Selection(
        scopes=["private:456"],
        start=datetime.fromtimestamp(current, timezone.utc),
        end=datetime.fromtimestamp(current + 1, timezone.utc),
    )
    result = preview(config, selection)
    assert len(result["items"]) == 1
    assert result["items"][0]["text"] == "chosen source text"
    assert result["items"][0]["actor"]["display_name"] == "me"
    assert "raw" not in result["items"][0]
    assert result["content_persisted"] is False
    assert not Path(config["data_dir"]).exists()
    assert calls == ["/get_login_info", "/get_friend_msg_history"]


def test_preview_cursor_bound_to_selected_chat_range_and_binding(monkeypatch):
    adapter, _ = qq(
        monkeypatch, {"get_friend_msg_history": {"messages": [message("one")]}}
    )
    inject_qq(monkeypatch, adapter)
    config = binding(adapter)
    selection = Selection(scopes=["private:456"])
    result = preview(config, selection, limit=1)
    cursor = result["next_cursor"]
    assert cursor
    with pytest.raises(AdapterError, match="import_preview_cursor_stale"):
        preview(
            config,
            selection.model_copy(update={"start": datetime.now(timezone.utc)}),
            cursor=cursor,
        )
    with pytest.raises(AdapterError, match="import_preview_cursor_stale"):
        preview(config | {"source_instance_id": "other"}, selection, cursor=cursor)
    with pytest.raises(AdapterError, match="import_preview_cursor_stale"):
        preview(config, selection, cursor=cursor + "tampered")
    with pytest.raises(AdapterError, match="import_scope_denied"):
        preview(config, Selection(scopes=["private:unapproved"]), cursor=cursor)
    with pytest.raises(AdapterError, match="import_preview_invalid"):
        preview(config, Selection(scopes=["private:456", "group:789"]))


def test_wechat_preview_paginates_actual_snapshot_and_keeps_body_out_of_cursor(
    tmp_path,
):
    adapter, writers, _ = create_wechat(
        tmp_path, [[row(1, content="first"), row(2, content="second")]]
    )
    config = binding(adapter)
    selection = Selection(scopes=["conversations:group@chatroom"])
    first = preview(config, selection, limit=1)
    second = preview(config, selection, cursor=first["next_cursor"], limit=1)
    assert first["items"][0]["text"] == "first"
    assert second["items"][0]["text"] == "second"
    assert second["next_cursor"] is None
    assert first["items"][0]["actor"]["source_actor_id"] == "sender-0"
    assert set(path.name for path in tmp_path.iterdir()) <= {
        "message_0.db",
        "message_0.db-wal",
        "message_0.db-shm",
        "snapshots.json",
    }
    for writer in writers:
        writer.close()


def test_preview_rejects_unsupported_sources_and_invalid_budgets():
    selection = Selection(scopes=["chosen"])
    with pytest.raises(AdapterError, match="import_preview_unsupported"):
        preview({"adapter_id": "browser.ai-history"}, selection)
    with pytest.raises(AdapterError, match="import_preview_invalid"):
        preview({"adapter_id": "qq.napcat"}, selection, limit=31)
    with pytest.raises(AdapterError, match="import_browse_invalid"):
        browse({}, limit=101)


def test_wechat_preview_rejects_large_sqlite_values_before_loading_original(tmp_path):
    adapter, writers, _ = create_wechat(tmp_path, [[row(1, content="x" * 100000)]])
    with pytest.raises(AdapterError, match="import_preview_budget_exceeded"):
        preview(binding(adapter), Selection(scopes=["conversations:group@chatroom"]))
    for writer in writers:
        writer.close()
