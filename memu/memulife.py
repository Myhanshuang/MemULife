"""MemULife raw-material bridge. No repository, model or index access here.

Selected Snapshot/Page objects become ordinary memory_write requests. A private
outbox freezes each page before delivery; its cursor moves only after every
request reports a committed result. Replaying an interrupted page is explicit.
"""
import base64
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, is_dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .core_client import CoreUnavailable, RemoteCore
from .operations import WriteInput
from .persistence import encoded, digest, private_save  # noqa: F401


class BridgeError(ValueError):
    pass


def binding(source):
    names = ('adapter_id', 'source_profile', 'source_instance_id', 'source_namespace')
    if not isinstance(source, dict) or any(
            not isinstance(source.get(name), str) or not source[name].strip() for name in names):
        raise BridgeError('source_binding_required')
    return {name: source[name] for name in names}


def snapshot_material(source, snapshot):
    """Map a *selected* raw snapshot, without summarizing or creating Units.

    Accept the MemULife dataclass or its encode_page wire representation. Text
    is taken from the adapter; original JSON stays in the separate audit.
    The memory pipeline owns all extraction and summarization.
    """
    origin = binding(source)
    row = asdict(snapshot) if is_dataclass(snapshot) else dict(snapshot)
    if row.get('deleted'):
        raise BridgeError('tombstone_requires_explicit_memory_manage')
    object_id = row.get('object_id')
    if not isinstance(object_id, str) or not object_id:
        raise BridgeError('source_object_id_required')
    raw = row.get('raw')
    if isinstance(raw, str):
        raw = base64.b64decode(raw, validate=True)
    if not isinstance(raw, bytes):
        raise BridgeError('source_raw_required')
    metadata = row.get('metadata', {})
    if not isinstance(metadata, dict):
        raise BridgeError('source_metadata_invalid')
    text = row.get('text') or ''
    media, attachments, attachment_texts, attachment_originals = [], [], [], []

    def add_media(data, mime, filename=None):
        if isinstance(data, str):
            data = base64.b64decode(data, validate=True)
        if not isinstance(data, bytes) or not data:
            raise BridgeError('source_media_invalid')
        if not isinstance(mime, str) or mime.split('/')[0] not in {'image', 'audio', 'video'}:
            raise BridgeError('unsupported_attachment_media_type')
        item = {'data_base64': base64.b64encode(data).decode('ascii'), 'mime_type': mime}
        if filename:
            item['filename'] = Path(filename).name
        media.append(item)
        return hashlib.sha256(data).hexdigest()

    mime = row.get('media_type', 'application/json')
    local_file = origin['adapter_id'] == 'filesystem.typora' and metadata.get('material_kind') == 'local_file'
    json_type = isinstance(mime, str) and (mime == 'application/json' or mime.endswith('+json'))
    structural = (metadata.get('kind') in {'folder', 'directory_listing', 'location'} or
                  metadata.get('native_event_type') is not None or metadata.get('coverage') == 'unsupported_content_type')
    if not local_file and not text and isinstance(mime, str) and (mime.startswith('text/') or
            (json_type and origin['adapter_id'] in {'filesystem.typora', 'files.local'})):
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise BridgeError('source_text_not_utf8') from None
    if (isinstance(mime, str) and mime.split('/')[0] in {'image', 'audio', 'video'}
            and (not local_file or metadata.get('parse_state') == 'native_media')):
        add_media(raw, mime, metadata.get('path') if local_file else None)
    for item in row.get('attachments', []):
        descriptor = {key: item.get(key) for key in ('attachment_id', 'status', 'media_type', 'source_uri')}
        descriptor['metadata'] = item.get('metadata', {})
        if item.get('data') is not None:
            attachment_mime = item.get('media_type', '')
            if attachment_mime.startswith('text/') or attachment_mime == 'application/json' or attachment_mime.endswith('+json'):
                data = item['data']
                if isinstance(data, str):
                    data = base64.b64decode(data, validate=True)
                descriptor['sha256'] = hashlib.sha256(data).hexdigest()
                try:
                    original_text = data.decode('utf-8')
                except UnicodeDecodeError:
                    raise BridgeError('source_text_not_utf8') from None
                attachment_texts.append('\n\n' + json.dumps({'attachment_id': item.get('attachment_id'), 'media_type': attachment_mime}, ensure_ascii=False) + '\n' + original_text)
                descriptor['status'] = 'available'
            elif attachment_mime.split('/')[0] not in {'image', 'audio', 'video'}:
                data = item['data']
                if isinstance(data, str):
                    data = base64.b64decode(data, validate=True)
                descriptor['sha256'] = hashlib.sha256(data).hexdigest()
                descriptor['status'] = 'retained'
                attachment_originals.append({'attachment_id': item.get('attachment_id'),
                    'media_type': attachment_mime, 'data_base64': base64.b64encode(data).decode('ascii')})
            else:
                descriptor['sha256'] = add_media(item['data'], attachment_mime,
                    item.get('metadata', {}).get('name') or item.get('attachment_id'))
                descriptor['status'] = 'available'
        attachments.append(descriptor)
    if not isinstance(text, str):
        raise BridgeError('source_has_no_supported_material')
    text += ''.join(attachment_texts)
    audit_only = structural or (not text.strip() and not media and (json_type or local_file or mime.startswith('text/')))
    if audit_only and local_file:
        text = '# 文件原件\n\n' + json.dumps({'path': metadata.get('path'), 'media_type': mime,
            'size_bytes': len(raw), 'parse_state': metadata.get('parse_state')}, ensure_ascii=False)
    elif audit_only:
        text = ''
    elif not text.strip() and not media:
        raise BridgeError('source_has_no_supported_material')
    stamp = row.get('occurred_at')
    material = {
        'text': text,
        'time': {'precision': 'instant', 'value': stamp} if stamp else {'precision': 'unknown', 'value': None},
        'media': media,
        'metadata': {'memulife': {
            **origin, 'object_id': object_id, 'revision': row.get('revision'),
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_media_type': mime,
            'raw_size_bytes': len(raw), 'snapshot_metadata': metadata, 'attachments': attachments,
            **({'attachment_originals': attachment_originals} if attachment_originals else {}),
        }},
    }
    material['metadata']['content'] = {'audit_only': audit_only, 'projection_version': 'adapter-text-v2'}
    projection_generation = 2
    if local_file:
        projection_generation = 4
        material['metadata']['content'].update(material_kind='local_file', projection_version='local-file-v1')
        material['metadata']['memulife']['transport_audit'] = {
            'format': 'file-original-v1', 'raw': base64.b64encode(raw).decode('ascii')}
        attachments.append({'attachment_id': 'file:' + object_id, 'media_type': mime,
            'status': 'retained' if audit_only else 'available', 'sha256': hashlib.sha256(raw).hexdigest(),
            'metadata': {'parse_state': metadata.get('parse_state'), 'role': 'original'}})
    if json_type and not local_file:
        material['metadata']['memulife']['transport_audit'] = {
            'format': 'adapter-original-v1', 'raw': base64.b64encode(raw).decode('ascii')}
    if metadata.get('material_kind') == 'chat_window':
        if metadata.get('projection_version') == 'wechat-speakers-v2':
            projection_generation = 4
        material['metadata']['content'].update(material_kind='chat_window',
            message_layout=metadata['message_layout'], projection_version=metadata['projection_version'],
            sequential_relations=True, unit_limit=3, conversation_id=metadata['conversation_id'])
        material['metadata']['memulife']['transport_audit'] = {
            'format': 'chat-originals-v1', 'records': json.loads(raw)['records']}
    if origin['adapter_id'] == 'browser.ai-history':
        from .acquisition.sources.browser_content import material_content
        normalized = material_content(raw, origin['source_profile'], object_id)
        if normalized:
            material['text'], material['time'] = normalized['text'] + ''.join(attachment_texts), normalized['time']
            projection_generation = 3
            material['metadata']['content'].update(audit_only=False, projection_version='browser-content-v2')
            if attachments and all(item.get('sha256') for item in attachments):
                projection_generation = 4
                material['metadata']['content']['projection_version'] = 'browser-content-v2-attachments'
            material['metadata']['memulife']['message_count'] = normalized['message_count']
            material['metadata']['memulife']['message_layout'] = normalized['message_layout']
            if normalized['audit'] is not None:
                material['metadata']['memulife']['transport_audit'] = normalized['audit']
    from .acquisition.sources.local_ai_events import PROFILES
    if origin['source_profile'] in PROFILES and origin['adapter_id'] in {
            'ai.local-logs', 'local.claude-code', 'local.codex', 'local.gemini-cli', 'local.opencode', 'local.cursor'}:
        from .acquisition.sources.local_ai_content import material_content, GENERATION
        projection_generation = GENERATION
        normalized = material_content(raw, origin['source_profile'], metadata)
        material['text'] = normalized.pop('text')
        material['metadata']['content'] = {key: normalized[key] for key in (
            'message_layout', 'message_count', 'material_kind', 'audit_only', 'projection_version',
            'evidence_contract', 'projection_counts', 'unit_limit', 'sequential_relations', 'conversation_id', 'turn_id')}
        material['metadata']['memulife']['transport_audit'] = normalized['transport_audit']
        if normalized['audit_only']:
            # Raw audit lives in metadata; an empty placeholder is not a Source.
            material['text'] = ''
    if origin['adapter_id'] != 'browser.ai-history' and attachments and all(item.get('sha256') for item in attachments):
        projection_generation += 1
        material['metadata']['content']['projection_version'] += '-attachments'
    if origin['adapter_id'] == 'immich.assets' and metadata.get('original_mode') == 'reference':
        projection_generation = 4
        material['metadata']['content'].update(material_kind='media_reference', projection_version='immich-reference-v1')
    from .source_policy import audit_without_content
    if audit_without_content(material['metadata'], media=bool(media)):
        material['text'] = ''
    raw_hash = metadata.get('original_raw_sha256', hashlib.sha256(raw).hexdigest())
    material['metadata']['memulife']['raw_sha256'] = raw_hash
    material['metadata']['projection'] = {
        'generation': projection_generation,
        'version': material['metadata']['content']['projection_version'],
        'source_instance_id': origin['source_instance_id'], 'object_id': object_id, 'raw_sha256': raw_hash}
    material['idempotency_key'] = 'source:' + digest([origin['source_instance_id'], object_id, raw_hash])
    # Use the exact same material schema as agent writes, before staging a page.
    WriteInput.model_validate({'from': 'validation-only', **material})
    return material


def page_materials(source, page, carry=None):
    """For push adapters too: callers must apply source selection first."""
    if is_dataclass(page):
        page = asdict(page)
    if (not isinstance(page, dict) or not isinstance(page.get('records'), list)
            or len(page['records']) > 100 or not isinstance(page.get('cursor'), dict)
            or type(page.get('has_more')) is not bool
            or not isinstance(page.get('warnings'), list)
            or any(not isinstance(item, str) for item in page['warnings'])):
        raise BridgeError('source_page_invalid')
    records = page['records']
    remaining = None
    from .acquisition.sources.chat_content import ADAPTERS as CHAT_ADAPTERS
    if source.get('adapter_id') in CHAT_ADAPTERS:
        from .acquisition.sources.chat_content import group_records as group_chat
        records = group_chat(([carry] if carry else []) + records)
        if records and page['has_more'] and records[-1].get('metadata', {}).get('material_kind') == 'chat_window':
            remaining = records.pop()
            remaining['raw'] = base64.b64encode(remaining['raw']).decode('ascii')
    from .acquisition.sources.local_ai_events import PROFILES
    if source.get('source_profile') in PROFILES and source.get('adapter_id') in {
            'ai.local-logs', 'local.claude-code', 'local.codex', 'local.gemini-cli', 'local.opencode', 'local.cursor'}:
        from .acquisition.sources.local_ai_content import group_records
        records = group_records(([carry] if carry else []) + records, source['source_profile'])
        if records and page['has_more']:
            remaining = records.pop()
            remaining['raw'] = base64.b64encode(remaining['raw']).decode('ascii')
    requests = [snapshot_material(source, item) for item in records]
    requests = [m for m in requests if not (
        m['metadata']['content'].get('material_kind') == 'agent_worklog'
        and m['metadata']['content']['audit_only']
        and not m['metadata']['memulife']['snapshot_metadata'].get('audit_only_part'))]
    from .acquisition.attachments import summarize
    attachment_summary = summarize(requests)
    warnings = set(page['warnings'])
    if attachment_summary['pending']:
        warnings.add('attachment_pending')
    if attachment_summary['unparsed']:
        warnings.add('attachment_unparsed')
    return {
        'requests': requests,
        'cursor': page['cursor'], 'has_more': page['has_more'], 'warnings': sorted(warnings), 'next': 0,
        'attachment_summary': attachment_summary,
        'watch_checkpoint': page.get('watch_checkpoint', {}),
        **({'carry': remaining} if remaining else {}),
    }


class MemULifeBridge:
    def __init__(self, client, identity, source, selection, state_path, *, mode='live', admission=None,
                 checkpoint_store=None):
        binding(source)
        if not isinstance(selection, dict) or not selection.get('scopes'):
            raise BridgeError('explicit_selection_required')
        if mode not in {'live', 'backfill', 'discovery', 'reconcile'}:
            raise BridgeError('source_mode_invalid')
        self.client, self.identity, self.source = client, identity, source
        self.selection, self.mode = selection, mode
        self.admission = admission or nullcontext
        self.checkpoint_store = checkpoint_store
        self.path = Path(state_path).expanduser().resolve()
        # Never save source configuration/credentials in outbox identity metadata.
        self.fingerprint = digest({
            'source': {**binding(source), 'config': source.get('config', {})},
            'selection': selection, 'mode': mode, 'identity': identity, 'target': client.url.geturl(),
        })

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError('bridge_already_running') from None
            yield

    def load(self):
        state = (self.checkpoint_store.load_checkpoint(self.path.stem) if self.checkpoint_store is not None else
                 json.loads(self.path.read_bytes()) if self.path.exists() else None)
        state = state if state is not None else {
            'version': 1, 'binding': self.fingerprint, 'cursor': None, 'pending': None,
        }
        if state.get('version') != 1 or state.get('binding') != self.fingerprint:
            raise BridgeError('bridge_state_binding_mismatch')
        if state.get('pending'):
            self.freeze(state['pending'])
        return state

    def save(self, state):
        if self.checkpoint_store is not None:
            self.checkpoint_store.save_checkpoint(self.path.stem, state)
        else:
            private_save(self.path, state)

    def freeze(self, pending):
        """Write each immutable material once; checkpoints contain references only."""
        directory = self.path.parent / (self.path.stem + '.materials')
        directory.mkdir(exist_ok=True, mode=0o700)
        for position, material in enumerate(pending['requests']):
            if set(material) == {'material_ref'}:
                continue
            name = digest(material) + '.json'
            target = directory / name
            if not target.exists():
                private_save(target, material)
            pending['requests'][position] = {'material_ref': name}
        if pending.get('carry') and 'material_ref' not in pending['carry']:
            name = digest(pending['carry']) + '.json'
            private_save(directory / name, pending['carry'])
            pending['carry'] = {'material_ref': name}

    def material(self, item):
        if set(item) != {'material_ref'}:
            return item
        name = item['material_ref']
        if Path(name).name != name or len(name) != 69 or not name.endswith('.json'):
            raise BridgeError('outbox_material_invalid')
        return json.loads((self.path.parent / (self.path.stem + '.materials') / name).read_bytes())

    def stage(self, page):
        """Durable selected push acceptance; the worker still runs the full core pipeline."""
        with self.locked(), self.admission():
            state = self.load()
            if state['pending'] is not None or state['cursor'] is not None:
                raise BridgeError('page_already_staged')
            state['pending'] = page_materials(self.source, page)
            self.freeze(state['pending'])
            self.save(state)

    def deliver(self, state):
        pending = state['pending']
        count = 0
        while pending['next'] < len(pending['requests']):
            with self.admission():
                pass
            material = self.material(pending['requests'][pending['next']])
            try:
                response = self.client.write(self.identity, material)
            except CoreUnavailable:
                return {'status': 'pending', 'error_code': 'outcome_unknown', 'committed': count,
                        'resume_required': True}
            result = response.get('result', {})
            accepted_states = {'done', 'accepted', 'queued', 'running', 'waiting'}
            if response.get('is_error') is not False or result.get('status') not in accepted_states:
                error = result.get('error')
                return {'status': 'pending', 'committed': count, 'resume_required': True,
                        'error_code': error.get('code', 'write_not_committed') if isinstance(error, dict)
                                      else 'write_not_committed',
                        'request_id': result.get('request_id')}
            # done/index_ready=false is already committed. Do not replay as a new write.
            pending.setdefault('write_results', []).append({key: result[key] for key in ('request_id', 'status', 'sources', 'units', 'views', 'index_ready') if key in result})
            pending['next'] += 1
            count += 1
            self.save(state)
        previous_carry = state.get('carry')
        state['cursor'], state['pending'] = pending['cursor'], None
        state['carry'] = pending.get('carry')
        state['last_result'] = {'status': 'done', 'committed': len(pending.get('write_results', [])), 'has_more': pending['has_more'],
                                'warnings': pending['warnings'], 'write_results': pending.get('write_results', []),
                                'attachment_summary': pending.get('attachment_summary', {})}
        state['last_result']['watch_checkpoint'] = pending.get('watch_checkpoint', {})
        self.save(state)
        for item in pending['requests'] + ([previous_carry] if previous_carry and previous_carry != state['carry'] else []):
            if set(item) == {'material_ref'}:
                (self.path.parent / (self.path.stem + '.materials') / item['material_ref']).unlink(missing_ok=True)
        return state['last_result']

    def collect(self, reader, *, resume=False, max_pages=100):
        """Bounded explicit import; never poll sources or retry HTTP automatically."""
        if not 1 <= max_pages <= 10000:
            raise BridgeError('invalid_max_pages')
        with self.locked():
            state = self.load()
            if state['pending'] and not resume:
                raise BridgeError('pending_page_requires_resume')
            count, warnings = 0, set()
            for page_number in range(max_pages):
                if state['pending'] is None:
                    page = reader.read(self.mode, state['cursor'])
                    with self.admission():
                        carry = self.material(state['carry']) if state.get('carry') else None
                        state['pending'] = page_materials(self.source, page, carry)
                        self.freeze(state['pending'])
                        if state['pending']['has_more'] and state['pending']['cursor'] == state['cursor']:
                            raise BridgeError('source_cursor_did_not_advance')
                        self.save(state)
                with self.admission():
                    pass  # Recheck selection/job permissions before handing an accepted page to core.
                warnings.update(state['pending']['warnings'])
                result = self.deliver(state)
                count += result['committed']
                warnings.update(result.get('warnings', []))
                result.update(committed=count, pages=page_number + 1, warnings=sorted(warnings))
                if result['status'] != 'done' or not result['has_more']:
                    return result
            return {**result, 'status': 'more', 'has_more': True}


def add_parser(commands):
    child = commands.add_parser('collect', aliases=['import-memulife'], help='Collect selected raw source material')
    child.add_argument('--source', required=True, type=Path, help='MemULife source.json binding')
    child.add_argument('--selection', required=True, type=Path, help='Explicit MemULife Selection JSON')
    child.add_argument('--state', required=True, type=Path, help='Private outbox/cursor file for this selection')
    child.add_argument('--from', dest='identity', required=True)
    child.add_argument('--url', help='HTTP API base URL; defaults to MEMU_CORE_URL')
    child.add_argument('--mode', choices=['live', 'backfill', 'discovery', 'reconcile'], default='live')
    child.add_argument('--max-pages', type=int, default=100)
    child.add_argument('--resume', action='store_true', help='Explicitly replay the frozen pending page')


def run_import(args, environment):
    from .http_transport import load_tokens
    tokens = load_tokens(environment)
    if args.identity not in tokens:
        raise BridgeError('unknown_identity')
    source, selection = json.loads(args.source.read_bytes()), json.loads(args.selection.read_bytes())
    client = RemoteCore(args.url or environment.get('MEMU_CORE_URL', 'http://127.0.0.1:5072'),
                        tokens, environment.get('MEMU_CORE_TIMEOUT_SECONDS', '600'))
    from .acquisition.collector import SourceReader
    reader = SourceReader(source, selection)
    bridge = MemULifeBridge(client, args.identity, source, selection, args.state, mode=args.mode)
    return bridge.collect(reader, resume=args.resume, max_pages=args.max_pages)
