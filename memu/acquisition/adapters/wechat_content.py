"""Project WeChat message payloads into readable text and explicit media refs."""

import re
from xml.etree import ElementTree as ET

from .base import Attachment


def xml(text):
    if not isinstance(text, str) or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        return None
    try:
        return ET.fromstring(text[text.index('<'):])
    except (ValueError, ET.ParseError):
        return None


def image_ref(identity, digest=None):
    metadata = {'reason': 'local_media_not_resolved'}
    if isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{32}', digest):
        metadata['content_md5'] = digest.lower()
    return Attachment(identity, metadata=metadata)


def project_message(content, local_type):
    kind = int(local_type) & 0xFFFFFFFF
    if kind in {1, 10000, 10002}:
        return content if isinstance(content, str) else None, []
    root = xml(content)
    if kind == 3:
        image = root.find('.//img') if root is not None else None
        return None, [image_ref('media', image.get('md5') if image is not None else None)]
    if kind == 47:
        emoji = root.find('.//emoji') if root is not None else None
        attachment = image_ref('media', emoji.get('md5') if emoji is not None else None)
        attachment.metadata.update(media_kind='emoji', reason='emoji_not_resolved')
        return None, [attachment]
    if kind in {34, 43}:
        return None, [Attachment('media', metadata={'reason': 'local_media_not_resolved'})]
    if kind != 49 or root is None:
        return None, []
    app = root.find('.//appmsg')
    if app is None:
        return None, []
    subtype = app.findtext('type') or str(int(local_type) >> 32)
    title, description, url = (app.findtext(field, '').strip() for field in ('title', 'des', 'url'))
    if subtype == '19':
        record = xml(app.findtext('recorditem'))
        if record is None:
            return '\n'.join(x for x in ('[转发的聊天记录]', title, description) if x), []
        lines, attachments = ['[转发的聊天记录]', title], []
        for index, item in enumerate(record.findall('.//dataitem')):
            if index >= 200:
                lines.append('〔其余转发条目未展开，原始记录已保留〕')
                break
            name = item.findtext('sourcename', '').strip()
            stamp = item.findtext('sourcetime', '').strip()
            lines.append(' | '.join(x for x in (name, stamp) if x))
            text = item.findtext('datadesc', '').strip()
            if text:
                lines.append(text)
            if item.get('datatype') == '2':
                attachments.append(image_ref(f'forwarded-{index}', item.findtext('fullmd5')))
                lines.append('〔转发记录中的图片〕')
            elif item.get('datatype') not in {'1', None}:
                label = item.findtext('datatitle', '').strip()
                if label:
                    lines.append(label)
            lines.append('')
        return '\n'.join(lines).strip(), attachments
    if subtype in {'5', '33', '36'}:
        return '\n'.join(x for x in ('[分享链接]', title, description, url) if x), []
    if subtype == '6':
        return '\n'.join(x for x in ('[文件]', title, description) if x), [
            Attachment('media', metadata={'reason': 'local_media_not_resolved', 'name': title})
        ]
    return '\n'.join(x for x in ('[微信分享内容]', title, description, url) if x), []
