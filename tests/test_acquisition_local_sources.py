import gzip
import json
import sqlite3

import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.local import (
    ClaudeCodeAdapter,
    CodexAdapter,
    CursorAdapter,
    GeminiCLIAdapter,
    OpenCodeAdapter,
)
from memu.acquisition.adapters.local.gemini import fold


def lines(*values):
    return b"".join(
        json.dumps(value, ensure_ascii=False).encode() + b"\n" for value in values
    )


def write(root, path, data):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_codex_complete_utf8_records_resume_and_native_identity(tmp_path):
    meta = {"type": "session_meta", "payload": {"id": "session-1"}}
    message = {
        "type": "response_item",
        "payload": {
            "id": "message-1",
            "role": "user",
            "content": [{"type": "input_text", "text": "中文😀"}],
        },
    }
    first, second = lines(meta), lines(message)
    for offset in range(1, len(second)):
        path = write(tmp_path, "sessions/a/rollout.jsonl", first + second[:offset])
        adapter = CodexAdapter({"root": str(tmp_path)})
        page = adapter.read("live")
        assert len(page.records) == 1
        assert page.cursor["files"]["sessions/a/rollout.jsonl"]["offset"] == len(first)
        assert "incomplete_tail" in page.warnings
        path.write_bytes(first + second)
        resumed = adapter.read("live", page.cursor)
        assert [record.text for record in resumed.records] == ["中文😀"]
        assert resumed.records[0].object_id.endswith("/message-1")
        assert not adapter.read("live", resumed.cursor).records


def test_codex_archive_copy_and_mirror_keeps_raw_without_double_text(tmp_path):
    content = lines(
        {
            "type": "session_meta",
            "payload": {"meta": {"id": "s", "forked_from_id": "parent"}},
        },
        {
            "type": "response_item",
            "ordinal": 100,
            "payload": {"id": "a", "role": "assistant", "content": "答复"},
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "答复"}},
    )
    path = write(tmp_path, "sessions/a.jsonl", content)
    adapter = CodexAdapter({"root": str(tmp_path)})
    first = adapter.read("live")
    path.unlink()
    write(tmp_path, "archived_sessions/a.jsonl.gz", gzip.compress(content))
    archived = adapter.read("live", first.cursor)
    assert [(r.object_id, r.revision) for r in archived.records] == [
        (r.object_id, r.revision) for r in first.records
    ]
    assert archived.records[-1].text is None
    assert json.loads(archived.records[-1].raw)["payload"]["message"] == "答复"
    assert archived.records[1].metadata["forked_from_id"] == "parent"


def test_claude_same_uuid_aba_and_distinct_identical_messages(tmp_path):
    values = [
        {
            "sessionId": "s",
            "uuid": "a",
            "type": "assistant",
            "message": {"content": value},
        }
        for value in ("A", "B", "A")
    ]
    values += [
        {"sessionId": "s", "uuid": x, "type": "user", "message": {"content": "same"}}
        for x in ("x", "y")
    ]
    write(tmp_path, "projects/p/subagents/agent.jsonl", lines(*values))
    adapter = ClaudeCodeAdapter({"root": str(tmp_path)})
    page = adapter.read("poll", limit=2)
    assert page.has_more
    next_page = adapter.read("poll", page.cursor)
    records = page.records + next_page.records
    assert [r.text for r in records] == ["A", "B", "A", "same", "same"]
    assert len({r.revision for r in records[:3]}) == 3
    assert records[-1].object_id != records[-2].object_id


def test_log_replacement_new_generation_and_symlink_boundary(tmp_path):
    path = write(
        tmp_path,
        "projects/p/a.jsonl",
        lines({"sessionId": "s", "uuid": "a", "message": "first"}),
    )
    adapter = ClaudeCodeAdapter({"root": str(tmp_path)})
    page = adapter.read("poll")
    replacement = path.with_suffix(".tmp")
    replacement.write_bytes(lines({"sessionId": "s", "uuid": "a", "message": "newer"}))
    replacement.replace(path)
    changed = adapter.read("poll", page.cursor)
    assert changed.records[0].text == "newer"
    assert changed.records[0].metadata["generation"] == 1
    assert "file_generation_changed" in changed.warnings
    path.unlink()
    path.symlink_to(tmp_path / "not-authorized.jsonl")
    (tmp_path / "not-authorized.jsonl").write_bytes(lines({"sessionId": "wrong"}))
    with pytest.raises(AdapterError, match="path_outside_scope"):
        adapter.read("poll", changed.cursor)


def test_gemini_fold_order_updates_rewind_and_raw_history(tmp_path):
    values = [
        {"sessionId": "g", "projectHash": "p"},
        {"id": "a", "type": "user", "content": "A"},
        {
            "id": "b",
            "type": "gemini",
            "content": "B",
            "toolCalls": [{"id": "tool", "result": None}],
        },
        {"id": "c", "content": "C"},
        {
            "$patch": {
                "updates": [
                    {
                        "id": "b",
                        "content": "B2",
                        "toolCalls": [{"id": "tool", "result": "done"}],
                    }
                ],
                "orderIds": ["a", "b"],
            }
        },
        {"$patch": {"removeIds": ["c"], "orderIds": ["b", "a"]}},
        {"$rewindTo": "a"},
        {"$set": {"messages": []}},
    ]
    write(tmp_path, "tmp/p/chats/session-g.jsonl", lines(*values))
    adapter = GeminiCLIAdapter({"root": str(tmp_path)})
    first = adapter.read("live", limit=5)
    assert first.records[-1].metadata["active_ids"] == ["c", "a", "b"]
    assert (
        first.cursor["files"]["tmp/p/chats/session-g.jsonl"]["fold"]["messages"]["b"][
            "toolCalls"
        ][0]["result"]
        == "done"
    )
    second = adapter.read("live", first.cursor)
    assert [record.metadata["active_ids"] for record in second.records] == [
        ["b", "a"],
        ["b"],
        [],
    ]
    assert all(not record.deleted for record in first.records + second.records)
    assert json.loads(second.records[1].raw) == {"$rewindTo": "a"}


@pytest.mark.parametrize(
    "target,expected", [("a", []), ("b", ["a"]), ("c", ["a", "b"]), ("missing", [])]
)
def test_gemini_rewind_exact_boundary(target, expected):
    state = {"messages": {key: {"id": key} for key in "abc"}, "metadata": {}}
    result, warnings = fold(state, {"$rewindTo": target})
    assert list(result["messages"]) == expected
    assert list(state["messages"]) == list("abc")
    assert not warnings


def test_gemini_unknown_patch_no_fabrication_and_idempotent_patch():
    state, warnings = fold({}, {"$patch": {"id": "missing", "content": "fiction"}})
    assert not state["messages"]
    assert warnings == ["patch_target_unknown"]
    state, _ = fold({}, {"sessionId": "s", "messages": [{"id": "a", "content": "A"}]})
    first, _ = fold(state, {"$patch": {"id": "a", "content": "B"}})
    second, _ = fold(first, {"$patch": {"id": "a", "content": "B"}})
    assert first == second


def test_gemini_legacy_snapshot_aba(tmp_path):
    path = write(tmp_path, "tmp/p/chats/session-legacy.json", b"{}")
    adapter = GeminiCLIAdapter({"root": str(tmp_path)})
    cursor = None
    for value in ("A", "B", "A"):
        path.write_text(
            json.dumps(
                {"sessionId": "legacy", "messages": [{"id": "1", "content": value}]}
            )
        )
        page = adapter.read("poll", cursor)
        assert page.records[0].text == value
        cursor = page.cursor


def test_opencode_mixed_db_pagination_wal_and_credential_table_excluded(tmp_path):
    source = sqlite3.connect(tmp_path / "opencode.db")
    source.execute("PRAGMA journal_mode=WAL")
    source.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY,title TEXT);
        CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,data TEXT,time_updated INTEGER);
        CREATE TABLE part(id TEXT PRIMARY KEY,session_id TEXT,message_id TEXT,data TEXT,time_updated INTEGER);
        CREATE TABLE session_v2(id TEXT PRIMARY KEY,title TEXT);
        CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,data TEXT);
        CREATE TABLE auth(id TEXT,secret TEXT);
        INSERT INTO auth VALUES ('credential','must-never-leave-source');
        INSERT INTO session VALUES ('s1','v1');
        INSERT INTO session_v2 VALUES ('s2','v2');
        INSERT INTO message VALUES ('m','s1','{"content":"older","role":"user"}',1);
        INSERT INTO part VALUES ('p','s1','m','{"type":"tool","state":{"status":"running"}}',1);
        INSERT INTO session_message VALUES ('v','s2','user',9007199254740993,'{"content":"newer"}');
    """)
    source.commit()
    adapter = OpenCodeAdapter({"root": str(tmp_path)})
    assert set(adapter.probe()["profiles"]) == {"sqlite-v1", "sqlite-v2"}
    cursor, records = None, []
    for _ in range(8):
        page = adapter.read("poll", cursor, limit=2)
        records.extend(page.records)
        cursor = page.cursor
        if not page.has_more:
            break
    assert len(records) == 5
    assert {r.object_id for r in records} == {
        "session:s1",
        "message:m",
        "part:p",
        "session_v2:s2",
        "session_message:v",
    }
    assert b"must-never-leave-source" not in b"".join(r.raw for r in records)
    source.execute(
        "UPDATE message SET data=? WHERE id='m'", (json.dumps({"content": "edited"}),)
    )
    source.commit()
    refreshed = adapter.read("reconcile", cursor)
    assert (
        next(r.text for r in refreshed.records if r.object_id == "message:m")
        == "edited"
    )
    source.close()


def test_opencode_unknown_schema_is_failure_not_zero_history(tmp_path):
    connection = sqlite3.connect(tmp_path / "opencode.db")
    connection.execute("CREATE TABLE message(id TEXT, secret TEXT)")
    connection.close()
    with pytest.raises(AdapterError, match="incompatible_schema"):
        OpenCodeAdapter({"root": str(tmp_path)}).probe()


def test_cursor_only_chat_keys_and_repeated_text_keeps_native_ids(tmp_path):
    path = tmp_path / "User/globalStorage/state.vscdb"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)")
    connection.executemany(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        [
            ("bubbleId:c:a", '{"text":"same"}'),
            ("bubbleId:c:b", '{"text":"same"}'),
            ("auth/token", '{"secret":"do-not-read"}'),
        ],
    )
    connection.commit()
    connection.close()
    adapter = CursorAdapter({"root": str(tmp_path)})
    assert adapter.probe()["status"] == "partial_coverage"
    first = adapter.read("poll", limit=1)
    second = adapter.read("poll", first.cursor, limit=1)
    assert first.records[0].object_id != second.records[0].object_id
    assert first.records[0].text == second.records[0].text == "same"
    assert all(b"do-not-read" not in r.raw for r in first.records + second.records)


def test_zstd_logs_bounded_complete_frame_and_partial_compressed_no_checkpoint(
    tmp_path,
):
    import zstandard

    content = lines(
        {"type": "session_meta", "payload": {"id": "s"}},
        {"type": "response_item", "payload": {"id": "m", "content": "compressed"}},
    )
    compressed = zstandard.ZstdCompressor().compress(content)
    path = write(tmp_path, "sessions/a.jsonl.zst", compressed[:-2])
    adapter = CodexAdapter({"root": str(tmp_path)})
    incomplete = adapter.read("live")
    assert not incomplete.records
    assert "compressed_file_incomplete" in incomplete.warnings
    path.write_bytes(compressed)
    complete = adapter.read("live", incomplete.cursor)
    assert complete.records[-1].text == "compressed"


def test_claude_metadata_before_session_has_native_identity_without_dropping_raw(
    tmp_path,
):
    write(
        tmp_path,
        "projects/p/a.jsonl",
        lines(
            {"type": "file-history-snapshot", "snapshot": {"file": "a"}},
            {"sessionId": "s", "uuid": "m", "type": "user", "message": "hello"},
        ),
    )
    page = ClaudeCodeAdapter({"root": str(tmp_path)}).read("live")
    assert len(page.records) == 2
    assert all(r.object_id.startswith("s/") for r in page.records)
    assert b"file-history-snapshot" in page.records[0].raw


def test_malformed_complete_record_does_not_publish_cursor(tmp_path):
    write(
        tmp_path,
        "projects/p/a.jsonl",
        lines({"sessionId": "s", "uuid": "m", "message": "hello"}) + b"{bad-json}\n",
    )
    with pytest.raises(AdapterError, match="incompatible_schema"):
        ClaudeCodeAdapter({"root": str(tmp_path)}).read("live")


def test_explicit_attachment_roots_no_symlink_or_implicit_log_path_authority(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    file = media / "a.txt"
    file.write_text("evidence")
    adapter = CodexAdapter({"root": str(root), "attachment_roots": [str(media)]})
    assert adapter.fetch_attachment({"path": str(file)}).data == b"evidence"
    with pytest.raises(AdapterError, match="path_outside_scope"):
        CodexAdapter({"root": str(root)}).fetch_attachment({"path": str(file)})
    link = media / "link.txt"
    link.symlink_to(file)
    with pytest.raises(AdapterError, match="path_outside_scope"):
        adapter.fetch_attachment({"path": str(link)})


def test_local_profile_schemas_reject_unknown_configuration():
    import jsonschema

    for adapter in (
        CodexAdapter,
        ClaudeCodeAdapter,
        GeminiCLIAdapter,
        OpenCodeAdapter,
        CursorAdapter,
    ):
        jsonschema.Draft202012Validator.check_schema(adapter.config_schema)
        jsonschema.validate({"root": "/tmp"}, adapter.config_schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {"root": "/tmp", "read_credentials": True}, adapter.config_schema
            )
