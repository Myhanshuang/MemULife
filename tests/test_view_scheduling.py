"""Bulk imports must not spend inference on repeatedly invalidated overviews."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

from memu.service import MemoryService
from memu.tasks.contract import TaskValidationError


def test_automatic_views_wait_for_ready_writes_but_explicit_refresh_works():
    ready, calls = ['pending-write'], []
    service = object.__new__(MemoryService)
    service.repo = SimpleNamespace(lock=nullcontext, ready_write_keys=lambda now: ready,
                                   state={'pending_views': {}})
    service._maintenance_lock = threading.Lock()
    service._discover_topics = lambda **kwargs: calls.append(kwargs)
    assert service.refresh_due() == [] and not calls
    service.refresh_due(force=True)
    assert calls == [{'force': True}]
    ready.clear()
    service.refresh_due()
    assert calls[-1] == {'force': False}


def test_view_errors_expose_fixed_categories_without_material():
    assert TaskValidationError('Summary exceeds its abstraction-level character budget').reason == 'summary_abstraction_budget'
    assert TaskValidationError('Summary repeats a tracked matter across entries').reason == 'summary_duplicate_matter'
    assert TaskValidationError('Unexpected private material').reason == 'output_contract'
