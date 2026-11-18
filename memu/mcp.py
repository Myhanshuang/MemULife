"""Stateless MCP dispatch. All memory operations go through the core HTTP API."""
import json
import re
from . import __version__
from .core_client import CoreUnavailable
from .events import event

PROTOCOL_VERSION = "2025-06-18"
INSTRUCTIONS = (
    "默认从 MEMORY 找到 Runtime，再按需读 Unit 与来源；已有定位符就直接读。"
    "有名称、原话、明确文字线索时优先 grep；换了说法，或有图片、声音线索时用 semantic。"
    "结果不足就换词、加别名、放宽时间、读关系邻居，再不行就在全部范围里查。"
    "索引用来继续读取，事实以实际正文和媒体为准。"
)
WRITE_INSTRUCTIONS = (
    "用户授权保存或整理记忆后才调用 memory_write。新增真实材料使用 mode=ingest，accepted 后用 memory_status 查询。"
    "修复已有记忆先读取目标和证据，使用 mode=revise、edit_revision、原样 old_text 与最终 new_text；"
    "revise 顶层必须带 reason、idempotency_key、changes 数组，目标和替换字段放在 changes 的每一项内。"
    "后台不会重新解释改稿。保留事实，明确区分用户陈述与推断，不根据不存在的证据编造结论。"
    "日/月 Summary 和 Runtime 用稳定 entry_id；修改默认保护。用户授权再次编辑时显式 allow_protected=true 保留保护；protect=false 解除保护。"
    "冲突必须重新阅读，不得盲目覆盖；一批全成或全败，重试保持同一 idempotency_key。"
    "修订 done 表示正文已提交，索引和汇总在后台更新；history=true 可读取修改历史。"
)


def external_write_error(arguments):
    """Adapters own projection/control metadata; external models cannot forge it."""
    metadata = arguments.get('metadata', {})
    if isinstance(metadata, dict) and {'projection', 'memulife', 'content'} & metadata.keys():
        return {'is_error': True, 'result': {'error': {
            'code': 'reserved_metadata', 'message': 'Internal import metadata is not available through MCP'}}}
    return None


def log_tool_error(name, response):
    if not response.get('is_error'):
        return
    value = response.get('result', {})
    error = value.get('error', {})
    code = error.get('code', value.get('status', 'tool_error'))
    code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,64}', code) else 'tool_error'
    trace_id = error.get('trace_id')
    fields = {'stage': name, 'error_code': code}
    if isinstance(trace_id, str) and re.fullmatch(r'[a-f0-9]{32}', trace_id):
        fields['request_id'] = trace_id
    # Do not record arguments, identities, locators, error messages or bodies.
    event('mcp_tool_rejected', **fields)



def _error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class MCPApplication:
    def __init__(self, core, *, write_enabled=True):
        self.core = core
        self.write_enabled = write_enabled

    def handle(self, request, identity):
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or
                not isinstance(request.get('method'), str)):
            return _error(None, -32600, 'Invalid JSON-RPC request')
        if 'id' not in request:
            return None  # Stateless notifications; disconnect never cancels a core commit.
        request_id = request['id']
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return _error(None, -32600, 'Request id must be a string or integer')
        method, params = request['method'], request.get('params', {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, 'params must be an object')
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': request_id, 'result': {}}
        if method == 'initialize':
            if (not isinstance(params.get('protocolVersion'), str) or
                    not isinstance(params.get('capabilities'), dict) or
                    not isinstance(params.get('clientInfo'), dict)):
                return _error(request_id, -32602, 'Invalid initialization parameters')
            return {'jsonrpc': '2.0', 'id': request_id, 'result': {
                'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {'listChanged': False}, 'resources': {}},
                'serverInfo': {'name': 'MemULife', 'version': __version__},
                'instructions': INSTRUCTIONS + (WRITE_INSTRUCTIONS if self.write_enabled else
                    'memory_write 已暂停，不可提交新的来源材料。') +
                    f'当前认证身份为 {identity}，所有工具的 from 必须使用此值。'}}
        try:
            if method == 'resources/templates/list':
                return {'jsonrpc': '2.0', 'id': request_id, 'result': {'resourceTemplates': [{
                    'uriTemplate': 'memu://media/{ref}', 'name': '原始媒体',
                    'description': '通过 memory_read 返回的媒体定位符读取原始媒体或选定片段。'}]}}
            if method == 'resources/list':
                return {'jsonrpc': '2.0', 'id': request_id, 'result': {'resources': []}}
            if method == 'resources/read':
                result = self.core.request('POST', '/v1/memory/resource', identity, {'uri': params.get('uri')})
                return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
            if method == 'tools/list':
                if params.get('cursor'):
                    return _error(request_id, -32602, 'Unknown tools cursor')
                result = self.core.tools(identity)
                if not self.write_enabled:
                    result = {**result, 'tools': [tool for tool in result['tools']
                                                 if tool['name'] not in {'memory_write', 'memory_create_unit'}]}
                return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
            if method != 'tools/call':
                return _error(request_id, -32601, 'Method not found')
            name, arguments = params.get('name'), params.get('arguments', {})
            if name not in {'memory_read', 'memory_search', 'memory_write', 'memory_manage', 'memory_status', 'memory_create_unit'}:
                return _error(request_id, -32602, 'Unknown tool')
            if not isinstance(arguments, dict):
                return _error(request_id, -32602, 'arguments must be an object')
            if name in {'memory_write', 'memory_create_unit'} and not self.write_enabled:
                response = {'is_error': True, 'result': {'error': {
                    'code': 'tool_disabled', 'message': f'{name} is temporarily disabled'}}}
            elif name == 'memory_write' and external_write_error(arguments):
                response = external_write_error(arguments)
            else:
                response = self.core.call(identity, name, arguments)
        except CoreUnavailable:
            if method != 'tools/call':
                return _error(request_id, -32000, 'Core unavailable; no automatic retry')
            response = {'is_error': True, 'result': {'error': {
                'code': 'core_unavailable',
                'message': 'Core response unavailable; outcome may be unknown. No automatic retry. '
                           'For writes, retain the original idempotency_key when checking or retrying.'}}}
        log_tool_error(name, response)
        value = response['result']
        if not isinstance(value, dict):
            value = {'result': value}
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {
            'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False, default=str)}],
            'structuredContent': value, 'isError': response['is_error']}}
