"""Failure policy shared by durable jobs; no network writes are replayed here."""
import builtins
import httpx

from .errors import IndexNotReady


def error_code(error):
    if isinstance(error, builtins.MemoryError):
        return 'resource_unavailable'
    if isinstance(error, (httpx.TimeoutException, builtins.TimeoutError)):
        return 'model_timeout'
    if isinstance(error, httpx.TransportError):
        return 'model_unavailable'
    if isinstance(error, httpx.HTTPStatusError):
        return 'model_throttled' if error.response.status_code == 429 else 'model_http_error'
    return getattr(error, 'code', 'task_failed')


def retryable(error):
    if isinstance(error, (builtins.MemoryError, builtins.TimeoutError, httpx.TransportError, IndexNotReady)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    # psycopg is optional outside the core; inspect the exception hierarchy lazily.
    return any(cls.__module__.startswith('psycopg') and cls.__name__ in {
        'OperationalError', 'InterfaceError', 'SerializationFailure', 'DeadlockDetected',
    } for cls in type(error).__mro__)


def failure(error, attempt, now):
    code = error_code(error)
    temporary = retryable(error) and attempt < 4
    return {'state': 'waiting' if temporary else 'failed', 'error_code': code,
            'error': code, 'attempts': attempt,
            'retry_at': now + min(1800, 150 * 2 ** max(0, attempt - 1)) if temporary else None}
