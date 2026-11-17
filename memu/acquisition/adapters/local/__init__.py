"""Read-only profiles for explicitly selected local AI application data."""

from .logs import ClaudeCodeAdapter, CodexAdapter, GeminiCLIAdapter

__all__ = ["ClaudeCodeAdapter", "CodexAdapter", "GeminiCLIAdapter"]
from .sqlite import CursorAdapter, OpenCodeAdapter

__all__ += ["CursorAdapter", "OpenCodeAdapter"]
