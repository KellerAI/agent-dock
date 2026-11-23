"""Single source of truth for permission-request state.

All methods run on the broker's asyncio loop thread. Because a claim checks and
sets state with no ``await`` in between, two decisions for the same request can
never both succeed.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.models import (
    TRANSITIONS,
    Action,
    Decision,
    Envelope,
    InvalidTransition,
    RequestState,
)

log = logging.getLogger(__name__)

# Delivers a message to the waiting hook. Returns False if it could not be sent.
Sender = Callable[[dict[str, Any]], Awaitable[bool]]


class RegistryError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RequestRecord:
    envelope: Envelope
    seq: int
    received_at: float  # monotonic
    deadline_at: float  # monotonic
    send: Sender = field(repr=False)
    state: RequestState = RequestState.RECEIVED
    on_terminal: list[Callable[[], None]] = field(default_factory=list, repr=False)

    @property
    def request_id(self) -> str:
        return self.envelope.request_id

    def transition(self, new: RequestState) -> None:
        allowed = TRANSITIONS.get(self.state, frozenset())
        if new not in allowed:
            raise InvalidTransition(f"{self.state} -> {new}")
        self.state = new
        if new.terminal:
            callbacks, self.on_terminal = self.on_terminal, []
            for cb in callbacks:
                try:
                    cb()
                except Exception:  # pragma: no cover - defensive
                    log.exception("terminal callback failed")


# ---- events published to observers (UI) ------------------------------------


@dataclass(frozen=True)
class RequestAdded:
    request_id: str
    envelope: Envelope
    seq: int
    received_at: float
    deadline_at: float


@dataclass(frozen=True)
class RequestRemoved:
    request_id: str
    state: RequestState


@dataclass(frozen=True)
class DecisionResult:
    ok: bool
    request_id: str
    reason: str = ""  # unknown | not_pending | unsupported | bad_suggestion | delivery_failed
    state: RequestState | None = None


Observer = Callable[[object], None]


class Registry:
    def __init__(
        self,
        *,
        broker_instance: str,
        max_queue: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.broker_instance = broker_instance
        self.max_queue = max_queue
        self._clock = clock
        self._records: dict[str, RequestRecord] = {}
        self._seen_ids: set[str] = set()
        self._seq = itertools.count(1)
        self._observers: list[Observer] = []

    # -- observers --
    def add_observer(self, cb: Observer) -> None:
        self._observers.append(cb)

    def remove_observer(self, cb: Observer) -> None:
        if cb in self._observers:
            self._observers.remove(cb)

    def _emit(self, event: object) -> None:
        for cb in list(self._observers):
            try:
                cb(event)
            except Exception:
                log.exception("observer failed for %s", type(event).__name__)

    # -- queries --
    def get(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    def actionable(self) -> list[RequestRecord]:
        """Requests still blocking an agent, oldest first."""
        return sorted(
            (r for r in self._records.values() if r.state.actionable), key=lambda r: r.seq
        )

    @property
    def pending_count(self) -> int:
        return sum(1 for r in self._records.values() if r.state.actionable)

    # -- lifecycle --
    def add(self, envelope: Envelope, send: Sender) -> RequestRecord:
        rid = envelope.request_id
        if rid in self._seen_ids:
            raise RegistryError("duplicate_request")
        if self.pending_count >= self.max_queue:
            raise RegistryError("queue_full")
        now = self._clock()
        rec = RequestRecord(
            envelope=envelope,
            seq=next(self._seq),
            received_at=now,
            deadline_at=now + envelope.deadline_ms / 1000.0,
            send=send,
        )
        self._seen_ids.add(rid)
        self._records[rid] = rec
        rec.transition(RequestState.PENDING)
        self._emit(
            RequestAdded(
                request_id=rid,
                envelope=envelope,
                seq=rec.seq,
                received_at=rec.received_at,
                deadline_at=rec.deadline_at,
            )
        )
        return rec

    def _finish(self, rec: RequestRecord, state: RequestState) -> None:
        rec.transition(state)
        self._records.pop(rec.request_id, None)
        self._emit(RequestRemoved(request_id=rec.request_id, state=state))

    async def decide(self, decision: Decision) -> DecisionResult:
        rid = decision.request_id
        rec = self._records.get(rid)
        if rec is None:
            return DecisionResult(False, rid, "unknown")
        if rec.state is not RequestState.PENDING:
            return DecisionResult(False, rid, "not_pending", rec.state)
        if str(decision.action) not in rec.envelope.supported_actions:
            return DecisionResult(False, rid, "unsupported", rec.state)
        if decision.action is Action.ALLOW_ALWAYS:
            valid = {s.index for s in rec.envelope.suggestions}
            if decision.suggestion_index not in valid:
                return DecisionResult(False, rid, "bad_suggestion", rec.state)

        # Atomic claim: no await between the state check above and this line.
        rec.transition(RequestState.CLAIMED)
        msg = {"type": "decision", "broker_instance": self.broker_instance, **decision.to_dict()}
        try:
            delivered = await rec.send(msg)
        except Exception:
            log.exception("delivery failed for %s", rid)
            delivered = False
        if not delivered:
            self._finish(rec, RequestState.CANCELLED)
            return DecisionResult(False, rid, "delivery_failed", RequestState.CANCELLED)
        final = (
            RequestState.FALLBACK if decision.action is Action.FALLBACK else RequestState.RESOLVED
        )
        self._finish(rec, final)
        return DecisionResult(True, rid, "", final)

    async def expire_due(self) -> list[str]:
        now = self._clock()
        due = [
            r
            for r in self._records.values()
            if r.state is RequestState.PENDING and r.deadline_at <= now
        ]
        for rec in due:
            await self._end_pending(rec, RequestState.EXPIRED, "expired")
        return [r.request_id for r in due]

    def next_deadline(self) -> float | None:
        ds = [r.deadline_at for r in self._records.values() if r.state is RequestState.PENDING]
        return min(ds) if ds else None

    def disconnected(self, request_id: str) -> None:
        """The hook connection closed (agent killed it, timed out, or answered natively)."""
        rec = self._records.get(request_id)
        if rec is None or rec.state.terminal:
            return
        if rec.state is RequestState.CLAIMED:
            # A decision is in flight; the decide() coroutine settles it.
            return
        self._finish(rec, RequestState.CANCELLED)

    async def _end_pending(self, rec: RequestRecord, state: RequestState, reason: str) -> None:
        # Mark terminal first so no decision can race in while we notify the hook.
        self._finish(rec, state)
        try:
            await rec.send(
                {
                    "type": reason,
                    "request_id": rec.request_id,
                    "broker_instance": self.broker_instance,
                }
            )
        except Exception:
            pass

    async def shutdown(self) -> None:
        for rec in list(self._records.values()):
            if rec.state is RequestState.PENDING:
                await self._end_pending(rec, RequestState.EXPIRED, "expired")
