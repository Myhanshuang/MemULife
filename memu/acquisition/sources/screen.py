"""Sealed screen segments; discovery never starts capture or opens video bytes."""

from copy import deepcopy

from ..adapters.base import AdapterError
from .base import SourceModule


class ScreenSource(SourceModule):
    adapter_id = "screen.native"
    name, icon, unit = "屏幕记录", "屏", "已有片段"
    description = "选择已经封存的片段；不会启动录屏。"
    page_kind, slug = "segments", "screen"
    profiles = ("wayland-portal-v1", "windows-wgc-v1")
    time_filter = True
    continuous_updates = False
    primary_media = True
    page_limit = 1
    event_types = ("screen.segment", "source.coverage")

    def scopes(self, binding):
        from ..adapters.screen import MAX_MANIFEST

        reader = self.reader_class(binding["source_profile"])(binding["config"])
        names, _ = reader._inventory()
        choices = []
        for name in names:
            item = reader._manifest(name, reader._read_file(name, MAX_MANIFEST))
            if item["kind"] == "sealed_segment":
                choices.append(
                    {
                        "id": f"{item['session_id']}/{item['stream_id']}/{item['segment_id']}",
                        "name": item["start_wallclock"] + " · " + item["segment_id"],
                        "kind": "segment",
                    }
                )
        return choices

    def narrow_config(self, config, selection):
        return deepcopy(config)

    def includes(self, record, selection):
        return record.object_id in selection.scopes

    def reader_class(self, profile):
        from ..adapters.screen import ScreenNativeAdapter

        return ScreenNativeAdapter

    def validate_profile_config(self, profile, config):
        if (
            config.get("backend")
            != {"wayland-portal-v1": "wayland_portal", "windows-wgc-v1": "windows_wgc"}[
                profile
            ]
        ):
            raise AdapterError("screen_profile_mismatch")


source = ScreenSource()
