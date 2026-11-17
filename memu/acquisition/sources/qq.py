"""QQ conversation identity, bounded histories and evidence filtering."""

from copy import deepcopy

from ..adapters.base import AdapterError
from .chats import ChatSource


def conversation_catalog(config):
    from ..adapters.qq import QQAdapter
    from ..adapters.local.common import native_id
    reader = QQAdapter(config)
    try:
        reader.http.max_bytes = 2 * 1024 * 1024
        login, _ = reader.rpc('get_login_info')
        if not isinstance(login, dict) or native_id(login.get('user_id')) != reader.account_id:
            raise AdapterError('account_binding_mismatch')
        result = []
        for kind, action, field, names in (
            ('private', 'get_friend_list', 'user_id', ('remark', 'nickname')),
            ('group', 'get_group_list', 'group_id', ('group_name',)),
        ):
            contacts, _ = reader.rpc(action)
            if not isinstance(contacts, list) or len(result) + len(contacts) > 10000:
                raise AdapterError('import_browse_budget_exceeded')
            for item in contacts:
                peer = native_id(item.get(field)) if isinstance(item, dict) else None
                if not peer:
                    raise AdapterError('incompatible_schema')
                name = next((item[key].strip() for key in names if isinstance(item.get(key), str) and item[key].strip()), peer)
                result.append({'id': kind + ':' + peer, 'name': name[:256], 'kind': kind,
                               'message_count': None, 'first_at': None, 'last_at': None,
                               'member_count': item.get('member_count') if type(item.get('member_count')) is int else None})
        return result
    finally:
        reader.close()


class QQSource(ChatSource):
    adapter_id = "qq.napcat"
    name, icon, slug = "QQ", "Q", "qq"
    description = "选择群聊或私聊，仅导入指定时间段的消息。"
    profiles = ("napcat-webui", "onebot11-napcat")
    subscribes = True

    def validate_profile_config(self, profile, config):
        if (profile == 'napcat-webui') != bool(config.get('webui_token_ref')):
            raise AdapterError('config_invalid')
        if profile == 'napcat-webui' and config.get('ws_url'):
            raise AdapterError('config_invalid')

    def configured_scopes(self, config):
        if config.get('browse_accessible'):
            return conversation_catalog(config)
        return [
            {
                "id": c["type"] + ":" + c["id"],
                "name": ("群聊 · " if c["type"] == "group" else "私聊 · ") + c["id"],
                "kind": c["type"],
            }
            for c in config.get("conversations", [])
        ]

    def validate(self, binding, selection):
        self.narrow_config(binding['config'], selection)

    def narrow_config(self, config, selection):
        self.validate_time(selection)
        chosen = set(selection.scopes)
        if not chosen or not chosen <= {row["id"] for row in self.configured_scopes(config)}:
            raise AdapterError("import_scope_denied")
        result = deepcopy(config)
        result.pop('browse_accessible', None)
        result["conversations"] = [dict(zip(('type', 'id'), value.split(':', 1))) for value in selection.scopes]
        return result

    def prepare_record(self, record):
        # The original page can contain messages outside the chosen interval.
        record.attachments = [
            a for a in record.attachments if a.attachment_id != "source-page"
        ]

    def reader_class(self, profile):
        from ..adapters.qq import QQAdapter

        return QQAdapter


source = QQSource()
