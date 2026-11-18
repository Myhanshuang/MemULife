"""Validated memory-operation contracts shared by the core API and MCP metadata."""
from typing import Literal
import uuid
from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from .types import StrictModel, Time, Relation
from .errors import MemoryError
from .events import event
from .retry import error_code

class MediaInput(StrictModel):
    data_base64: str = Field(min_length=1)
    mime_type: str = Field(pattern=r"^(image|audio|video)/[^\s]+$")
    filename: str | None = None


class CallerInput(StrictModel):
    from_: str = Field(alias="from", min_length=1,
                       description="必须与当前请求的认证身份一致。")


class ReadInput(CallerInput):
    refs: list[str] | None = None
    links: bool = False
    direction: Literal["both", "in", "out"] = "both"
    relations: list[str] | None = None
    hops: int | None = Field(default=None, ge=1, le=8)
    budget_chars: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    history: bool = False
    entry_id: str | None = None


class SearchInput(CallerInput):
    method: Literal["grep", "semantic"] = "grep"
    query: str = ""
    query_refs: list[str] | None = None
    query_media: list[MediaInput] | None = None
    layers: list[str] | None = None
    scope: dict | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)
    budget_chars: int | None = Field(default=None, ge=1)
    cursor: str | None = None


class RevisionChange(StrictModel):
    ref: str = Field(min_length=1)
    entry_id: str | None = None
    expected_revision: str = Field(min_length=1, description='来自目标条目的 edit_revision，不是文件 revision。')
    old_text: str = Field(max_length=100_000)
    new_text: str = Field(max_length=100_000)
    add_refs: list[str] = Field(default_factory=list, max_length=100)
    add_relations: list[Relation] = Field(default_factory=list, max_length=100)
    add_topics: list[str] = Field(default_factory=list, max_length=20)
    protect: bool | None = None
    allow_protected: bool = Field(default=False,
        description='用户明确再次编辑受保护内容时设为 true；仍校验版本，保存后默认继续保护。')


class WriteInput(CallerInput):
    model_config = {'json_schema_extra': {'allOf': [{
        'if': {'properties': {'mode': {'const': 'revise'}}, 'required': ['mode']},
        'then': {'required': ['changes', 'reason', 'idempotency_key'], 'properties': {
            'changes': {'minItems': 1},
            'reason': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
            'idempotency_key': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
            'text': {'const': ''}, 'media': {'maxItems': 0},
            'metadata': {'maxProperties': 0}, 'time': {'type': 'null'}}},
        'else': {'properties': {'changes': {'maxItems': 0}, 'reason': {'const': ''}}},
    }]}}
    mode: Literal['ingest', 'revise'] = 'ingest'
    text: str = ""
    time: Time | None = None
    media: list[MediaInput] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None,
        description='mode=revise 时必填且非空。同一次逻辑修改重试必须复用原值。')
    changes: list[RevisionChange] = Field(default_factory=list, max_length=20,
        description='mode=revise 时必填，1–20 项；ref、expected_revision、old_text、new_text 放在每一项内。')
    reason: str = Field(default='', max_length=2000,
        description='mode=revise 时顶层必填且非空：说明用户授权的修改理由。')

    @model_validator(mode='after')
    def revision_arguments(self):
        if self.mode == 'revise':
            missing = [field for field, present in (
                ('changes', bool(self.changes)), ('reason', bool(self.reason.strip())),
                ('idempotency_key', bool(self.idempotency_key and self.idempotency_key.strip()))) if not present]
            if missing:
                raise PydanticCustomError('revision_required',
                    'mode=revise requires nonempty top-level fields: {fields}', {'fields': ', '.join(missing)})
            if self.text or self.media or self.metadata or self.time:
                raise PydanticCustomError('revision_mixed_material',
                    'mode=revise cannot contain ingestion material: text, media, metadata or time')
        elif self.changes or self.reason:
            raise PydanticCustomError('revision_mode_required', 'changes and reason require mode=revise')
        return self


class CreateUnitInput(CallerInput):
    body: str = Field(min_length=1, max_length=100_000, pattern=r'\S')
    source_refs: list[str] = Field(min_length=1, max_length=100)
    relations: list[Relation] = Field(min_length=1, max_length=100,
        description='从新 Unit 指向已有 Unit 的关系。')
    time: Time | None = None
    reason: str = Field(max_length=2000, pattern=r'\S')
    idempotency_key: str = Field(max_length=512, pattern=r'\S')


class StatusInput(CallerInput):
    request_id: str | None = None
    idempotency_key: str | None = None


class ManageInput(CallerInput):
    action: Literal["update", "correct", "delete", "rebuild", "retry", "topics", "topic"]
    refs: list[str] | None = None
    with_source: bool = False
    source_refs: list[str] | None = None
    text: str = ""
    time: Time | None = None
    view: Literal["day", "month", "runtime", "memory", "index", "topics"] | None = None
    scope: str | None = Field(default=None, description="day: YYYY-MM-DD; month: YYYY-MM; runtime: canonical topic; memory: global")
    request_id: str | None = None


TOOL_MODELS = {
    "memory_read": ReadInput, "memory_search": SearchInput,
    "memory_create_unit": CreateUnitInput, "memory_write": WriteInput, "memory_manage": ManageInput,
    'memory_status': StatusInput,
}
DESCRIPTIONS = {
    "memory_read": "按稳定定位符读取入口、条目、行段或媒体。日/月 Summary 和 Runtime 返回稳定 entry_id；修订使用 edit_revision。可用 entry_id 定位条目、history=true 读取修订历史；可取入/出关系。",
    "memory_search": "grep 仅匹配文字；semantic 原生编码文字/媒体，可组合 query_refs；返回 Unit 与可继续读取的地址。",
    "memory_create_unit": "用户授权后，依据已有 Source 行段直接创建受保护 Unit，同时关联到已有 Unit。先读取证据与目标；提交最终正文、关系、理由和幂等键。返回新 Unit 定位符。",
    "memory_write": "用户授权后写入记忆。mode=ingest 保存真实素材并返回 accepted。mode=revise 顶层必须提供 reason、idempotency_key 和非空 changes 数组；每项包含 ref、expected_revision（读取返回的 edit_revision）、原样 old_text、new_text，直接原子修订 Unit、日/月 Summary 或 Runtime，不经内部模型重写。新增引用必须存在；Summary/Runtime 使用 entry_id，修改默认受保护；用户授权再次编辑受保护内容时指定 allow_protected=true，仍保留保护；仅显式 protect=false 解除保护。冲突时重新读取，禁止盲目覆盖；重试复用原幂等键。",
    'memory_status': '凭 request_id 或原 idempotency_key 查询本人写入状态、阶段、错误码和已提交地址；所有者可查询其他写入。',
    "memory_manage": "仅所有者：topics 列出跟踪主题；topic 用 scope 指定主题名、text 指定关注范围并归入历史记录；update 经来源事件更新视图；correct 删旧重建；delete 可清选定 Source；rebuild（含 topics）或 retry 维护失败任务。",
}


def tool_definitions(config) -> list[dict]:
    vocabulary = "；关系词表：" + "、".join(config.build.relation_vocab)
    definitions = [{
        "name": name,
        "description": description + (vocabulary if name in {"memory_read", "memory_search", "memory_create_unit"} else ""),
        "inputSchema": TOOL_MODELS[name].model_json_schema(by_alias=True),
        "annotations": {"readOnlyHint": name in {"memory_read", "memory_search", "memory_status"},
                        "destructiveHint": name in {"memory_manage", "memory_write"}, "openWorldHint": False},
    } for name, description in DESCRIPTIONS.items()]
    for definition in definitions:
        if definition["name"] == "memory_search":
            definition["inputSchema"]["properties"]["method"]["default"] = config.read.default_method
    return definitions


class CoreApplication:
    def __init__(self, service):
        self.service = service

    def write(self, identity, material):
        """Common ingress for agents, adapters and other authenticated producers."""
        return self.call(identity, 'memory_write', {'from': identity, **material})

    def tools(self, identity):
        if identity not in self.service.config.access.whitelist:
            raise PermissionError('Unknown identity')
        definitions = tool_definitions(self.service.config)
        for definition in definitions:
            definition['inputSchema']['properties']['from']['const'] = identity
        return {'tools': definitions}

    def call(self, identity, name, arguments):
        if name not in TOOL_MODELS:
            return self.failure('unknown_tool', 'Unknown memory operation')
        try:
            parsed = TOOL_MODELS[name].model_validate(arguments)
        except ValidationError as error:
            # Model-level validators have loc=(). Keep their reason instead of
            # returning an empty field list; never log input values or prose.
            details = [{'field': '.'.join(str(part) for part in item['loc']) or '$',
                        'code': item['type'], 'message': item['msg']}
                       for item in error.errors(include_input=False, include_context=False, include_url=False)]
            trace_id = uuid.uuid4().hex
            event('operation_rejected', request_id=trace_id, stage=name, error_code='invalid_arguments',
                  validation_reason=','.join(sorted({item['code'] for item in details})))
            message = '; '.join(f"{item['field']}: {item['message']}" for item in details)
            return self.failure('invalid_arguments', f'Invalid arguments: {message}',
                                details=details, trace_id=trace_id)
        if parsed.from_ != identity:
            return self.failure('access_denied', 'Request source differs from authenticated identity')
        arguments = parsed.model_dump(by_alias=True, exclude_unset=True)
        arguments['from_'] = arguments.pop('from')
        try:
            result = getattr(self.service, name)(**arguments)
            return {'result': result, 'is_error': isinstance(result, dict) and result.get('status') in {'failed', 'conflict'}}
        except (MemoryError, ValueError, PermissionError, FileNotFoundError) as error:
            return self.failure(getattr(error, 'code', 'invalid_request'), str(error))
        except Exception as error:
            event('operation_failed', stage=name, error_code=error_code(error))
            return self.failure('internal_error', 'Operation failed; inspect core configuration and task state')

    @staticmethod
    def failure(code, message, **details):
        return {'result': {'error': {'code': code, 'message': message, **details}}, 'is_error': True}
