import pytest

from agent_dock.broker.registry import Registry, RegistryError, RequestAdded, RequestRemoved
from agent_dock.core.models import (
    Action,
    Decision,
    Envelope,
    InvalidTransition,
    PermissionSuggestion,
    RequestState,
)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def env(**kw) -> Envelope:
    base = dict(agent="claude", session_id="s", tool_name="Bash", tool_input={}, deadline_ms=45_000)
    base.update(kw)
    return Envelope(**base)


class Sink:
    def __init__(self, ok=True):
        self.ok = ok
        self.msgs = []

    async def __call__(self, msg):
        self.msgs.append(msg)
        return self.ok


@pytest.fixture
def reg():
    return Registry(broker_instance="inst", clock=Clock())


async def test_allow_once_resolves_and_emits(reg):
    events = []
    reg.add_observer(events.append)
    sink = Sink()
    e = env()
    reg.add(e, sink)
    assert reg.pending_count == 1
    res = await reg.decide(Decision(e.request_id, Action.ALLOW_ONCE))
    assert res.ok and res.state is RequestState.RESOLVED
    assert sink.msgs[0]["decision"] == "allow_once"
    assert sink.msgs[0]["broker_instance"] == "inst"
    assert reg.pending_count == 0
    assert isinstance(events[0], RequestAdded)
    assert isinstance(events[1], RequestRemoved) and events[1].state is RequestState.RESOLVED


async def test_double_decision_rejected(reg):
    e = env()
    sink = Sink()
    reg.add(e, sink)
    assert (await reg.decide(Decision(e.request_id, Action.ALLOW_ONCE))).ok
    second = await reg.decide(Decision(e.request_id, Action.DENY))
    assert not second.ok and second.reason == "unknown"
    assert len(sink.msgs) == 1


async def test_concurrent_claims_only_one_wins(reg):
    import asyncio

    e = env()
    sink = Sink()
    reg.add(e, sink)
    results = await asyncio.gather(
        reg.decide(Decision(e.request_id, Action.ALLOW_ONCE)),
        reg.decide(Decision(e.request_id, Action.DENY)),
    )
    assert sorted(r.ok for r in results) == [False, True]
    assert len(sink.msgs) == 1


async def test_decision_routes_only_to_matching_request(reg):
    sinks = [Sink() for _ in range(10)]
    envs = [env(session_id=f"s{i}") for i in range(10)]
    for e, s in zip(envs, sinks, strict=True):
        reg.add(e, s)
    await reg.decide(Decision(envs[3].request_id, Action.DENY))
    assert [len(s.msgs) for s in sinks] == [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    assert reg.pending_count == 9


async def test_duplicate_and_queue_full():
    reg = Registry(broker_instance="i", max_queue=2, clock=Clock())
    e = env()
    reg.add(e, Sink())
    with pytest.raises(RegistryError, match="duplicate"):
        reg.add(e, Sink())
    reg.add(env(), Sink())
    with pytest.raises(RegistryError, match="queue_full"):
        reg.add(env(), Sink())


async def test_unsupported_and_bad_suggestion(reg):
    e = env(supported_actions=["allow_once", "deny", "fallback"])
    reg.add(e, Sink())
    r = await reg.decide(Decision(e.request_id, Action.ALLOW_ALWAYS, suggestion_index=0))
    assert not r.ok and r.reason == "unsupported"
    e2 = env(
        supported_actions=["allow_once", "allow_always"],
        suggestions=[PermissionSuggestion(2, "x", "localSettings")],
    )
    reg.add(e2, Sink())
    r = await reg.decide(Decision(e2.request_id, Action.ALLOW_ALWAYS, suggestion_index=0))
    assert not r.ok and r.reason == "bad_suggestion"
    r = await reg.decide(Decision(e2.request_id, Action.ALLOW_ALWAYS, suggestion_index=2))
    assert r.ok


async def test_expiry_and_stale_decision():
    clock = Clock()
    reg = Registry(broker_instance="i", clock=clock)
    sink = Sink()
    e = env(deadline_ms=1000)
    reg.add(e, sink)
    clock.t += 0.5
    assert await reg.expire_due() == []
    clock.t += 1
    assert await reg.expire_due() == [e.request_id]
    assert sink.msgs[-1]["type"] == "expired"
    r = await reg.decide(Decision(e.request_id, Action.ALLOW_ONCE))
    assert not r.ok


async def test_disconnect_cancels(reg):
    events = []
    reg.add_observer(events.append)
    e = env()
    reg.add(e, Sink())
    reg.disconnected(e.request_id)
    assert events[-1].state is RequestState.CANCELLED
    assert not (await reg.decide(Decision(e.request_id, Action.ALLOW_ONCE))).ok


async def test_delivery_failure_cancels(reg):
    e = env()
    reg.add(e, Sink(ok=False))
    r = await reg.decide(Decision(e.request_id, Action.ALLOW_ONCE))
    assert not r.ok and r.reason == "delivery_failed"
    assert reg.pending_count == 0


async def test_fallback_state(reg):
    e = env()
    reg.add(e, Sink())
    r = await reg.decide(Decision(e.request_id, Action.FALLBACK))
    assert r.ok and r.state is RequestState.FALLBACK


def test_invalid_transitions():
    from agent_dock.broker.registry import RequestRecord

    rec = RequestRecord(envelope=env(), seq=1, received_at=0, deadline_at=1, send=Sink())
    with pytest.raises(InvalidTransition):
        rec.transition(RequestState.RESOLVED)


def test_envelope_validation():
    good = env().to_dict()
    Envelope.from_dict(good)
    for bad in (
        {**good, "protocol_version": 99},
        {**good, "agent": "gemini"},
        {**good, "request_id": "nope"},
        {**good, "tool_input": []},
        {**good, "deadline_ms": -1},
        {k: v for k, v in good.items() if k != "session_id"},
    ):
        with pytest.raises(ValueError):
            Envelope.from_dict(bad)
