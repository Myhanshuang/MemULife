from contextlib import nullcontext
from threading import Event, Lock
from types import SimpleNamespace

from memu.service import MemoryService


def test_new_write_between_views_yields_without_changing_view_order():
    state = {'writes': {}, 'deletion_epoch': 0, 'pending_views': {
        name: {'view': 'day', 'scope': name, 'ref': name, 'state': 'queued',
               'due': 0, 'generation': 1}
        for name in ('first', 'second')
    }}
    service = MemoryService.__new__(MemoryService)
    service.repo = SimpleNamespace(state=state, lock=nullcontext, save_state=lambda: None)
    service._maintenance_lock = Lock()
    service.changed = Event()
    service._discover_topics = lambda **kwargs: None
    service._dependencies_ready = lambda job: True
    refreshed = []

    def refresh(job, generation, epoch):
        refreshed.append(job['ref'])
        state['pending_views'][job['scope']]['state'] = 'done'
        if job['ref'] == 'first':
            state['writes']['retried-image'] = {'state': 'queued'}

    service._refresh_view = refresh
    assert service.refresh_due() == ['first']
    assert state['pending_views']['second']['state'] == 'queued'
    state['writes']['retried-image']['state'] = 'done'
    assert service.refresh_due() == ['second']
    assert refreshed == ['first', 'second']
