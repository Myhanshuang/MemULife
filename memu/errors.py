"""Stable service error codes; never turn an invalid result into empty success."""


class MemoryError(Exception):
    code = "memory_error"


class AccessDenied(MemoryError, PermissionError):
    code = "access_denied"


class IndexNotReady(MemoryError):
    code = "index_not_ready"


class RevisionConflict(MemoryError):
    code = "revision_conflict"


class ModelNotConfigured(MemoryError):
    code = "model_not_configured"


class RequestConflict(MemoryError):
    code = "idempotency_conflict"
