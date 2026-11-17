"""Dotenv settings for the resident service and HTTP diagnostics.

Dotenv is parsed as data: no shell execution or variable interpolation.
Credentials remain environment values, never serialized Config fields.
"""

import json
import os
from pathlib import Path
import re

from dotenv.parser import parse_stream


ENV_FIELDS = {
    'MEMU_TIMEZONE': ('repository.timezone', str),
    'MLLM_BASE_URL': ('models.mllm.endpoint', str),
    'MLLM_MODEL_NAME': ('models.mllm.model', str),
    'MLLM_PROTOCOL': ('models.mllm.protocol', str),
    'MLLM_TIMEOUT_SECONDS': ('models.mllm.timeout_seconds', float),
    'MLLM_MAX_OUTPUT_TOKENS': ('models.mllm.max_output_tokens', int),
    'MLLM_REPAIR_ATTEMPTS': ('models.mllm.repair_attempts', int),
    'MLLM_CONCURRENCY': ('models.mllm.concurrency', int),
    'EMB_BASE_URL': ('models.embedding.endpoint', str),
    'EMB_MODEL_NAME': ('models.embedding.model', str),
    'EMB_MODEL_REVISION': ('models.embedding.model_revision', str),
    'EMB_BACKEND': ('models.embedding.backend', str),
    'EMB_MODEL_PATH': ('models.embedding.model_path', str),
    'EMB_THREADS': ('models.embedding.threads', int),
    'EMB_CONCURRENCY': ('models.embedding.concurrency', int),
    'EMB_TIMEOUT_SECONDS': ('models.embedding.timeout_seconds', float),
    'EMB_IDLE_SECONDS': ('models.embedding.idle_seconds', float),
    'MEMU_MCP_HOST': ('mcp.host', str),
    'MEMU_MCP_PORT': ('mcp.port', int),
    'MEMU_MCP_ALLOWED_HOSTS': ('mcp.allowed_hosts', json.loads),
    'MEMU_MCP_ALLOWED_ORIGINS': ('mcp.allowed_origins', json.loads),
    'MEMU_MCP_MAX_REQUESTS': ('mcp.max_requests', int),
    'MEMU_MCP_MAX_BODY_BYTES': ('mcp.max_body_bytes', int),
    'MEMU_MCP_WRITE_ENABLED': ('mcp.write_enabled', json.loads),
    'MEMU_CORE_HOST': ('core.host', str),
    'MEMU_CORE_PORT': ('core.port', int),
    'MEMU_CORE_ALLOWED_HOSTS': ('core.allowed_hosts', json.loads),
    'MEMU_CORE_MAX_REQUESTS': ('core.max_requests', int),
    'MEMU_CORE_MAX_BODY_BYTES': ('core.max_body_bytes', int),
    'MEMU_BUILD_DETAIL': ('build.detail', str),
    'MEMU_BLACKLIST': ('build.blacklist', json.loads),
    'MEMU_RELATION_CANDIDATES': ('build.relation_candidates', int),
    'MEMU_RELATION_LIMIT': ('build.relation_limit', int),
    'MEMU_RELATION_VOCAB': ('build.relation_vocab', json.loads),
    'MEMU_DIGEST_THRESHOLD_CHARS': ('build.digest_threshold_chars', int),
    'MEMU_TASK_BATCH_CHARS': ('build.task_batch_chars', int),
    'MEMU_MAX_SPLIT_DEPTH': ('build.max_split_depth', int),
    'MEMU_SOURCE_PRIORITY_BACKLOG': ('build.source_priority_backlog', int),
    'MEMU_SOURCE_PRIORITY_QUIET_SECONDS': ('build.source_priority_quiet_seconds', float),
    'MEMU_UNIT_BATCH_ITEMS': ('build.unit_batch_items', int),
    'MEMU_COMPACT_CONTEXT_CHARS': ('build.compact_context_chars', int),
    'MEMU_VIEW_REFRESH_SECONDS': ('views.refresh_delay_seconds', float),
    'MEMU_DAILY_AT': ('views.daily_at', str),
    'MEMU_RUNTIME_REBUILD_WEEKDAY': ('views.runtime_rebuild_weekday', str),
    'MEMU_MEMORY_OVERVIEW_CHARS': ('views.memory_overview_chars', int),
    'MEMU_PER_TOPIC_CHARS': ('views.per_topic_chars', int),
    'MEMU_DAY_SUMMARY_CHARS': ('views.day_summary_chars', int),
    'MEMU_MONTH_SUMMARY_CHARS': ('views.month_summary_chars', int),
    'MEMU_TOPIC_MIN_SOURCES': ('views.topic_min_sources', int),
    'MEMU_TOPIC_MIN_DAYS': ('views.topic_min_days', int),
    'MEMU_READ_METHOD': ('read.default_method', str),
    'MEMU_READ_LIMIT': ('read.limit', int),
    'MEMU_READ_BUDGET_CHARS': ('read.budget_chars', int),
    'MEMU_RELATION_HOPS': ('read.relation_hops', int),
    'MEMU_RRF_CONSTANT': ('read.rrf_constant', int),
    'MEMU_TASK_READ_CALLS': ('read.task_read_calls', int),
    'MEMU_TASK_READ_CHARS': ('read.task_read_chars', int),
    'MEMU_ACCESS_WHITELIST': ('access.whitelist', json.loads),
    'MEMU_ACCESS_OWNERS': ('access.owners', json.loads),
}
ENV_NAMES = set(ENV_FIELDS) | {
    'MLLM_API_KEY', 'EMB_API_KEY', 'MEMU_MEMORY_ROOT', 'MEMU_DATABASE_URL',
    'MEMU_MCP_IDENTITY', 'MEMU_ENV_FILE',
    'MEMU_MCP_TOKENS', 'MEMU_CORE_URL', 'MEMU_CORE_TIMEOUT_SECONDS', 'MEMU_MCP_URL',
    'MEMU_ACQUISITION_ROOT', 'MEMU_ACQUISITION_HOST', 'MEMU_ACQUISITION_PORT', 'MEMU_ACQUISITION_URL',
    'MEMU_ACQUISITION_WORKERS',
    'MEMU_ACQUISITION_ALLOWED_HOSTS', 'MEMU_ACQUISITION_ALLOWED_ORIGINS',
}


def read_environment(*, environ=None, env_file=None):
    """Process variables override dotenv, including deliberately empty values."""
    inherited = dict(os.environ if environ is None else environ)
    selected = env_file if env_file is not None else inherited.get('MEMU_ENV_FILE')
    if selected is not None and not str(selected).strip():
        raise ValueError('MEMU_ENV_FILE must not be empty')
    path = Path(selected).expanduser() if selected is not None else Path.cwd() / '.env'
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    values = {}
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise ValueError(f'Invalid dotenv syntax at line {binding.original.line}')
                if binding.key is None:
                    continue
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', binding.key):
                    raise ValueError(f'Invalid dotenv key at line {binding.original.line}')
                if binding.key not in ENV_NAMES or binding.key == 'MEMU_ENV_FILE':
                    raise ValueError(f'Unsupported dotenv setting: {binding.key}')
                if binding.value is None or binding.key in values:
                    raise ValueError(f'Missing value or duplicate dotenv setting: {binding.key}')
                values[binding.key] = binding.value
        values['MEMU_ENV_FILE'] = str(path)
    elif selected is not None:
        raise FileNotFoundError('Selected MEMU_ENV_FILE does not exist')
    result = {**values, **inherited}
    if path.exists():
        result['MEMU_ENV_FILE'] = str(path)
    for name in ['MEMU_MEMORY_ROOT', 'EMB_MODEL_PATH', 'MEMU_ACQUISITION_ROOT']:
        if name not in result or not result[name]:
            continue
        location = Path(result[name]).expanduser()
        result[name] = str((location if location.is_absolute() else path.parent / location).resolve())
    if 'MEMU_MEMORY_ROOT' in result and not result['MEMU_MEMORY_ROOT']:
        raise ValueError('MEMU_MEMORY_ROOT must not be empty')
    return result


def load_environment():
    result = read_environment()
    # Only application settings may be introduced by this loader.
    os.environ.update({name: value for name, value in result.items() if name in ENV_NAMES})
    return result


def repository_root(explicit=None, *, environment=None):
    environment = os.environ if environment is None else environment
    value = explicit if explicit is not None else environment.get('MEMU_MEMORY_ROOT', Path.cwd() / 'memory')
    if not str(value).strip():
        raise ValueError('Memory repository path must not be empty')
    return Path(value).expanduser().resolve()


def environment_config(environment):
    result = {}

    def assign(path, value):
        current = result
        fields = path.split('.')
        for field in fields[:-1]:
            if field not in current:
                current[field] = {}
            if not isinstance(current[field], dict):
                raise ValueError(f'Invalid configuration section: {field}')
            current = current[field]
        current[fields[-1]] = value

    for name, (path, convert) in ENV_FIELDS.items():
        if name in environment:
            try:
                value = convert(environment[name])
            except (TypeError, ValueError):
                raise ValueError(f'Invalid environment setting: {name}') from None
            assign(path, value)
    for name, model in [('MLLM_API_KEY', 'mllm'), ('EMB_API_KEY', 'embedding')]:
        if name in environment:
            assign(f'models.{model}.api_key_env', name if environment[name] else None)
    if 'MEMU_DATABASE_URL' in environment:
        assign('database.dsn_env', 'MEMU_DATABASE_URL')
    return result
