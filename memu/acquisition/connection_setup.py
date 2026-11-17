"""Connect with credentials, bind the discovered identity, select content later."""
from copy import deepcopy

from jsonschema import Draft202012Validator

from .adapters.base import AdapterError
from .adapters.http import SourceHTTP, environment_secret, identifier


FIELDS = {
    'qq.napcat': ('base_url', 'token_env', 'history_order', 'ws_url', 'media_hosts'),
    'notion.workspace': ('token_ref', 'include_comments', 'max_requests'),
    'feishu.documents': ('access_token_ref', 'base_url', 'download_media', 'max_attachment_bytes', 'max_block_pages'),
}
IDENTITY = {'qq.napcat': 'account_id', 'notion.workspace': 'bot_id', 'feishu.documents': 'principal_id'}


def setup_schema(adapter, schema, profile=None):
    if adapter == 'qq.napcat' and profile == 'napcat-webui':
        return {'type': 'object', 'properties': {
            'base_url': {'type': 'string', 'minLength': 1, 'default': 'http://127.0.0.1:6099/webui/'},
            'webui_token_ref': deepcopy(schema['properties']['webui_token_ref'])},
            'required': ['base_url', 'webui_token_ref'], 'additionalProperties': False}
    properties = {key: deepcopy(schema['properties'][key]) for key in FIELDS[adapter]}
    if adapter == 'qq.napcat':
        properties['history_order']['default'] = 'oldest_first'
    required = {'qq.napcat': ['base_url', 'token_env'], 'notion.workspace': ['token_ref'],
                'feishu.documents': ['access_token_ref']}[adapter]
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def complete_connection(binding):
    adapter = binding['adapter_id']
    if adapter not in FIELDS:
        return binding
    config = deepcopy(binding['config'])
    # Explicit legacy bindings retain their existing account and scope boundaries.
    if config.get(IDENTITY[adapter]) or (adapter == 'notion.workspace' and config.get('workspace_id')):
        return binding
    from .sources import get_source
    module = get_source(adapter)
    if binding['source_profile'] not in module.profiles:
        raise AdapterError('source_profile_unknown')
    schema = setup_schema(adapter, module.reader_class(binding['source_profile']).config_schema, binding['source_profile'])
    if not Draft202012Validator(schema).is_valid(config):
        raise AdapterError('config_invalid')
    if adapter == 'qq.napcat':
        if binding['source_profile'] == 'napcat-webui':
            from .adapters.napcat_webui import connect
            return {**binding, 'config': connect(config)}
        from .adapters.qq import safe_endpoint
        from .adapters.local.common import native_id
        http = SourceHTTP(safe_endpoint(config['base_url']))
        try:
            headers = {'Authorization': 'Bearer ' + environment_secret('env:' + config['token_env'])}
            login, _ = http.json('POST', '/get_login_info', body={}, headers=headers)
            status, _ = http.json('POST', '/get_status', body={}, headers=headers)
            if any(value.get('status') != 'ok' or value.get('retcode') != 0 for value in (login, status)):
                raise AdapterError('upstream_read_failed')
            if not isinstance(login.get('data'), dict) or not native_id(login['data'].get('user_id')):
                raise AdapterError('incompatible_schema')
            if not isinstance(status.get('data'), dict) or status['data'].get('online') is not True:
                raise AdapterError('source_offline')
            config.update(account_id=native_id(login['data']['user_id']), browse_accessible=True)
            config.setdefault('history_order', 'oldest_first')
        finally:
            http.close()
    elif adapter == 'notion.workspace':
        from .adapters.notion import API_VERSION, native_id
        http = SourceHTTP('https://api.notion.com/v1')
        try:
            who, _ = http.json('GET', '/users/me', headers={
                'Authorization': 'Bearer ' + environment_secret(config['token_ref']), 'Notion-Version': API_VERSION})
            if who.get('object') != 'user':
                raise AdapterError('incompatible_schema')
            config.update(bot_id=native_id(who.get('id')), browse_accessible=True)
        finally:
            http.close()
    else:
        http = SourceHTTP(config.get('base_url', 'https://open.feishu.cn/open-apis'))
        try:
            who, _ = http.json('GET', '/authen/v1/user_info', headers={
                'Authorization': 'Bearer ' + environment_secret(config['access_token_ref'])})
            if who.get('code') != 0:
                raise AdapterError('auth_required')
            config.update(principal_id=identifier(who.get('data', {}).get('open_id')), browse_accessible=True)
        finally:
            http.close()
    return {**binding, 'config': config}
