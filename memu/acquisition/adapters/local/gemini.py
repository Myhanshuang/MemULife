"""Replay Gemini CLI append-only session changes without discarding old records."""

from __future__ import annotations

from copy import deepcopy

from ..base import AdapterError


def fold(state: dict, record: dict) -> tuple[dict, list[str]]:
    state = deepcopy(state)
    messages = state.setdefault("messages", {})
    metadata = state.setdefault("metadata", {})
    warnings = []
    if "$set" in record:
        update = record["$set"]
        if not isinstance(update, dict):
            raise AdapterError("incompatible_schema")
        for key, value in update.items():
            if key != "messages":
                metadata[key] = value
        if "messages" in update:
            if not isinstance(update["messages"], list):
                raise AdapterError("incompatible_schema")
            messages.clear()
            for message in update["messages"]:
                if not isinstance(message, dict) or not isinstance(
                    message.get("id"), str
                ):
                    raise AdapterError("incompatible_schema")
                messages[message["id"]] = message
    elif "$rewindTo" in record:
        ids = list(messages)
        target = record["$rewindTo"]
        keep = ids[: ids.index(target)] if target in ids else []
        state["messages"] = {key: messages[key] for key in keep}
    elif "$patch" in record:
        patch = record["$patch"]
        if not isinstance(patch, dict):
            raise AdapterError("incompatible_schema")
        updates = patch.get("updates", [])
        if not isinstance(updates, list):
            raise AdapterError("incompatible_schema")
        if "id" in patch:
            updates = [patch, *updates]
        for update in updates:
            if not isinstance(update, dict):
                raise AdapterError("incompatible_schema")
            message = messages.get(update.get("id"))
            if message is None:
                warnings.append("patch_target_unknown")
                continue
            if "content" in update:
                message["content"] = update["content"]
            for tool_patch in update.get("toolCalls", []):
                tool = next(
                    (
                        tool
                        for tool in message.get("toolCalls", [])
                        if tool.get("id") == tool_patch.get("id")
                    ),
                    None,
                )
                if tool is None:
                    warnings.append("tool_patch_target_unknown")
                elif "result" in tool_patch:
                    tool["result"] = tool_patch["result"]
        for key in patch.get("removeIds", []):
            messages.pop(key, None)
        order = patch.get("orderIds", [])
        for key in dict.fromkeys(order):
            if key in messages:
                messages[key] = messages.pop(key)
    elif "id" in record:
        if not isinstance(record["id"], str):
            raise AdapterError("incompatible_schema")
        messages[record["id"]] = record
    else:
        metadata.update(
            {key: value for key, value in record.items() if key != "messages"}
        )
        for message in record.get("messages", []):
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise AdapterError("incompatible_schema")
            messages[message["id"]] = message
    return state, warnings
