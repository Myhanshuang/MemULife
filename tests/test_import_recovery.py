from threading import RLock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memu.acquisition.service import AcquisitionService
from memu.core_client import CoreUnavailable
from memu.storage import _parse_entry, _render_entry
from memu.types import Entry, WorklogUnitDraft


def test_locator_discussion_roundtrips_without_creating_evidence_links():
    body = '局部引用键 S1 → c1:31-36、U1 → u2:8；示例 >c1:31-36。\n代码 for(;;) | >u999:1 \\'
    entry = Entry(time={'precision': 'unknown', 'value': None}, body=body, facts=['c6604:6-18'])
    draft = WorklogUnitDraft(body=body, facts=entry.facts, anchor='c6604:6-18', topics=[])
    parsed = _parse_entry(_render_entry(entry.model_dump(), 'owner'))
    assert parsed['body'] == draft.body == body
    assert parsed['facts'] == ['c6604:6-18']
    assert parsed['relations'] == []
    with pytest.raises(ValidationError):
        WorklogUnitDraft(body=' \n ', facts=[], anchor='c6604:6-18', topics=[])
    with pytest.raises(ValidationError):
        WorklogUnitDraft(body=body, facts=['not-a-reference'], anchor='c6604:6-18', topics=[])


@pytest.mark.parametrize('recovered_status', ['running', 'done', 'failed'])
def test_status_outage_recovers_without_resubmitting_writes(recovered_status):
    service = AcquisitionService.__new__(AcquisitionService)
    service.lock = RLock()
    service.save = lambda: None
    job = {'owner': 'owner', 'state': 'processing', 'error_code': None,
           'progress': {'write_results': [{'request_id': 'existing', 'status': 'queued'}]}}
    calls = []

    def unavailable(owner, keys):
        calls.append(keys)
        raise CoreUnavailable('temporary outage')

    # Deliberately no write method: receipt recovery must never resubmit material.
    service.client = SimpleNamespace(statuses=unavailable)
    service.check_writes(job)
    assert job['state'] == 'processing' and job['error_code'] == 'core_status_unavailable'
    receipt = {'request_id': 'existing', 'status': recovered_status}
    if recovered_status == 'failed':
        receipt['error'] = {'code': 'task_validation_failed'}
    service.client.statuses = lambda owner, keys: {'results': [receipt]}
    service.check_writes(job)
    assert job['state'] == {'running': 'processing', 'done': 'completed', 'failed': 'paused'}[recovered_status]
    assert job['error_code'] == ('task_validation_failed' if recovered_status == 'failed' else None)
    assert calls == [['existing']]
