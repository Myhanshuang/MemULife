"""Attachment transport validation and material-level completeness accounting."""
import base64
import binascii

from .adapters.base import AdapterError, Attachment


def decode_attachments(values):
    if not isinstance(values, list) or len(values) > 64:
        raise AdapterError('source_attachment_invalid')
    result, seen, size = [], set(), 0
    for value in values:
        if not isinstance(value, dict) or set(value) - {
                'attachment_id', 'data', 'media_type', 'status', 'metadata'}:
            raise AdapterError('source_attachment_invalid')
        identity = value.get('attachment_id')
        mime, status = value.get('media_type'), value.get('status')
        if (not isinstance(identity, str) or not 1 <= len(identity) <= 512 or identity in seen
                or not isinstance(mime, str) or '/' not in mime or len(mime) > 200
                or status not in {'available', 'pending', 'excluded', 'referenced'}
                or not isinstance(value.get('metadata', {}), dict)):
            raise AdapterError('source_attachment_invalid')
        data = value.get('data')
        if data is not None:
            try:
                if not isinstance(data, str):
                    raise ValueError()
                data = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise AdapterError('source_attachment_invalid') from None
            size += len(data)
            if not data or size > 100 * 1024 * 1024 or status != 'available':
                raise AdapterError('source_attachment_invalid')
        elif status == 'available':
            raise AdapterError('source_attachment_invalid')
        seen.add(identity)
        result.append(Attachment(identity, data, mime, status=status, metadata=value.get('metadata', {})))
    return result


def summarize(materials):
    summary = dict(available=0, pending=0, excluded=0, unparsed=0)
    for material in materials:
        for item in material['metadata']['memulife'].get('attachments', []):
            if item.get('sha256'):
                summary['available'] += 1
                summary['unparsed'] += int(item.get('status') == 'retained')
            elif item.get('status') == 'referenced':
                summary['referenced'] = summary.get('referenced', 0) + 1
            elif item.get('status') == 'excluded':
                summary['excluded'] += 1
            else:
                summary['pending'] += 1
    return summary
