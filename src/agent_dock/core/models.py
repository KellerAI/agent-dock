"""Domain models shared by the hook client, broker and UI.

The broker envelope is Agent Dock's own internal schema. Agent-native JSON is
only produced/consumed inside ``agent_dock.adapters``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from .. import PROTOCOL_VERSION

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[str, ...] = ("claude", "codex")


class Action(enum.StrEnum):
    ALLOW_ONCE = "allow_once"
    DENY = "deny"
    # Persist one of the agent's *own* suggestions (Claude only). The decision
    # carries an index into the suggestions the hook received; the hook re-reads
    # its local copy, so the broker can never mint a rule.
    ALLOW_ALWAYS = "allow_always"
    # Explicitly hand the request back to the agent's native prompt.
    FALLBACK = "fallback"


class RequestState(enum.StrEnum):
    RECEIVED = "received"
    PENDING = "pending"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FALLBACK = "fallback"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def actionable(self) -> bool:
        """Counts toward the badge: the agent is still blocked on the dock."""
        return self in (RequestState.PENDING, RequestState.CLAIMED)


TERMINAL_STATES = frozenset(
    {RequestState.RESOLVED, RequestState.EXPIRED, RequestState.CANCELLED, RequestState.FALLBACK}
)

# Allowed transitions of the permission lifecycle (broker-owned).
TRANSITIONS: dict[RequestState, frozenset[RequestState]] = {
    RequestState.RECEIVED: frozenset(
        {RequestState.PENDING, RequestState.FALLBACK, RequestState.CANCELLED}
    ),
    RequestState.PENDING: frozenset(
        {
            RequestState.CLAIMED,
            RequestState.EXPIRED,
            RequestState.CANCELLED,
            RequestState.FALLBACK,
        }
    ),
    # CLAIMED -> FALLBACK when the user explicitly chose "answer in terminal".
    RequestState.CLAIMED: frozenset(
        {RequestState.RESOLVED, RequestState.CANCELLED, RequestState.FALLBACK}
    ),
}


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class PermissionSuggestion:
    """A persistent-permission option exactly as the agent offered it."""

    index: int
    label: str  # human-readable rule preview, e.g. "Bash(npm test) → project (local)"
    destination: str  # session | localSettings | projectSettings | userSettings

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "label": self.label, "destination": self.destination}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionSuggestion:
        return cls(index=int(d["index"]), label=str(d["label"]), destination=str(d["destination"]))


@dataclass
class Envelope:
    """Request sent from a hook subprocess to the broker."""

    agent: Provider
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    cwd: str | None = None
    transcript_path: str | None = None
    summary: str = ""  # one-line action description, e.g. the command
    detail: str = ""  # longer preview: command, diff, plan text (already truncated)
    detail_truncated: bool = False
    suggestions: list[PermissionSuggestion] = field(default_factory=list)
    supported_actions: list[str] = field(
        default_factory=lambda: [Action.ALLOW_ONCE, Action.DENY, Action.FALLBACK]
    )
    deadline_ms: int = 45_000
    hook_pid: int = 0
    terminal_program: str | None = None  # $TERM_PROGRAM of the agent, for "show terminal"
    permission_mode: str | None = None
    timestamp: float = 0.0  # wall clock, display only
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "agent": self.agent,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "summary": self.summary,
            "detail": self.detail,
            "detail_truncated": self.detail_truncated,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "supported_actions": [str(a) for a in self.supported_actions],
            "deadline_ms": self.deadline_ms,
            "hook_pid": self.hook_pid,
            "terminal_program": self.terminal_program,
            "permission_mode": self.permission_mode,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Envelope:
        """Validate untrusted input. Raises ValueError on anything malformed."""
        if not isinstance(d, dict):
            raise ValueError("envelope must be an object")
        if d.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version {d.get('protocol_version')!r}")
        agent = d.get("agent")
        if agent not in PROVIDERS:
            raise ValueError(f"unknown agent {agent!r}")
        rid = d.get("request_id")
        try:
            uuid.UUID(str(rid))
        except ValueError:
            raise ValueError("request_id must be a UUID") from None

        def s(key: str, *, required: bool = False, limit: int = 4096) -> str | None:
            v = d.get(key)
            if v is None:
                if required:
                    raise ValueError(f"missing {key}")
                return None
            if not isinstance(v, str):
                raise ValueError(f"{key} must be a string")
            return v[:limit]

        tool_input = d.get("tool_input")
        if tool_input is None:
            tool_input = {}
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input must be an object")
        actions = d.get("supported_actions") or []
        if not isinstance(actions, list):
            raise ValueError("supported_actions must be a list")
        parsed_actions: list[str] = []
        for a in actions:
            try:
                parsed_actions.append(Action(a))
            except ValueError:
                continue  # ignore unknown future actions
        sugg_raw = d.get("suggestions") or []
        if not isinstance(sugg_raw, list):
            raise ValueError("suggestions must be a list")
        suggestions = [PermissionSuggestion.from_dict(x) for x in sugg_raw[:20]]
        deadline = d.get("deadline_ms", 45_000)
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
            raise ValueError("deadline_ms must be a positive integer")
        hook_pid = d.get("hook_pid", 0)
        if not isinstance(hook_pid, int) or isinstance(hook_pid, bool):
            raise ValueError("hook_pid must be an integer")
        ts = d.get("timestamp", 0.0)
        return cls(
            protocol_version=PROTOCOL_VERSION,
            request_id=str(rid),
            agent=agent,  # type: ignore[arg-type]
            session_id=s("session_id", required=True, limit=256) or "",
            tool_name=s("tool_name", required=True, limit=256) or "",
            tool_input=tool_input,
            cwd=s("cwd"),
            transcript_path=s("transcript_path"),
            summary=s("summary", limit=1024) or "",
            detail=s("detail", limit=64 * 1024) or "",
            detail_truncated=bool(d.get("detail_truncated", False)),
            suggestions=suggestions,
            supported_actions=parsed_actions,
            deadline_ms=deadline,
            hook_pid=hook_pid,
            terminal_program=s("terminal_program", limit=128),
            permission_mode=s("permission_mode", limit=64),
            timestamp=float(ts) if isinstance(ts, (int, float)) else 0.0,
        )


@dataclass(frozen=True)
class Decision:
    request_id: str
    action: Action
    message: str | None = None  # deny feedback for the agent
    suggestion_index: int | None = None  # for ALLOW_ALWAYS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"request_id": self.request_id, "decision": str(self.action)}
        if self.message:
            d["message"] = self.message
        if self.suggestion_index is not None:
            d["suggestion_index"] = self.suggestion_index
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        if not isinstance(d, dict):
            raise ValueError("decision must be an object")
        action = Action(d.get("decision"))
        msg = d.get("message")
        if msg is not None and not isinstance(msg, str):
            raise ValueError("message must be a string")
        idx = d.get("suggestion_index")
        if idx is not None and (not isinstance(idx, int) or isinstance(idx, bool)):
            raise ValueError("suggestion_index must be an integer")
        if action is Action.ALLOW_ALWAYS and idx is None:
            raise ValueError("allow_always requires suggestion_index")
        return cls(
            request_id=str(d.get("request_id")),
            action=action,
            message=msg[:2000] if msg else None,
            suggestion_index=idx,
        )


@dataclass
class SessionIdentity:
    provider: Provider
    session_id: str  # routing identity; never the primary display title
    display_title: str | None = None  # mutable presentation metadata
    title_source: str = "fallback"  # hook | index | transcript | generated | cache | fallback
    title_updated_at: float | None = None
    project_path: str | None = None
