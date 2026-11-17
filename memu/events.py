"""Content-free, allowlisted structured operational events."""
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

_LOG = logging.getLogger('memu')
_FIELDS = {'request_id', 'stage', 'elapsed_ms', 'attempt', 'error_code', 'count',
           'status', 'task', 'http_status', 'retry_at', 'commit_id', 'validation_reason', 'job_id', 'source_id', 'error_type'}
_CONTEXT = ContextVar('memu_event_context', default={})


@contextmanager
def event_context(**fields):
    token = _CONTEXT.set({**_CONTEXT.get(), **{key: value for key, value in fields.items() if key in _FIELDS}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def configure_logging():
    if not _LOG.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False


def event(name, **fields):
    # Never accept arbitrary exception text, HTTP paths, model output or material.
    row = {'event': name, 'timestamp': time.time()}
    row.update(_CONTEXT.get())
    row.update({key: value for key, value in fields.items() if key in _FIELDS})
    _LOG.info(json.dumps(row, ensure_ascii=False, separators=(',', ':')))
