from .base import Adapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, Adapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}

__all__ = ["ADAPTERS", "Adapter", "ClaudeAdapter", "CodexAdapter"]
