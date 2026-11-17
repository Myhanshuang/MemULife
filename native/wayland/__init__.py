"""Explicitly started XDG Portal / PipeWire capture helper; no import side effects."""


class CaptureError(Exception):
    """Stable diagnostics; never include desktop metadata or restore tokens."""

    def __init__(self, code, *, stage=None):
        self.code = code
        self.stage = stage
        super().__init__(code)
