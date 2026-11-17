"""Configuration forms and private credentials for owner-selected sources."""
from copy import deepcopy
from contextlib import contextmanager
import os

from ..persistence import private_save
from .adapters.base import AdapterError
from .sources import sources


# Only these adapter fields may create a managed environment credential.
SECRET_FIELDS = {
    'qq.napcat': {'token_env': 'NapCat 访问令牌', 'webui_token_ref': 'NapCat WebUI 登录密码'},
    'feishu.documents': {'access_token_ref': '飞书用户访问令牌', 'oauth.client_id_ref': '应用 ID',
                         'oauth.client_secret_ref': '应用密钥'},
    'notion.workspace': {'token_ref': 'Notion 集成密钥'},
    'immich.assets': {'api_key_ref': 'Immich API 密钥'},
}


def setup_catalog():
    result = []
    for module in sources():
        profiles = []
        for profile in module.profiles:
            if module.adapter_id == 'qq.napcat' and profile == 'onebot11-napcat':
                continue  # Existing HTTP connections remain supported.
            if module.adapter_id == 'immich.assets' and profile == 'installed-openapi':
                continue  # Existing bindings remain readable; new connections use URL/key.
            schema = (module.config_schema() if module.adapter_id in {'filesystem.typora', 'browser.ai-history'}
                      else module.reader_class(profile).config_schema)
            if module.adapter_id == 'immich.assets':
                from .adapters.immich_auto import SETUP_SCHEMA
                schema = SETUP_SCHEMA
            if module.adapter_id in {'qq.napcat', 'notion.workspace', 'feishu.documents'}:
                from .connection_setup import setup_schema
                schema = setup_schema(module.adapter_id, schema, profile)
            profiles.append({'id': profile, 'schema': deepcopy(schema)})
        result.append({'adapter_id': module.adapter_id, **module.profile(), 'profiles': profiles,
                       'secret_fields': SECRET_FIELDS.get(module.adapter_id, {})})
    return {'adapters': result}


class SourceCredentials:
    def __init__(self, root):
        import json
        self.path = root / 'credentials.json'
        self.values = json.loads(self.path.read_bytes()) if self.path.exists() else {}
        self.installed = {}
        self.previous = {}

    @contextmanager
    def temporary(self, values):
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def prepare(self, source_id, binding, credentials):
        if not isinstance(credentials, dict):
            raise AdapterError('source_credentials_invalid')
        allowed = SECRET_FIELDS.get(binding['adapter_id'], {})
        config = deepcopy(binding['config'])
        values = {}
        for field, secret in credentials.items():
            if field not in allowed or not isinstance(secret, str) or not 1 <= len(secret) <= 16384 or any(c in secret for c in '\r\n\x00'):
                raise AdapterError('source_credentials_invalid')
            target = config
            parts = field.split('.')
            for part in parts[:-1]:
                target = target.setdefault(part, {})
                if not isinstance(target, dict):
                    raise AdapterError('source_credentials_invalid')
            name = 'MEMU_SOURCE_' + source_id.replace('-', '').upper() + '_' + field.replace('.', '_').upper()
            target[parts[-1]] = name if field == 'token_env' else 'env:' + name
            values[name] = secret
        return {**binding, 'config': config}, values

    def install(self, values):
        for name, value in values.items():
            if not name.startswith('MEMU_SOURCE_') or not isinstance(value, str):
                raise AdapterError('source_credentials_invalid')
            if name not in self.previous:
                self.previous[name] = os.environ.get(name)
            os.environ[name] = value
            self.installed[name] = value

    def save(self, source_id, values):
        if values:
            self.values[source_id] = values
            private_save(self.path, self.values)
            self.install(values)

    def close(self):
        for name, value in self.installed.items():
            if os.environ.get(name) == value:
                if self.previous[name] is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = self.previous[name]

    def remove(self, source_id):
        values = self.values.get(source_id, {})
        if not values:
            return
        remaining = {key: value for key, value in self.values.items() if key != source_id}
        private_save(self.path, remaining)
        self.values = remaining
        for name, value in values.items():
            if os.environ.get(name) == value:
                previous = self.previous.get(name)
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
            self.installed.pop(name, None)
            self.previous.pop(name, None)
