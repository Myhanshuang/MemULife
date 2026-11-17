from pathlib import Path

import pytest

from memu.storage import InvalidReference, Repository, RevisionConflict


DAY = {"precision": "day", "value": "2026-11-10"}


def unit(body, facts, **extra):
    return {"time": DAY, "body": body, "facts": facts, "topics": ["memory"], **extra}


def test_stable_lines_sources_and_event_time(tmp_path):
    repo = Repository(tmp_path)
    source = repo.save_source("first\nsecond\nthird\n", "agent1", {"precision": "day", "value": "2026-11-13"})
    first, second = repo.append_entries("unit", [unit("A | B\\C\ncontinued", ["c1:1-1", "c1:3-3"]), unit("second event", [source])], "agent1")
    before = repo.read(second)
    assert repo.read(first)["body"] == "A | B\\C\ncontinued"
    assert before["path"] == "2026/11/10/units.md"
    assert repo.read(source)["path"].startswith("2026/11/13/")
    repo.blank(first)
    assert repo.read(second)["start_line"] == before["start_line"]
    third = repo.append_entries("unit", [unit("third event", [source])], "agent2")[0]
    assert repo.read(third)["source"] == "agent2"
    assert repo.read(third)["start_line"] > before["start_line"]
    with pytest.raises(InvalidReference):
        repo.read(first)
    repo.blank("c1:2-2")
    assert [block["ref"] for block in repo.iter_blocks(["source"])] == ["c1:1-1", "c1:3-3"]
    with pytest.raises(InvalidReference):
        repo.read(source)


def test_media_and_view_entry_addresses_survive_reopen(tmp_path):
    repo = Repository(tmp_path)
    media = repo.save_media(b"original video bytes", "agent1", DAY, "video/mp4", duration=19.0)
    assert repo.read(media + "@12.4-18.0")["start_seconds"] == 12.4
    with pytest.raises(InvalidReference):
        repo.read(media + "@12.4-20.0")
    event = repo.append_entries("unit", [unit("observed event", [media])], "agent1")[0]
    runtime = repo.write_view("runtime", "memory", [unit("current state", [event])], {"aliases": ["记忆系统"]})
    memory = repo.write_view("memory", None, [unit("overview", [runtime])], {"directory": [{"ref": runtime, "title": "Memory"}]})
    with pytest.raises(InvalidReference):
        repo.read(runtime + ":2")
    reopened = Repository(tmp_path)
    assert reopened.read(runtime)["aliases"] == ["记忆系统"]
    assert reopened.read("MEMORY.md")["ref"] == memory
    assert Path(reopened.read(media)["absolute_path"]).read_bytes() == b"original video bytes"


def test_transaction_stages_checkpoint_and_rolls_back(tmp_path):
    repo = Repository(tmp_path)
    with repo.transaction():
        source = repo.save_source("source", "agent1", DAY)
        event = repo.append_entries("unit", [unit("event", [source])], "agent1")[0]
        repo.state["writes"] = {"request-1": {"status": "done", "refs": [event]}}
        repo.save_state()
        assert repo.read(event)["body"] == "event"
        assert not (tmp_path / repo.read(event)["path"]).exists()
    assert Repository(tmp_path).state["writes"]["request-1"]["refs"] == [event]
    before = repo.iter_blocks()
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.append_entries("unit", [unit("must not survive", [source])], "agent1")
            repo.state["writes"]["request-2"] = {"status": "done"}
            raise RuntimeError("abort before publication")
    assert repo.iter_blocks() == before
    assert "request-2" not in Repository(tmp_path).state["writes"]


def test_journal_recovers_committed_unit_and_idempotency_together(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    source = repo.save_source("source", "agent1", DAY)
    original = repo._replace_file

    def interrupted(path, data):
        if path.endswith("units.md"):
            raise OSError("simulated interruption after durable journal")
        original(path, data)

    monkeypatch.setattr(repo, "_replace_file", interrupted)
    with pytest.raises(OSError):
        with repo.transaction():
            event = repo.append_entries("unit", [unit("only one event", [source])], "agent1")[0]
            repo.state["writes"] = {"same-key": {"refs": [event], "status": "done"}}
            repo.save_state()
    recovered = Repository(tmp_path)
    assert [item["ref"] for item in recovered.iter_blocks(["unit"])] == [event]
    assert recovered.state["writes"]["same-key"]["status"] == "done"
    assert not (tmp_path / ".meta/transaction.json").exists()


def test_conflicts_invalid_refs_and_partial_time(tmp_path):
    repo = Repository(tmp_path)
    source = repo.save_source("known fact", "agent1", DAY)
    revision = repo.revision("c1")
    repo.append_source(source, "new fact")
    with pytest.raises(RevisionConflict):
        repo.commit({}, expected_revisions={"c1": revision})
    with pytest.raises(InvalidReference):
        repo.append_entries("unit", [unit("invented", ["c1:1-99"])], "agent1")
    refs = repo.append_entries("unit", [
        unit("month only", [source], time={"precision": "month", "value": "2026-11"}),
        unit("year only", [source], time={"precision": "year", "value": "2026"}),
        unit("unknown", [source], time={"precision": "unknown", "value": None}),
        unit("cross day", [source], time={"precision": "range", "value": {"from": "2026-11-10", "to": "2026-11-12"}}),
    ], "agent1")
    assert [repo.read(ref)["path"] for ref in refs] == [
        "2026/11/_undated/units.md", "2026/_undated/units.md", "_undated/units.md", "2026/11/units.md",
    ]
