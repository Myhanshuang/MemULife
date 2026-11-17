"""One source of policy for prompts, tools, workers and authorization."""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from .types import StrictModel
from .errors import AccessDenied
from .environment import environment_config


class RepositoryConfig(StrictModel):
    timezone: str = "Asia/Singapore"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value


class EndpointConfig(StrictModel):
    endpoint: str = ""
    model: str = ""
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0)


class MLLMConfig(EndpointConfig):
    concurrency: int = Field(default=1, ge=1, le=32)
    protocol: Literal["gateway", "llamacpp"] = "gateway"
    max_output_tokens: int = Field(default=4096, ge=128)
    repair_attempts: int = Field(default=2, ge=0, le=2)


class EmbeddingConfig(EndpointConfig):
    concurrency: int = Field(default=1, ge=1, le=32)
    model_config = {"protected_namespaces": ("model_validate", "model_dump")}
    backend: Literal["http", "local_gemma", "vllm_gemma"] = "http"
    model_revision: str = ""
    model_path: str = ""
    threads: Literal[1, 2, 4] = 2
    idle_seconds: float = Field(default=30.0, ge=1, le=3600)

    @model_validator(mode="after")
    def local_path(self):
        if self.backend == "local_gemma" and not self.model_path:
            raise ValueError("Local Gemma requires model_path")
        if self.backend == "vllm_gemma" and not self.model_revision.strip():
            raise ValueError("vLLM Gemma requires an immutable model_revision for vector identity")
        return self


class ModelsConfig(StrictModel):
    mllm: MLLMConfig = Field(default_factory=MLLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

class BuildConfig(StrictModel):
    detail: str = "detailed"
    blacklist: list[str] = Field(default_factory=list)
    relation_candidates: int = Field(default=12, ge=1)
    relation_limit: int = Field(default=4, ge=0)
    relation_vocab: list[str] = Field(default_factory=lambda: ["延续", "补充", "回应", "更正", "同一事件", "因于", "相关"])
    digest_threshold_chars: int = Field(default=12000, ge=1)
    task_batch_chars: int = Field(default=32000, ge=100)
    unit_batch_items: int = Field(default=8, ge=1)
    compact_context_chars: int = Field(default=4096, ge=256)
    max_split_depth: int = Field(default=6, ge=1, le=10)
    source_priority_backlog: int = Field(default=32, ge=1, le=10000)
    source_priority_quiet_seconds: float = Field(default=30, ge=1, le=300)

    @field_validator("blacklist")
    @classmethod
    def compile_blacklist(cls, patterns):
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    @field_validator("relation_vocab")
    @classmethod
    def safe_relations(cls, values):
        if not values or len(set(values)) != len(values):
            raise ValueError("Relation vocabulary must be nonempty and unique")
        if any(not word or re.search(r"[\s>;|]", word) for word in values):
            raise ValueError("Relation words may not contain structural separators")
        return values


class ViewsConfig(StrictModel):
    refresh_delay_seconds: float = Field(default=60.0, ge=0)
    daily_at: str = "02:10"
    runtime_rebuild_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] = "sunday"
    memory_overview_chars: int = Field(default=1200, ge=1)
    per_topic_chars: int = Field(default=400, ge=1)
    day_summary_chars: int = Field(default=1600, ge=100)
    month_summary_chars: int = Field(default=800, ge=100)
    topic_min_sources: int = Field(default=2, ge=2)
    topic_min_days: int = Field(default=2, ge=2)

    @field_validator("daily_at")
    @classmethod
    def clock_time(cls, value):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("daily_at must be HH:MM")
        return value


class ReadConfig(StrictModel):
    default_method: Literal["grep", "semantic"] = "grep"
    limit: int = Field(default=12, ge=1)
    budget_chars: int = Field(default=6000, ge=1)
    relation_hops: int = Field(default=1, ge=1, le=8)
    rrf_constant: int = Field(default=60, ge=1)
    task_read_calls: int = Field(default=4, ge=0)
    task_read_chars: int = Field(default=12000, ge=0)


class AccessConfig(StrictModel):
    whitelist: dict[str, list[Literal["read", "write"]]] = Field(default_factory=dict)
    owners: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def owner_access(self):
        for owner in self.owners:
            if owner not in self.whitelist or "write" not in self.whitelist[owner]:
                raise ValueError("Every owner must be a whitelisted writer")
        return self


class DatabaseConfig(StrictModel):
    dsn_env: str = "MEMU_DATABASE_URL"


class HTTPConfig(StrictModel):
    host: str = Field(default='127.0.0.1', min_length=1)
    port: int = Field(default=5071, ge=1024, le=65535)
    allowed_hosts: list[str] = Field(default_factory=lambda: ['localhost', '127.0.0.1', '::1'])
    allowed_origins: list[str] = Field(default_factory=list)
    max_requests: int = Field(default=16, ge=4, le=64)
    max_body_bytes: int = Field(default=16 * 1024 ** 2, ge=1024, le=96 * 1024 ** 2)

    @field_validator('allowed_hosts')
    @classmethod
    def exact_hosts(cls, values):
        if not values or any(not value or any(c in value for c in '/*@?#') for value in values):
            raise ValueError('allowed_hosts must contain explicit DNS names or IP addresses')
        return [value.lower() for value in values]

    @field_validator('allowed_origins')
    @classmethod
    def exact_origins(cls, values):
        origins = []
        for value in values:
            try:
                parsed = urlsplit(value)
                if (not value.isascii() or any(c.isspace() for c in value) or '*' in value or
                        '\\' in value or parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                        parsed.username is not None or parsed.password is not None or
                        parsed.path or parsed.query or parsed.fragment):
                    raise ValueError
                port = parsed.port
                host = parsed.hostname
                if ':' in host:
                    host = '[' + host + ']'
                suffix = '' if port is None or port == {'http': 80, 'https': 443}[parsed.scheme] else f':{port}'
                origins.append(f'{parsed.scheme}://{host}{suffix}')
            except ValueError:
                raise ValueError('allowed_origins requires exact HTTP(S) origins without paths or wildcards') from None
        return list(dict.fromkeys(origins))


class MCPConfig(HTTPConfig):
    write_enabled: bool = True


class CoreConfig(HTTPConfig):
    port: int = Field(default=5072, ge=1024, le=65535)


class Config(StrictModel):
    repository: RepositoryConfig = Field(default_factory=RepositoryConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    views: ViewsConfig = Field(default_factory=ViewsConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    core: CoreConfig = Field(default_factory=CoreConfig)

    @classmethod
    def from_environment(cls, environment=None) -> Config:
        return cls.model_validate(environment_config(os.environ if environment is None else environment))

    def authorize(self, source: str, permission: str) -> None:
        if not isinstance(source, str) or source not in self.access.whitelist:
            raise AccessDenied("Source is not in the whitelist")
        if permission == "manage":
            if source not in self.access.owners:
                raise AccessDenied("Owner permission is required")
        elif permission not in self.access.whitelist[source]:
            raise AccessDenied(f"Source does not have {permission} permission")

    def task_policy(self) -> dict:
        return {
            **self.build.model_dump(),
            "memory_overview_chars": self.views.memory_overview_chars,
            "per_topic_chars": self.views.per_topic_chars,
            "day_summary_chars": self.views.day_summary_chars,
            "month_summary_chars": self.views.month_summary_chars,
            "timezone": self.repository.timezone,
        }
