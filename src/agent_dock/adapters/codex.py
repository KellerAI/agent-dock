"""Codex PermissionRequest adapter.

Codex supports only allow/deny(+message) on this hook; updatedInput,
updatedPermissions and interrupt fail closed, so they are never emitted.
"""

from __future__ import annotations

from typing import Any

from ..core.models import Action, Decision, Envelope
from ..settings import Settings
from .base import DEFAULT_DENY_MESSAGE, Preview, build_envelope, generic_preview, hook_output


def _preview(tool: str, ti: Any) -> Preview:
    base = generic_preview(tool, ti)
    t = ti if isinstance(ti, dict) else {}
    desc = t.get("description")
    if isinstance(desc, str) and desc:
        base.detail = f"{base.detail}\n\n# {desc}"
    if tool == "apply_patch":
        base.summary = "Apply patch"
    return base


class CodexAdapter:
    name = "codex"

    def parse(self, data: dict[str, Any], settings: Settings) -> Envelope | None:
        if data.get("hook_event_name") not in (None, "PermissionRequest"):
            return None
        tool = data.get("tool_name")
        if not isinstance(tool, str) or tool in settings.passthrough_tools:
            return None
        return build_envelope(
            agent="codex",
            data=data,
            preview=_preview(tool, data.get("tool_input")),
            settings=settings,
        )

    def format(self, decision: Decision, data: dict[str, Any]) -> dict[str, Any] | None:
        if decision.action is Action.ALLOW_ONCE:
            return hook_output("allow")
        if decision.action is Action.DENY:
            return hook_output("deny", message=decision.message or DEFAULT_DENY_MESSAGE)
        return None  # ALLOW_ALWAYS is never offered for Codex; FALLBACK -> native prompt
