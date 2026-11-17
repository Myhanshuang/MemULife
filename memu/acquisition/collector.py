"""Source discovery/selection and local readers, independent of memory storage."""
from jsonschema import Draft202012Validator

from .adapters.base import AdapterError
from .sources import get_source
from .sources.selection import Selection
from .sources.reader import SelectedReader


def validate_binding(binding):
    module = get_source(binding['adapter_id'])
    if binding['source_profile'] not in module.profiles:
        raise AdapterError('source_profile_unknown')
    if not Draft202012Validator(module.config_schema()).is_valid(binding.get('config')):
        raise AdapterError('config_invalid')
    module.validate_profile_config(binding['source_profile'], binding['config'])
    return module


def prepare(binding, value):
    module = validate_binding(binding)
    selection = value if isinstance(value, Selection) else Selection.model_validate(value)
    module.validate(binding, selection)
    return module.prepare_plan(binding, selection)


class SourceReader:
    def __init__(self, binding, selection):
        self.binding = binding
        self.module = validate_binding(binding)
        self.selection = selection if isinstance(selection, Selection) else Selection.model_validate(selection)
        self._reader = None
        self._validated = False

    def read(self, mode, cursor=None):
        if not self._validated or self.module.adapter_id != 'ai.local-logs':
            self.module.validate(self.binding, self.selection)
            self._validated = True
        self.module.verify_plan(self.binding, self.selection)
        if self._reader is not None:
            reader = self._reader
        elif self.module.import_driver == 'files':
            from .files import FileReader
            reader = FileReader(self.binding, self.selection)
        elif self.module.import_driver == 'reader':
            reader = SelectedReader(self.binding, self.selection)
        else:
            raise AdapterError('import_push_required')
        self._reader = reader
        page = reader.read(mode, cursor, limit=min(self.module.page_limit, 100))
        # A plan's source/ancestor binding must still hold after a slow upstream read.
        self.module.verify_plan(self.binding, self.selection)
        return page

    def close(self):
        if self._reader is not None and hasattr(self._reader, 'close'):
            self._reader.close()
