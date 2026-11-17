from copy import deepcopy
from threading import RLock
from types import SimpleNamespace
import hashlib
import os
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.service import AcquisitionService
from memu.acquisition.pairing import BrowserPairing
from memu.config import Config
from memu.persistence import digest


def test_edit_connection_name_preserves_import_identity_and_ownership():
    service = AcquisitionService.__new__(AcquisitionService)
    service.lock = RLock()
    service.config = Config.model_validate({'access': {'owners': ['owner'], 'whitelist': {'owner': ['write']}}})
    binding = {'source_instance_id': 'source', 'owner': 'owner', 'source_identity': 'owner',
               'adapter_id': 'browser.ai-history', 'source_profile': 'gemini', 'source_namespace': 'Gemini',
               'enabled': True, 'config': {'provider': 'gemini', 'account_id': 'account', 'workspace_id': 'u/1'}}
    service.state = {'sources': {'source': deepcopy(binding)}}
    saved = []
    service.save = lambda: saved.append(deepcopy(service.state))
    original_hash = digest(service.binding('owner', 'source'))
    service.rename('owner', 'source', '工作账号')
    assert digest(service.binding('owner', 'source')) == original_hash
    assert service.inventory('owner')['sources'][0]['namespace'] == '工作账号'
    assert saved[0]['sources']['source']['source_namespace'] == 'Gemini'
    service.config.access.whitelist['another-owner'] = ['write']
    service.config.access.owners.append('another-owner')
    with pytest.raises(AdapterError, match='scope_denied'):
        service.rename('another-owner', 'source', 'wrong owner')
    with pytest.raises(AdapterError):
        service.rename('owner', 'source', ' ')
    assert len(saved) == 1


def test_delete_connection_revokes_admission_preserves_receipts_and_other_sources(tmp_path):
    config = Config.model_validate({'access': {'owners': ['owner', 'other'],
                                               'whitelist': {'owner': ['write'], 'other': ['write']}}})
    core_calls = []
    client = SimpleNamespace(tokens={'owner': 'fixture-token'},
                             request=lambda *args: core_calls.append(args) or {'refs': []})
    service = AcquisitionService(tmp_path, client, config)
    try:
        def register(account):
            return service.register('owner', {'adapter_id': 'browser.ai-history', 'source_profile': 'gemini',
                'source_namespace': account, 'config': {'provider': 'gemini', 'account_id': account,
                                                       'workspace_id': 'default'}})['source_id']
        source, other = register('first'), register('second')
        selection = {'scopes': ['conversation']}
        pending = service.new_job('owner', source, selection, 'config')
        accepted = service.new_job('owner', source, selection, 'config')
        accepted.update(state='processing', delivery_complete=True)
        neighbour = service.new_job('owner', other, selection, 'config')
        service.state['subscriptions']['sync'] = {'source_id': source, 'state': 'active'}
        service.state['subscriptions']['other-sync'] = {'source_id': other, 'state': 'active'}
        secret_name = 'MEMU_SOURCE_DELETE_FIXTURE_' + source.replace('-', '')
        service.credentials.save(source, {secret_name: 'synthetic-secret'})
        pairing = BrowserPairing(service)
        origin = 'chrome-extension://' + 'a' * 32
        token = 'synthetic-browser-token'
        key = hashlib.sha256(token.encode()).hexdigest()
        pairing.sessions[key] = {'source_id': source, 'owner': 'owner', 'origin': origin}
        pairing.sessions['other'] = {'source_id': other, 'owner': 'owner', 'origin': origin}
        with pytest.raises(AdapterError, match='scope_denied'):
            service.delete_source('other', source)
        assert service.binding('owner', source)
        assert service.delete_source('owner', source)['deleted'] is True
        assert service.delete_source('owner', source)['deleted'] is True  # lost response is safe to retry
        inventory = {row['source_id']: row for row in service.inventory('owner')['sources']}
        assert inventory[source]['deleted_at'] == service.state['sources'][source]['deleted_at']
        assert inventory[source]['enabled'] is False
        assert inventory[other]['deleted_at'] is None and inventory[other]['enabled'] is True
        assert pending['state'] == 'cancelled'
        assert accepted['state'] == 'processing' and neighbour['state'] == 'queued'
        assert service.state['subscriptions']['sync']['state'] == 'cancelled'
        assert service.state['subscriptions']['other-sync']['state'] == 'active'
        assert source not in service.credentials.values and secret_name not in os.environ
        assert not core_calls  # deletion never calls the memory core
        with pytest.raises(AdapterError, match='import_binding_unavailable'):
            pairing.authenticate(origin, token)
        pairing.revoke('owner', source)
        assert set(pairing.sessions) == {'other'}
        pairing.revoke('owner', other)
        remaining = next(row for row in service.inventory('owner')['sources'] if row['source_id'] == other)
        assert remaining['deleted_at'] is None and remaining['enabled'] is True  # disconnect is not deletion
        assert service.job_detail('owner', accepted['job_id'])['provider'] == 'gemini'
        assert len(service.jobs('owner')['jobs']) == 3
        assert service.binding('owner', other)['enabled'] is True
    finally:
        service.close()
