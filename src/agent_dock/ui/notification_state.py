"""Presentation state machine for the floating panel (pure Python, no AppKit).

This is deliberately independent of the permission lifecycle: nothing here can
allow, deny or resolve a request. The AppKit layer feeds events in (with
monotonic timestamps) and renders ``view()``; it schedules one timer for
``next_deadline()`` and calls ``tick()`` when it fires.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Mode(enum.StrEnum):
    HIDDEN = "hidden"
    EXPANDED = "expanded"  # auto-expanded after an arrival
    COMPACT = "compact"  # one-line chip
    HOVER_EXPANDED = "hover_expanded"  # expanded because the pointer is over it
    PINNED = "pinned"  # user opened the inbox from the menu bar

    @property
    def transient(self) -> bool:
        return self in (Mode.EXPANDED, Mode.COMPACT, Mode.HOVER_EXPANDED)


@dataclass
class _Card:
    request_id: str
    arrived_at: float
    seq: int
    dismissed: bool = False


@dataclass(frozen=True)
class PresentationView:
    mode: Mode
    selected: str | None
    pending: tuple[str, ...]  # all actionable request ids, oldest first
    dismissed: frozenset[str]
    badge: int  # outstanding actionable requests (dismissed ones included)

    @property
    def others_waiting(self) -> int:
        return max(0, self.badge - (1 if self.selected else 0))


class PresentationController:
    def __init__(
        self,
        *,
        expanded_seconds: float = 10.0,
        visible_seconds: float = 30.0,
        hover_leave_seconds: float = 1.0,
        badge_only: bool = False,
    ):
        self.expanded_seconds = expanded_seconds
        self.visible_seconds = max(visible_seconds, expanded_seconds)
        self.hover_leave_seconds = hover_leave_seconds
        self.badge_only = badge_only

        self.mode = Mode.HIDDEN
        self.selected: str | None = None
        self._cards: dict[str, _Card] = {}
        self._seq = 0
        self._arrival_at: float | None = None  # arrival time of the presented card
        self._pointer_inside = False
        self._interaction_depth = 0
        self._leave_at: float | None = None
        self._deferred: str | None = None  # arrived while user was busy with another card

    # ---- queries -------------------------------------------------------------
    def view(self) -> PresentationView:
        pending = tuple(c.request_id for c in sorted(self._cards.values(), key=lambda c: c.seq))
        return PresentationView(
            mode=self.mode,
            selected=self.selected if self.mode is not Mode.HIDDEN else None,
            pending=pending,
            dismissed=frozenset(c.request_id for c in self._cards.values() if c.dismissed),
            badge=len(self._cards),
        )

    @property
    def busy(self) -> bool:
        return self._pointer_inside or self._interaction_depth > 0

    def next_deadline(self) -> float | None:
        if not self.mode.transient or self.busy:
            return None
        if self._leave_at is not None:
            return self._leave_at
        if self._arrival_at is None:
            return None
        if self.mode is Mode.EXPANDED:
            return self._arrival_at + self.expanded_seconds
        if self.mode is Mode.COMPACT:
            return self._arrival_at + self.visible_seconds
        return None

    # ---- request events (from broker) -----------------------------------------
    def request_added(self, request_id: str, now: float) -> None:
        if request_id in self._cards:
            return
        self._seq += 1
        self._cards[request_id] = _Card(request_id, now, self._seq)
        if self.mode is Mode.PINNED:
            if self.selected is None:
                self.selected = request_id
            return
        if self.mode.transient and self.busy:
            # Never replace content under the pointer or reset its timers.
            self._deferred = request_id
            return
        if self.mode is Mode.HOVER_EXPANDED and self._leave_at is not None:
            # Pointer just left; treat as busy until the leave delay ends.
            self._deferred = request_id
            return
        self._present(request_id, now)

    def request_removed(self, request_id: str, now: float) -> None:
        card = self._cards.pop(request_id, None)
        if card is None:
            return
        if self._deferred == request_id:
            self._deferred = None
        if self.selected != request_id:
            return
        if self.mode is Mode.PINNED:
            self.selected = self._oldest()
            return
        replacement = self._deferred or self._latest_undismissed()
        self._deferred = None
        if replacement is None:
            self._hide()
        else:
            # Keep the current visibility mode and timers; just swap content.
            self.selected = replacement

    # ---- pointer / interaction -------------------------------------------------
    def pointer_entered(self, now: float) -> None:
        self._pointer_inside = True
        self._leave_at = None
        if self.mode in (Mode.EXPANDED, Mode.COMPACT):
            self.mode = Mode.HOVER_EXPANDED

    def pointer_exited(self, now: float) -> None:
        self._pointer_inside = False
        if self.mode is Mode.HOVER_EXPANDED and self._interaction_depth == 0:
            self._leave_at = now + self.hover_leave_seconds

    def interaction_began(self, now: float) -> None:
        """A sub-menu, text field or confirmation is open: suspend auto-collapse/hide."""
        self._interaction_depth += 1
        self._leave_at = None

    def interaction_ended(self, now: float) -> None:
        self._interaction_depth = max(0, self._interaction_depth - 1)
        if self._interaction_depth == 0 and not self._pointer_inside:
            if self.mode is Mode.HOVER_EXPANDED:
                self._leave_at = now + self.hover_leave_seconds
            elif self.mode is Mode.EXPANDED:
                self.mode = Mode.HOVER_EXPANDED
                self._leave_at = now + self.hover_leave_seconds

    # ---- user commands (never decisions) ----------------------------------------
    def dismiss(self, now: float, request_id: str | None = None) -> None:
        rid = request_id or self.selected
        if rid in self._cards:
            self._cards[rid].dismissed = True  # type: ignore[index]
        if self.mode.transient:
            self._deferred = None
            self._hide()

    def menu_clicked(self, now: float) -> None:
        if self.mode is Mode.PINNED:
            self._hide()
            return
        self.mode = Mode.PINNED
        self._leave_at = None
        self._pointer_inside = False
        self._interaction_depth = 0
        if self.selected not in self._cards:
            self.selected = self._oldest()
        self._deferred = None

    def close(self, now: float) -> None:
        """Escape / click outside / close button: only ever hides the surface."""
        if self.mode is not Mode.HIDDEN:
            self._hide()

    def select(self, request_id: str, now: float) -> None:
        if request_id in self._cards:
            self.selected = request_id

    # ---- time -------------------------------------------------------------------
    def tick(self, now: float) -> None:
        if not self.mode.transient or self.busy:
            return
        if self._leave_at is not None:
            if now < self._leave_at:
                return
            self._leave_at = None
            if self._deferred is not None:
                rid, self._deferred = self._deferred, None
                self._present(rid, now)
                return
            assert self._arrival_at is not None
            if now >= self._arrival_at + self.visible_seconds:
                self._hide()
            elif now >= self._arrival_at + self.expanded_seconds:
                self.mode = Mode.COMPACT
            else:
                self.mode = Mode.EXPANDED
            return
        if self._arrival_at is None:
            return
        if (
            self.mode in (Mode.EXPANDED, Mode.COMPACT)
            and now >= self._arrival_at + self.visible_seconds
        ):
            self._hide()
        elif self.mode is Mode.EXPANDED and now >= self._arrival_at + self.expanded_seconds:
            self.mode = Mode.COMPACT

    # ---- internals --------------------------------------------------------------
    def _present(self, request_id: str, now: float) -> None:
        if self.badge_only:
            return
        self.selected = request_id
        self.mode = Mode.EXPANDED
        self._arrival_at = now
        self._leave_at = None

    def _hide(self) -> None:
        self.mode = Mode.HIDDEN
        self._leave_at = None
        self._pointer_inside = False
        self._interaction_depth = 0
        self._arrival_at = None

    def _oldest(self) -> str | None:
        cards = sorted(self._cards.values(), key=lambda c: c.seq)
        return cards[0].request_id if cards else None

    def _latest_undismissed(self) -> str | None:
        cards = [c for c in self._cards.values() if not c.dismissed]
        return max(cards, key=lambda c: c.seq).request_id if cards else None
