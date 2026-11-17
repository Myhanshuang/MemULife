"""Explicit factories: configuration never imports arbitrary Python modules."""

from ..contracts import validator
from .base import AdapterError
from ..sources import get_source, sources

PROFILES = {source.adapter_id: source.profiles for source in sources()}


def adapter_class(adapter_id: str, profile: str):
    if profile not in PROFILES.get(adapter_id, ()):
        raise AdapterError("source_profile_unknown")
    return get_source(adapter_id).reader_class(profile)


def create(adapter_id: str, profile: str, config: dict):
    from jsonschema import Draft202012Validator

    implementation = adapter_class(adapter_id, profile)
    get_source(adapter_id).validate_profile_config(profile, config)
    if list(Draft202012Validator(implementation.config_schema).iter_errors(config)):
        raise AdapterError("config_invalid")
    return implementation(config)


def config_schema(adapter_id: str) -> dict:
    return get_source(adapter_id).config_schema()


def manifest(adapter_id: str) -> dict:
    if adapter_id not in PROFILES:
        raise AdapterError("adapter_unknown")
    source = get_source(adapter_id)
    browser = source.import_driver == "push"
    result = {
        "adapter_id": adapter_id,
        "version": "0.2.0",
        "license": "project-license-unspecified",
        "execution_location": "browser" if browser else "edge",
        "supported_source_profiles": list(PROFILES[adapter_id]),
        "config_schema": config_schema(adapter_id),
        "credential_schema": {
            "type": "object",
            "properties": {"token_env": {"type": "string"}},
            "additionalProperties": False,
        },
        "capabilities": {
            name: {
                "status": "conditional",
                "conditions": [
                    "explicit scope configuration",
                    "successful runtime profile probe",
                ],
            }
            for name in ("discover", "backfill", "poll", "reconcile", "attachments")
        },
        "rate_limits": {"requests_per_second": 1, "burst": 1, "concurrency": 1},
        "event_types": list(source.event_types),
        "fixture_set_version": "adapter-fixtures-v1",
    }
    result["capabilities"]["read_only"] = {"status": "supported", "conditions": []}
    if source.import_driver == "files":
        result["capabilities"]["backfill"] = {"status": "unsupported", "conditions": []}
    result["capabilities"]["subscribe"] = {
        "status": "conditional" if source.subscribes else "unsupported",
        "conditions": ["configured native bridge or event transport"]
        if source.subscribes
        else [],
    }
    validator("adapter-manifest.schema.json").validate(result)
    return result
