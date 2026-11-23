"""Claude Code PermissionRequest adapter.

Input/output contract: https://code.claude.com/docs/en/hooks (PermissionRequest).
"""

from __future__ import annotations

from typing import Any

from ..core.models import Action, Decision, Envelope, PermissionSuggestion
from ..core.text import one_line
from ..settings import Settings
from .base import (
    DEFAULT_DENY_MESSAGE,
    Preview,
    build_envelope,
    generic_preview,
    hook_output,
    pretty,
    shrink,
)

DESTINATIONS = {
    "session": "this session only",
    "localSettings": "this project (.claude/settings.local.json)",
    "projectSettings": "this project, shared (.claude/settings.json)",
    "userSettings": "all projects (~/.claude/settings.json)",
}

# Tools whose "allow" must echo the full tool input (per Claude docs).
ECHO_INPUT_TOOLS = {"ExitPlanMode"}


def _rule_text(rule: dict[str, Any]) -> str:
    tool = str(rule.get("toolName", "?"))
    content = rule.get("ruleContent")
    return f"{tool}({content})" if isinstance(content, str) and content else f"{tool} (any use)"


def describe_suggestion(entry: Any) -> str | None:
    """Human label for a native permission update, or None to hide it."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    dest = DESTINATIONS.get(str(entry.get("destination")), str(entry.get("destination")))
    if kind == "addRules" and entry.get("behavior") == "allow":
        rules = entry.get("rules")
        if not isinstance(rules, list) or not rules:
            return None
        text = ", ".join(_rule_text(r) for r in rules if isinstance(r, dict))
        return one_line(f"Always allow {text} — {dest}", 200)
    if kind == "addDirectories":
        dirs = entry.get("directories")
        if not isinstance(dirs, list) or not dirs:
            return None
        return one_line(f"Allow access to {', '.join(map(str, dirs))} — {dest}", 200)
    if kind == "setMode":
        mode = entry.get("mode")
        if mode in (None, "bypassPermissions", "dontAsk"):
            return None  # never offer blanket elevation from the dock
        return one_line(f"Allow and switch to {mode} mode — {dest}", 200)
    return None  # replaceRules/removeRules/deny rules: not offered


def _preview(tool: str, ti: Any) -> Preview:
    t = ti if isinstance(ti, dict) else {}
    if tool == "Bash" and isinstance(t.get("command"), str):
        desc = t.get("description")
        detail = t["command"] + (f"\n\n# {desc}" if isinstance(desc, str) and desc else "")
        return Preview(summary=t["command"], detail=detail)
    if tool in ("Edit", "MultiEdit") and isinstance(t.get("file_path"), str):
        edits = t.get("edits") if tool == "MultiEdit" else [t]
        chunks = []
        for e in edits if isinstance(edits, list) else []:
            if not isinstance(e, dict):
                continue
            old = str(e.get("old_string", ""))
            new = str(e.get("new_string", ""))
            chunks.append(
                "\n".join(
                    ["- " + ln for ln in old.splitlines()] + ["+ " + ln for ln in new.splitlines()]
                )
            )
        return Preview(summary=f"Edit {t['file_path']}", detail="\n…\n".join(chunks))
    if tool == "Write" and isinstance(t.get("file_path"), str):
        return Preview(summary=f"Write {t['file_path']}", detail=str(t.get("content", ""))[:20000])
    if tool == "WebFetch" and isinstance(t.get("url"), str):
        return Preview(summary=f"Fetch {t['url']}", detail=pretty(shrink(t)))
    if tool == "ExitPlanMode":
        plan = t.get("plan")
        return Preview(summary="Approve plan", detail=str(plan) if plan else pretty(shrink(t)))
    if tool.startswith("mcp__"):
        return Preview(
            summary=tool.replace("mcp__", "", 1).replace("__", " · "), detail=pretty(shrink(t))
        )
    return generic_preview(tool, t)


class ClaudeAdapter:
    name = "claude"

    def parse(self, data: dict[str, Any], settings: Settings) -> Envelope | None:
        if data.get("hook_event_name") not in (None, "PermissionRequest"):
            return None
        tool = data.get("tool_name")
        if not isinstance(tool, str) or tool in settings.passthrough_tools:
            return None
        suggestions: list[PermissionSuggestion] = []
        raw = data.get("permission_suggestions")
        if isinstance(raw, list):
            for i, entry in enumerate(raw[:20]):
                label = describe_suggestion(entry)
                if label:
                    suggestions.append(
                        PermissionSuggestion(
                            index=i, label=label, destination=str(entry.get("destination"))
                        )
                    )
        return build_envelope(
            agent="claude",
            data=data,
            preview=_preview(tool, data.get("tool_input")),
            settings=settings,
            suggestions=suggestions,
            extra_actions=[Action.ALLOW_ALWAYS] if suggestions else None,
        )

    def format(self, decision: Decision, data: dict[str, Any]) -> dict[str, Any] | None:
        tool = data.get("tool_name")
        echo = data.get("tool_input") if tool in ECHO_INPUT_TOOLS else None
        if decision.action is Action.ALLOW_ONCE:
            return hook_output("allow", updatedInput=echo)
        if decision.action is Action.ALLOW_ALWAYS:
            raw = data.get("permission_suggestions")
            idx = decision.suggestion_index
            # Only ever echo the agent's own suggestion, re-validated from the
            # original stdin, never anything constructed by the broker or UI.
            if (
                not isinstance(raw, list)
                or not isinstance(idx, int)
                or not 0 <= idx < len(raw)
                or describe_suggestion(raw[idx]) is None
            ):
                return None  # fall back to the native prompt
            return hook_output("allow", updatedInput=echo, updatedPermissions=[raw[idx]])
        if decision.action is Action.DENY:
            return hook_output("deny", message=decision.message or DEFAULT_DENY_MESSAGE)
        return None
