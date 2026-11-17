"""One-time legacy provenance migration, outside the core model pipeline."""
from copy import deepcopy
import json

from .sources.browser_content import material_content


def migrate_metadata(metadata, text):
    value = deepcopy(metadata)
    origin = value.get('memulife', {})
    layout = origin.get('message_layout')
    audit = origin.get('transport_audit', {})
    if layout is None and audit.get('provider') == 'gemini':
        normalized = material_content(json.dumps(audit).encode(), 'gemini', origin['object_id'])
        if normalized and normalized['text'].splitlines() == text.splitlines():
            layout = normalized['message_layout']
    if layout:
        value.setdefault('content', {})['message_layout'] = layout
    return value
