"""Keep usable source material and attachments separate from audit-only receipts."""
import re


def has_preserved_attachment(metadata):
    content = metadata.get('content', {})
    origin = metadata.get('memulife', {})
    if content.get('material_kind') == 'local_file':
        return True
    if any(item.get('data_base64') for item in origin.get('attachment_originals', [])):
        return True
    return any(item.get('sha256') or item.get('status') in {'available', 'retained', 'referenced'}
               for item in origin.get('attachments', []))


def audit_without_content(metadata, *, media=False):
    return bool(metadata.get('content', {}).get('audit_only') and not media
                and not has_preserved_attachment(metadata))


def empty_audit_body(text):
    """Only recognize generated envelopes; never infer that ordinary prose is meaningless."""
    text = text.strip()
    return not text or bool(re.fullmatch(
        r'#{1,6}\s+(?:[a-z][a-z0-9-]* 原始审计（没有可提取的新事件）|来源审计（结构记录或没有可提取正文）)', text))
