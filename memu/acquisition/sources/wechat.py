"""WeChat read-only snapshots and conversation selections."""

from .chats import ChatSource
from ..adapters.base import AdapterError
from ..adapters.wechat import WeChatAdapter
from ..adapters.wechat_catalog import conversation_catalog


class WeChatSource(ChatSource):
    adapter_id = "wechat.localdb"
    name, icon, slug = "微信", "微", "wechat"
    description = "从已授权的只读快照中，按聊天与消息时间导入。"
    profiles = ("readonly-sqlite",)

    def configured_scopes(self, config):
        # Legacy connection-level conversations must not hide the catalog.
        # Every reader still receives only the explicit per-job selection.
        return conversation_catalog(WeChatAdapter(config))

    def validate(self, binding, selection):
        self.narrow_config(binding['config'], selection)

    def narrow_config(self, config, selection):
        self.validate_time(selection)
        available = {row['id'] for row in self.configured_scopes(config)}
        if not selection.scopes or not set(selection.scopes) <= available:
            raise AdapterError('import_scope_denied')
        return {**config, 'conversations': [scope.removeprefix('conversations:')
                                          for scope in selection.scopes]}

    def reader_class(self, profile):
        return WeChatAdapter


source = WeChatSource()
