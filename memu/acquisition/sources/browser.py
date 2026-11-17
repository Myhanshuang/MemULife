"""Ephemeral AI conversation discovery; content arrives through selected pushes."""

from .base import SourceModule


class BrowserSource(SourceModule):
    adapter_id = "browser.ai-history"
    name, icon, unit = "网页 AI", "AI", "对话"
    description = "先浏览会话目录，选中后才允许扩展读取和保存对话。"
    page_kind, slug = "conversations", "ai"
    profiles = ("chatgpt", "claude", "gemini", "perplexity", "deepseek", "grok")
    import_driver = "push"
    preview = True
    message_selection = True
    subscribes = True

    def scopes(self, binding):
        from .discovery import discovery

        return discovery.get(binding["tenant_id"], binding["source_instance_id"])

    def validate(self, binding, selection):
        # Explicit IDs remain valid after the transient catalogue expires.
        self.validate_time(selection)

    def prepare_plan(self, binding, selection):
        known = {row["id"]: row for row in self.scopes(binding)}
        return selection.model_copy(
            update={
                "locators": {
                    scope: known.get(scope, {}).get("locator", scope)
                    for scope in selection.scopes
                }
            }
        )

    def preview_page(self, binding, selection, cursor="", limit=30):
        from ..adapters.base import AdapterError
        from .message_preview import previews

        if len(selection.scopes) != 1 or selection.messages:
            raise AdapterError("import_preview_scope_invalid")
        conversation = selection.scopes[0]
        known = {row["id"]: row for row in self.scopes(binding)}
        locator = known.get(conversation, {}).get("locator", conversation)
        return previews.read(binding, conversation, locator, cursor, limit)

    def config_schema(self):
        return {
            "type": "object",
            "properties": {
                "provider": {"enum": list(self.profiles)},
                "account_id": {"type": "string", "minLength": 1},
                "workspace_id": {"type": "string", "minLength": 1},
            },
            "required": ["provider", "account_id", "workspace_id"],
            "additionalProperties": False,
        }


source = BrowserSource()
