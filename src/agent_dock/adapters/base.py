"""Shared adapter plumbing: agent-native hook JSON <-> Agent Dock envelope."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..core.models import Action, Decision, Envelope, PermissionSuggestion
from ..core.text import one_line, sanitize, truncate
from ..settings import Settings

DETAIL_LIMIT = 16 * 1024
STRING_LIMIT = 2048


def shrink(value: Any, *, depth: int = 0) -> Any:
    """Bound the size of an arbitrary JSON value for display/transport."""
    if depth > 4:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= STRING_LIMIT else value[:STRING_LIMIT] + "…"
    if isinstance(value, list):
        out = [shrink(v, depth=depth + 1) for v in value[:50]]
        if len(value) > 50:
            out.append(f"… {len(value) - 50} more")
        return out
    if isinstance(value, dict):
        items = list(value.items())[:50]
        return {str(k)[:200]: shrink(v, depth=depth + 1) for k, v in items}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:STRING_LIMIT]


def pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class Preview:
    summary: str
    detail: str


class Adapter(Protocol):
    name: str

    def parse(self, data: dict[str, Any], settings: Settings) -> Envelope | None: ...

    def format(self, decision: Decision, data: dict[str, Any]) -> dict[str, Any] | None: ...


def build_envelope(
    *,
    agent: str,
    data: dict[str, Any],
    preview: Preview,
    settings: Settings,
    suggestions: list[PermissionSuggestion] | None = None,
    extra_actions: list[Action] | None = None,
) -> Envelope:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {} if tool_input is None else {"value": tool_input}
    detail, cut = truncate(sanitize(preview.detail), DETAIL_LIMIT)
    actions = [Action.ALLOW_ONCE, Action.DENY, Action.FALLBACK] + list(extra_actions or [])

    def opt_str(key: str) -> str | None:
        v = data.get(key)
        return v if isinstance(v, str) and v else None

    return Envelope(
        agent=agent,  # type: ignore[arg-type]
        session_id=opt_str("session_id") or "unknown",
        tool_name=opt_str("tool_name") or "unknown",
        tool_input=shrink(tool_input),
        cwd=opt_str("cwd"),
        transcript_path=opt_str("transcript_path"),
        summary=one_line(preview.summary, 300),
        detail=detail,
        detail_truncated=cut,
        suggestions=list(suggestions or []),
        supported_actions=[str(a) for a in actions],
        deadline_ms=int(settings.request_timeout_seconds * 1000),
        hook_pid=os.getpid(),
        terminal_program=os.environ.get("TERM_PROGRAM") or None,
        permission_mode=opt_str("permission_mode"),
        timestamp=time.time(),
    )


def generic_preview(tool_name: str, tool_input: Any) -> Preview:
    ti = tool_input if isinstance(tool_input, dict) else {}
    cmd = ti.get("command")
    if isinstance(cmd, str):
        return Preview(summary=cmd, detail=cmd)
    if isinstance(cmd, list) and all(isinstance(c, str) for c in cmd):
        joined = " ".join(cmd)
        return Preview(summary=joined, detail=joined)
    for key in ("file_path", "path", "url", "pattern", "query"):
        v = ti.get(key)
        if isinstance(v, str):
            return Preview(summary=v, detail=pretty(shrink(ti)))
    return Preview(summary=tool_name, detail=pretty(shrink(tool_input)))


def hook_output(behavior: str, **fields: Any) -> dict[str, Any]:
    decision: dict[str, Any] = {"behavior": behavior}
    decision.update({k: v for k, v in fields.items() if v is not None})
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


DEFAULT_DENY_MESSAGE = "The user denied this request from Agent Dock."
