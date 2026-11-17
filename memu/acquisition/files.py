"""Finite selected file reads, with originals retained independently of parsing."""
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from markdown_it import MarkdownIt
from .adapters.base import AdapterError, Attachment, Page, Snapshot
from .adapters.local.common import LocalAccess
from .sources.file_selection import resolve, validate_frozen, version
from .file_content import extract_file


class FileReader:
    def __init__(self, binding, selection):
        self.config, self.selection = binding['config'], selection
        self.root = Path(self.config['root']).expanduser().resolve(strict=True)
        self.access = LocalAccess({'root': str(self.root),
            'max_record_bytes': self.config.get('max_file_bytes', 16 * 1024 * 1024)})

    def read(self, mode, cursor=None, limit=50):
        validate_frozen(self.config, self.selection)
        offset = (cursor or {}).get('offset', 0)
        if type(offset) is not int or not 0 <= offset <= len(self.selection.scopes):
            raise AdapterError('source_cursor_invalid')
        records, warnings = [], set()
        for name in self.selection.scopes[offset:offset + limit]:
            path = resolve(self.root, name)
            raw = self.access.read_file(path)
            if version(path) != self.selection.revisions[name]:
                raise AdapterError('import_candidate_changed')
            text, mime, parse_state = extract_file(name, raw)
            attachments = []
            tokens = (MarkdownIt().parse(text) if text and path.suffix.lower() in {'.md', '.markdown'} else [])
            for token in [child for parent in tokens for child in [parent, *(parent.children or [])]]:
                if token.type in {'html_block', 'html_inline'}:
                    warnings.add('html_attachment_mapping')
                if token.type not in {'image', 'link_open'}:
                    continue
                target = token.attrGet('src' if token.type == 'image' else 'href')
                if not target or target.startswith('#'):
                    continue
                if len(attachments) >= 64:
                    raise AdapterError('attachment_budget')
                uri = urlsplit(urljoin(path.as_uri(), target))
                attachment = Attachment('link:' + str(len(attachments)), source_uri=uri.geturl(), status='excluded')
                if self.selection.include_attachments and uri.scheme == 'file' and not uri.netloc:
                    local = Path(unquote(uri.path))
                    try:
                        attachment.data = self.access.read_file(local)
                        attachment.media_type = mimetypes.guess_type(local.name)[0] or 'application/octet-stream'
                        attachment.attachment_id = f'{len(attachments)}-{local.name}'
                        attachment.status = 'available'
                    except AdapterError:
                        raise AdapterError('local_attachment_unavailable') from None
                elif self.selection.include_attachments and token.type == 'image':
                    attachment.status = 'pending'
                    attachment.metadata['error_code'] = 'remote_attachment_download_unsupported'
                attachments.append(attachment)
            records.append(Snapshot(name, raw, text, media_type=mime,
                revision=self.selection.revisions[name], metadata={'path': name,
                    'raw_sha256': hashlib.sha256(raw).hexdigest(), 'material_kind': 'local_file',
                    'parse_state': parse_state},
                attachments=attachments))
        validate_frozen(self.config, self.selection)
        next_offset = offset + len(records)
        return Page(records, {'offset': next_offset}, next_offset < len(self.selection.scopes),
                    scope_complete=False, warnings=sorted(warnings))
