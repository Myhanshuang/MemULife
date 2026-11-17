"""Consume an exported C# portable fixture through the real Python spool reader.

Run the core fixture with MEMULIFE_FIXTURE_MANIFEST=/tmp/native-fixture.json,
then run this script with that path using the repository Python environment.
The media is deliberately a container fixture; this does not qualify a codec.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

from memu.acquisition.adapters.screen import ScreenNativeAdapter
from memu.acquisition.contracts import validator


def verify(path: Path) -> None:
    raw = path.read_bytes()
    item = json.loads(raw)
    media = path.with_suffix(path.suffix + ".media").read_bytes()
    with tempfile.TemporaryDirectory(prefix="memulife-native-reader-") as temporary:
        root = Path(temporary)
        video = root / item["relative_path"]
        video.parent.mkdir(parents=True)
        video.write_bytes(media)
        manifest = (
            root / "manifests" / item["session_id"] / (item["segment_id"] + ".json")
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(raw)
        adapter = ScreenNativeAdapter(
            {
                "root": str(root),
                **{key: item[key] for key in ("device_id", "stream_id", "backend")},
            }
        )
        page = adapter.read("discover")
        assert len(page.records) == 1
        record = page.records[0]
        assert record.raw == raw
        assert record.revision == "sealed-v1"
        assert record.attachments[0].data == media
        assert hashlib.sha256(media).hexdigest() == item["sha256"]
        payload = record.metadata["screen_payload"]
        definitions = validator("screen-payloads.schema.json").schema["$defs"]
        Draft202012Validator(
            {"$ref": "#/$defs/VideoChunk", "$defs": definitions},
            format_checker=FormatChecker(),
        ).validate(payload)
        coverage = record.metadata["screen_coverage"]
        assert coverage["status"] == "partial"
        assert "frame_index_unavailable" in coverage["notes"]
        assert not adapter.read("live", page.cursor).records
    print(
        "Native C# fixture accepted by ScreenNativeAdapter and VideoChunk schema; replay is idempotent"
    )


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
