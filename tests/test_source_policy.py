"""Focused checks for empty-audit filtering, attachment retention and stable deletion."""
import base64
from copy import deepcopy

import pytest

from memu.acquisition.adapters.base import Snapshot
from memu.errors import AccessDenied, RevisionConflict
from memu.memulife import snapshot_material
from test_service import service_fixture


def test_import_filters_audits_across_sources_and_keeps_original_attachments(tmp_path):
    service, model, _, _ = service_fixture(tmp_path)
    before = deepcopy(model.calls)
    for adapter, kind, text in [('notion.workspace', 'folder', None),
                                ('feishu.documents', 'directory_listing', None),
                                ('browser.ai-history', 'unknown', '  '),
                                ('qq.napcat', 'unknown', None)]:
        material = snapshot_material({'adapter_id': adapter, 'source_profile': 'fixture',
            'source_instance_id': adapter, 'source_namespace': adapter},
            Snapshot('empty-one', raw=b'{}', text=text, metadata={'kind': kind}))
        assert material['text'] == ''
        result = service.memory_write(from_='owner', **material)
        assert result['sources'] == [] and result['units'] == []
    metadata = {'content': {'audit_only': True}, 'memulife': {
        'attachments': [{'attachment_id': 'file-one', 'status': 'retained', 'metadata': {'name': '扫描件.pdf'}}],
        'attachment_originals': [{'attachment_id': 'file-one', 'media_type': 'application/pdf',
                                 'data_base64': base64.b64encode(b'pdf-fixture').decode()}]}}
    result = service.memory_write(from_='owner', text='', metadata=metadata)
    detail = service.memory_browse_detail('owner', ref=result['sources'][0])
    assert detail['display']['mode'] == 'attachment' and detail['media'][0]['name'] == '扫描件.pdf'
    assert model.calls == before


def test_cleanup_only_empty_audit_envelopes_preserves_lines_quotes_and_attachments(tmp_path):
    service, model, _, _ = service_fixture(tmp_path)
    repo = service.repo
    time = {'precision': 'unknown', 'value': None}
    placeholder = '# 来源审计（结构记录或没有可提取正文）'
    with repo.transaction():
        audits = [repo.save_source(placeholder, 'owner', time, {'content': {'audit_only': True},
            'memulife': {'adapter_id': adapter, 'transport_audit': {'raw_utf8': 'original audit'}}})
            for adapter in ('notion.workspace', 'feishu.documents')]
        quote = repo.save_source(placeholder, 'owner', time)
        meaningful_audit = repo.save_source('原始审计数据包含需要保留的具体内容。', 'owner', time, {'content': {'audit_only': True}})
        attachment = repo.save_source('# 文件原件', 'owner', time, {'content': {'audit_only': True, 'material_kind': 'local_file'}})
        remote = repo.save_source('资产引用', 'owner', time, {'content': {'audit_only': True},
            'memulife': {'attachments': [{'attachment_id': 'remote', 'status': 'referenced'}]}})
        unit = repo.append_entries('unit', [{'body': '正常记忆', 'time': time, 'facts': [quote]}], 'owner')[0]
    saved = {ref: repo.read(ref) for ref in [quote, meaningful_audit, attachment, remote, unit]}
    before = deepcopy(model.calls)
    visible = {item['ref'] for item in service.memory_browse('owner', layers=['source'])['items']}
    assert not set(audits) & visible and meaningful_audit not in visible
    assert {quote, attachment, remote} <= visible
    plan = service.memory_browse_cleanup_audits('owner')
    assert plan['source_count'] == 2 and plan['affected_units'] == plan['affected_views'] == 0
    with pytest.raises(AccessDenied):
        service.memory_browse_cleanup_audits('reader', revision=plan['revision'])
    with pytest.raises(RevisionConflict):
        service.memory_browse_cleanup_audits('owner', revision='0' * 64)
    assert service.memory_browse_cleanup_audits('owner', revision=plan['revision'])['source_count'] == 2
    for ref in audits:
        row = repo.catalog[ref.split(':')[0]]
        assert (repo.root / row['path']).read_text() == '\n'
        assert row['metadata']['memulife']['transport_audit']['raw_utf8'] == 'original audit'
    for ref, original in saved.items():
        current = repo.read(ref)
        assert (current['body'], current.get('start_line'), current.get('end_line')) == (original['body'], original.get('start_line'), original.get('end_line'))
    assert service.memory_browse_cleanup_audits('owner')['source_count'] == 0
    assert model.calls == before
