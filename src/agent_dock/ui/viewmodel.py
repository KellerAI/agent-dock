"""Display text for cards, chips and the status item (pure Python, unit-tested).

The AppKit layer only lays these strings out; all wording decisions live here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..core.models import Action, Envelope
from ..core.text import one_line

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}

# $TERM_PROGRAM -> bundle identifier, for "Open terminal".
TERMINAL_BUNDLES = {
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "vscode": "com.microsoft.VSCode",
    "WarpTerminal": "dev.warp.Warp-Stable",
    "ghostty": "com.mitchellh.ghostty",
    "WezTerm": "com.github.wez.wezterm",
    "Hyper": "co.zeit.hyper",
    "Tabby": "org.tabby",
    "kitty": "net.kovidgoyal.kitty",
    "alacritty": "org.alacritty",
    "zed": "dev.zed.Zed",
}

DETAIL_LINES_TRANSIENT = 8
DETAIL_LINES_INBOX = 14


@dataclass(frozen=True)
class SuggestionVM:
    index: int
    label: str


@dataclass(frozen=True)
class CardVM:
    request_id: str
    title: str
    subtitle: str
    summary: str
    detail: str
    detail_truncated: bool
    can_allow_always: bool
    suggestions: tuple[SuggestionVM, ...]
    terminal_bundle: str | None
    cwd: str | None
    dismissed: bool

    @property
    def accessibility_label(self) -> str:
        return f"Permission request from {self.subtitle}: {self.title}. {self.summary}"


def project_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = os.path.basename(os.path.normpath(cwd))
    return name or cwd


def card(
    env: Envelope,
    *,
    title: str | None,
    dismissed: bool = False,
    reveal_detail: bool = True,
) -> CardVM:
    agent = AGENT_LABELS.get(env.agent, env.agent)
    proj = project_name(env.cwd)
    subtitle = " · ".join(p for p in (agent, proj, env.tool_name) if p)
    detail = env.detail or env.summary
    if not reveal_detail:
        detail = "•••••• hidden (sensitive mode) — hover to reveal"
    sugg = tuple(SuggestionVM(s.index, s.label) for s in env.suggestions)
    return CardVM(
        request_id=env.request_id,
        title=one_line(title or f"{proj or agent} · {env.tool_name} approval", 90),
        subtitle=one_line(subtitle, 120),
        summary=one_line(env.summary or env.tool_name, 200),
        detail=detail,
        detail_truncated=env.detail_truncated,
        can_allow_always=str(Action.ALLOW_ALWAYS) in env.supported_actions and bool(sugg),
        suggestions=sugg,
        terminal_bundle=TERMINAL_BUNDLES.get(env.terminal_program or ""),
        cwd=env.cwd,
        dismissed=dismissed,
    )


def compact_line(card_vm: CardVM | None, badge: int) -> str:
    if card_vm is None:
        return f"{badge} waiting" if badge else ""
    prefix = f"{badge} · " if badge > 1 else ""
    return one_line(f"{prefix}{card_vm.title} · needs approval", 80)


def others_waiting_text(n: int) -> str:
    if n <= 0:
        return ""
    return f"{n} more waiting"


def badge_text(n: int) -> str:
    if n <= 0:
        return ""
    return "99+" if n > 99 else str(n)


def status_accessibility(n: int) -> str:
    if n == 0:
        return "Agent Dock: no pending permission requests"
    noun = "request" if n == 1 else "requests"
    return f"Agent Dock: {n} pending permission {noun}"


def inbox_header(n: int) -> str:
    return "No pending requests" if n == 0 else f"{n} waiting for approval"


DELIVERY_ERRORS = {
    "unknown": "This request is no longer pending (it was answered, expired or the agent stopped).",
    "not_pending": "This request is already being answered.",
    "unsupported": "That option isn't supported for this agent.",
    "bad_suggestion": "That rule is no longer offered by the agent.",
    "delivery_failed": "The agent stopped waiting before the answer arrived.",
}


def delivery_error(reason: str) -> str:
    return DELIVERY_ERRORS.get(reason, f"Could not deliver the answer ({reason}).")
