"""Bundled acquisition contracts; no dependency on the MemULife checkout."""
from functools import lru_cache
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker


@lru_cache
def validator(name):
    if name not in {'adapter-manifest.schema.json', 'screen-payloads.schema.json'}:
        raise ValueError('Unknown acquisition contract')
    return Draft202012Validator(json.loads((Path(__file__).parent / 'spec' / name).read_text()),
                               format_checker=FormatChecker())
