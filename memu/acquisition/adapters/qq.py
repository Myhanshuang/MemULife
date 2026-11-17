"""Independent, read-only OneBot/NapCat client; no upstream reader is bundled."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .base import AdapterError, Attachment, Page, Snapshot
from .http import SourceHTTP
from .local.common import encode, native_id


READ_ACTIONS = frozenset(
    {
        "get_login_info",
        "get_version_info",
        "get_status",
        "get_friend_list",
        "get_group_list",
        "get_friend_msg_history",
        "get_group_msg_history",
        "get_msg",
        "get_image",
        "get_record",
        "get_file",
    }
)


def safe_endpoint(value: str, *, websocket: bool = False) -> str:
    parsed = urlparse(value)
    secure, plain = ("wss", "ws") if websocket else ("https", "http")
    loopback = parsed.hostname == "localhost"
    try:
        loopback |= ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if (
        (parsed.scheme != secure and not (parsed.scheme == plain and loopback))
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AdapterError("unsafe_endpoint")
    return value.rstrip("/")


class QQAdapter:
    adapter_id = "qq.napcat"
    config_schema = {
        "type": "object",
        "required": ["base_url", "account_id"],
        "oneOf": [{"required": ["token_env"]}, {"required": ["webui_token_ref"]}],
        "anyOf": [{"required": ["conversations"]}, {"required": ["browse_accessible"], "properties": {"browse_accessible": {"const": True}}}],
        "additionalProperties": False,
        "properties": {
            "base_url": {"type": "string", "minLength": 1},
            "ws_url": {"type": "string", "minLength": 1},
            "account_id": {"type": "string", "minLength": 1},
            "token_env": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
            "webui_token_ref": {"type": "string", "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$"},
            "browse_accessible": {"type": "boolean"},
            "conversations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["type", "id"],
                    "additionalProperties": False,
                    "properties": {
                        "type": {"enum": ["private", "group"]},
                        "id": {"type": "string", "minLength": 1},
                    },
                },
            },
            "history_order": {"enum": ["newest_first", "oldest_first"]},
            "media_hosts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
    }

    def __init__(self, config: dict, client: httpx.Client | None = None):
        self.config = config
        self.base_url = safe_endpoint(config.get("base_url", ""))
        if config.get('webui_token_ref'):
            from .napcat_webui import endpoint
            self.base_url = endpoint(self.base_url)
        self.account_id = native_id(config.get("account_id"))
        if not self.account_id:
            raise AdapterError("account_binding_required")
        self.conversations = config.get("conversations", [])
        if not isinstance(self.conversations, list) or (not self.conversations and not config.get('browse_accessible')):
            raise AdapterError("conversation_scope_required")
        for item in self.conversations:
            if (
                not isinstance(item, dict)
                or item.get("type") not in {"private", "group"}
                or not native_id(item.get("id"))
            ):
                raise AdapterError("invalid_conversation_scope")
        self.http = SourceHTTP(self.base_url, client=client)
        self.client = self.http.client
        self.webui = None
        if config.get('webui_token_ref'):
            from .napcat_webui import NapCatWebUI
            self.webui = NapCatWebUI(config, self.http)
        self._status = "ready"

    def close(self):
        self.http.close()

    def _headers(self):
        name = self.config.get("token_env")
        if not isinstance(name, str) or not os.environ.get(name):
            raise AdapterError("auth_required")
        return {"Authorization": "Bearer " + os.environ[name]}

    def rpc(self, action: str, parameters: dict | None = None):
        if action not in READ_ACTIONS:
            raise AdapterError("action_not_allowed")
        if self.webui:
            return self.webui.call(action, parameters)
        value, raw = self.http.json(
            "POST", f"/{action}", body=parameters or {}, headers=self._headers()
        )
        if value.get("status") != "ok" or value.get("retcode") != 0:
            raise AdapterError("upstream_read_failed", retryable=True)
        return value.get("data"), raw

    def probe(self):
        login, _ = self.rpc("get_login_info")
        version, _ = self.rpc("get_version_info")
        status, _ = self.rpc("get_status")
        if (
            not isinstance(login, dict)
            or native_id(login.get("user_id")) != self.account_id
        ):
            raise AdapterError("account_binding_mismatch")
        if not isinstance(status, dict) or status.get("online") is not True:
            raise AdapterError("source_offline", retryable=True)
        return {
            "adapter_id": self.adapter_id,
            "status": "ready",
            "account_id": self.account_id,
            "backend_version": version,
            "device_verified": False,
            "capabilities": {
                "discover": True,
                "backfill": True,
                "poll": True,
                "reconcile": True,
                "subscribe": bool(self.config.get("ws_url")),
                "read_only": True,
                "send_messages": False,
                "history": "bounded",
                "outgoing_messages": "conditional",
            },
            "conditions": [
                "reportSelfMessage=true required for outgoing subscription",
                "history order must match selected profile",
                "source retrievability is not guaranteed",
                "external NapCat backend license and device validation required",
            ],
        }

    def health(self):
        return {
            "status": self.http.last_error or self._status,
            "send_messages": False,
            "foreground_actions": False,
            "retry_after_seconds": self.http.retry_after_seconds,
            "last_success_at": self.http.last_success_at,
        }

    def _scope(self, kind, peer):
        if not any(
            item["type"] == kind and native_id(item["id"]) == peer
            for item in self.conversations
        ):
            raise AdapterError("conversation_scope_denied")

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if mode not in {"discover", "live", "poll", "backfill", "reconcile"}:
            raise AdapterError("capability_unavailable")
        if not 1 <= limit <= 100:
            raise AdapterError("invalid_limit")
        state = deepcopy(cursor or {})
        if state.get("account_id") not in {None, self.account_id}:
            raise AdapterError("account_binding_mismatch")
        state["account_id"] = self.account_id
        if mode == "discover":
            return self._discover(state, limit)
        if not self.conversations:
            raise AdapterError('conversation_scope_required')
        if self.config.get("history_order") not in {"newest_first", "oldest_first"}:
            raise AdapterError("history_profile_required")
        index = int(state.get("conversation_index", 0))
        if not 0 <= index < len(self.conversations):
            index = 0
        scope = self.conversations[index]
        kind, peer = scope["type"], native_id(scope["id"])
        scoped = state.setdefault("history", {}).setdefault(f"{kind}:{peer}", {})
        parameters = {
            "group_id" if kind == "group" else "user_id": peer,
            "count": min(limit, 100),
        }
        if mode == "backfill" and scoped.get("anchor"):
            parameters["message_seq"] = scoped["anchor"]
        action = (
            "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
        )
        data, raw_page = self.rpc(action, parameters)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise AdapterError("incompatible_schema")
        messages = data["messages"]
        if len(messages) > limit:
            raise AdapterError("upstream_page_too_large")
        digest = hashlib.sha256(raw_page).hexdigest()
        if mode == "backfill" and messages and scoped.get("digest") == digest:
            raise AdapterError("pagination_loop")
        selection = getattr(self, "import_selection", None)
        selected_messages = messages
        if selection:
            from ..sources.selection import in_time

            def message_time(message):
                value = message.get("time")
                if type(value) is not int:
                    return None
                try:
                    return datetime.fromtimestamp(value, timezone.utc).isoformat()
                except (ValueError, OSError, OverflowError):
                    return None

            selected_messages = [
                m for m in messages if in_time(message_time(m), selection)
            ]
        records = [
            self._message(message, kind, peer, state) for message in selected_messages
        ]
        if selection and selection.include_attachments:
            from .qq_media import resolve
            resolve(self, records)
        for record in records if not selection else []:
            record.attachments.append(
                Attachment(
                    "source-page", raw_page, "application/json", status="available"
                )
            )
        # IDs are opaque anchors; never compare/max short OneBot IDs.
        if messages:
            oldest = (
                messages[-1]
                if self.config["history_order"] == "newest_first"
                else messages[0]
            )
            raw = oldest.get("raw", {})
            anchor = native_id(raw.get("msgId")) or native_id(oldest.get("message_id"))
            if not anchor:
                raise AdapterError("history_anchor_unavailable")
            if mode == "backfill" and anchor == scoped.get("anchor"):
                raise AdapterError("pagination_loop")
            scoped.update(anchor=anchor, digest=digest)
        bounded_end = len(messages) < limit
        if selection and selection.start and messages:
            times = [message_time(m) for m in messages]
            if (
                all(times)
                and min(datetime.fromisoformat(t) for t in times) < selection.start
            ):
                # History traversal moves from newest toward older opaque anchors.
                bounded_end = True
        if mode != "backfill" or bounded_end:
            state["conversation_index"] = (index + 1) % len(self.conversations)
            if mode != "backfill":
                scoped.pop("anchor", None)
        more = (mode == "backfill" and not bounded_end) or index + 1 < len(
            self.conversations
        )
        return Page(records, state, more, False, ["source_retrievability_unknown"])

    def _discover(self, state, limit):
        index = int(state.get("discover_index", 0))
        records = []
        # API returns full contact lists; scope filtering happens before persistence.
        friends, _ = self.rpc("get_friend_list")
        groups, _ = self.rpc("get_group_list")
        if not isinstance(friends, list) or not isinstance(groups, list):
            raise AdapterError("incompatible_schema")
        allowed = {(item["type"], native_id(item["id"])) for item in self.conversations}
        candidates = [("private", item) for item in friends] + [
            ("group", item) for item in groups
        ]
        selected = [
            (kind, item)
            for kind, item in candidates
            if (kind, native_id(item.get("group_id" if kind == "group" else "user_id")))
            in allowed
        ]
        for kind, item in selected[index : index + limit]:
            peer = native_id(item.get("group_id" if kind == "group" else "user_id"))
            records.append(
                Snapshot(
                    f"conversation:{kind}:{peer}",
                    encode(item),
                    metadata={"conversation_type": kind, "peer_id": peer},
                )
            )
        state["discover_index"] = index + len(records)
        return Page(
            records,
            state,
            state["discover_index"] < len(selected),
            False,
            ["contact_list_is_not_complete_history_discovery"],
        )

    def _message(self, message, kind, peer, state):
        if not isinstance(message, dict):
            raise AdapterError("incompatible_schema")
        if "self_id" in message and native_id(message["self_id"]) != self.account_id:
            raise AdapterError("account_binding_mismatch")
        raw = message.get("raw") or {}
        if not isinstance(raw, dict):
            raise AdapterError("incompatible_schema")
        sender = message.get("sender") or {}
        actor_id = native_id(sender.get("user_id")) or native_id(message.get("user_id"))
        long_id = native_id(raw.get("msgId"))
        real_seq = native_id(message.get("real_seq"))
        short = native_id(message.get("message_id"))
        if long_id:
            identity, strength = long_id, "native"
        elif real_seq:
            identity = f"seq:{real_seq}:{message.get('time')}:{actor_id}"
            strength = "composite"
        else:
            # Weak identity includes transport identity/time/actor. No text dedup.
            identity = f"weak:{short}:{message.get('time')}:{actor_id}:{hashlib.sha256(encode(message)).hexdigest()}"
            strength = "provisional"
        object_id = f"{self.account_id}:{kind}:{peer}:{identity}"
        aliases = state.setdefault("aliases", {})
        alias_key = f"{kind}:{peer}:{short}"
        candidates = aliases.setdefault(alias_key, []) if short else []
        if short and object_id not in candidates:
            candidates.append(object_id)
        if len(aliases) > 10000:
            # Losing an alias weakens recall resolution; it never removes a message.
            aliases.pop(next(iter(aliases)))
        parts = message.get("message", [])
        if not isinstance(parts, list):
            raise AdapterError("message_array_profile_required")
        body = "".join(
            str(part.get("data", {}).get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        attachments = []
        for index, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") in {
                "image",
                "record",
                "video",
                "file",
            }:
                data = part.get("data", {})
                attachments.append(
                    Attachment(
                        f"part:{index}",
                        source_uri=data.get("url"),
                        metadata={"part": index, "kind": part["type"], "native": data},
                    )
                )
        occurred = None
        if type(message.get("time")) is int:
            try:
                occurred = datetime.fromtimestamp(
                    message["time"], timezone.utc
                ).isoformat()
            except (ValueError, OSError, OverflowError):
                pass
        return Snapshot(
            object_id,
            encode(message),
            body,
            occurred_at=occurred,
            attachments=attachments,
            metadata={
                "conversation_id": f"{kind}:{peer}",
                "identity_strength": strength,
                "short_id": short,
                "native_message_id": long_id,
                "real_seq": real_seq,
                "actor": {
                    "source_actor_id": actor_id,
                    "role": "self"
                    if actor_id == self.account_id
                    else "other"
                    if actor_id
                    else "unknown",
                    "display_name": sender.get("nickname"),
                },
            },
        )

    def event(self, event: dict, cursor: dict | None = None) -> Page:
        """Normalize one subscription event; caller commits Page before reading on."""
        state = deepcopy(cursor or {})
        if native_id(event.get("self_id")) != self.account_id:
            raise AdapterError("account_binding_mismatch")
        kind = "group" if event.get("group_id") else "private"
        actor = native_id(event.get("user_id"))
        raw = event.get("raw") or {}
        peer = (
            native_id(event.get("group_id"))
            if kind == "group"
            else (
                native_id(event.get("target_id"))
                or native_id(raw.get("peerUin"))
                or (actor if actor != self.account_id else None)
            )
        )
        if not peer:
            raise AdapterError("conversation_identity_unresolved")
        self._scope(kind, peer)
        if event.get("post_type") in {"message", "message_sent"}:
            return Page([self._message(event, kind, peer, state)], state, False, False)
        if event.get("notice_type") in {"group_recall", "friend_recall"}:
            long_id = native_id(raw.get("msgId"))
            candidates = (
                [f"{self.account_id}:{kind}:{peer}:{long_id}"]
                if long_id
                else state.get("aliases", {}).get(
                    f"{kind}:{peer}:{native_id(event.get('message_id'))}", []
                )
            )
            resolved = len(candidates) == 1
            identity = (
                candidates[0]
                if resolved
                else "unresolved-recall:" + hashlib.sha256(encode(event)).hexdigest()
            )
            return Page(
                [
                    Snapshot(
                        identity,
                        encode(event),
                        deleted=resolved,
                        metadata={
                            "reason": "revoked",
                            "unresolved_recall": not resolved,
                        },
                    )
                ],
                state,
                False,
                False,
                [] if resolved else ["recall_identity_unresolved"],
            )
        return Page(
            [
                Snapshot(
                    "event:" + hashlib.sha256(encode(event)).hexdigest(),
                    encode(event),
                    metadata={"native_event_type": event.get("post_type")},
                )
            ],
            state,
            False,
            False,
        )

    def subscribe(self, cursor=None):
        """Yield candidate pages; consumer must durable-commit before next iteration.

        OneBot has no persistent replay offset. Every new connection reports a gap
        requiring overlap reconciliation; this generator never closes it itself.
        """
        from websockets.sync.client import connect
        from websockets.exceptions import WebSocketException

        if not self.config.get("ws_url"):
            raise AdapterError("capability_unavailable")
        endpoint = safe_endpoint(self.config["ws_url"], websocket=True)
        state = deepcopy(cursor or {})
        try:
            with connect(
                endpoint,
                additional_headers=self._headers(),
                open_timeout=15,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
                proxy=None,
            ) as websocket:
                yield Page([], state, False, False, ["subscription_reconcile_required"])
                for raw in websocket:
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise AdapterError("incompatible_schema") from None
                    if not isinstance(value, dict):
                        raise AdapterError("incompatible_schema")
                    if value.get("post_type") == "meta_event":
                        if native_id(value.get("self_id")) != self.account_id:
                            raise AdapterError("account_binding_mismatch")
                        yield Page([], state, False, False)
                        continue
                    try:
                        page = self.event(value, state)
                    except AdapterError as error:
                        if error.code == "conversation_scope_denied":
                            continue
                        raise
                    yield page
                    state = page.cursor
        except (OSError, WebSocketException):
            self._status = "source_offline"
            raise AdapterError("subscription_disconnected", retryable=True) from None

    def fetch_attachment(self, ref: dict) -> Attachment:
        from .qq_media import fetch
        return fetch(self, ref)
