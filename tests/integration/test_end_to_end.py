"""Real broker on a real Unix socket, real hook subprocesses."""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from agent_dock import paths
from agent_dock.broker import transport
from agent_dock.broker.registry import RequestAdded, RequestRemoved
from agent_dock.broker.server import AlreadyRunning, Broker, BrokerThread, probe
from agent_dock.core.models import Action, Decision, RequestState
from agent_dock.settings import Settings
from agent_dock.simulate import build_stdin


class Harness:
    def __init__(self, settings=None):
        self.broker = Broker(settings or Settings())
        self.bt = BrokerThread(self.broker)
        self.added: list[RequestAdded] = []
        self.removed: list[RequestRemoved] = []
        self.cond = threading.Condition()
        self.broker.add_observer(self._on)

    def _on(self, ev):
        with self.cond:
            if isinstance(ev, RequestAdded):
                self.added.append(ev)
            elif isinstance(ev, RequestRemoved):
                self.removed.append(ev)
            self.cond.notify_all()

    def wait(self, pred, timeout=10):
        with self.cond:
            assert self.cond.wait_for(pred, timeout), "timed out waiting for broker event"

    def decide(self, rid, action, **kw):
        return self.bt.submit(Decision(rid, action, **kw)).result(5)


@pytest.fixture
def h():
    harness = Harness()
    harness.bt.start()
    yield harness
    harness.bt.stop()


def spawn_hook(agent="claude", **kw):
    data = build_stdin(
        agent=agent,
        tool_name=kw.pop("tool_name", "Bash"),
        tool_input=kw.pop("tool_input", {"command": "npm test"}),
        cwd="/tmp/proj",
        **kw,
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "agent_dock.hook", agent],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p.stdin.write(json.dumps(data))
    p.stdin.close()
    return p, data


def output(p):
    out = p.stdout.read()
    assert p.wait(10) == 0
    return json.loads(out) if out.strip() else None


def test_allow_round_trip(h):
    p, data = spawn_hook()
    h.wait(lambda: h.added)
    ev = h.added[0]
    assert ev.envelope.session_id == data["session_id"]
    assert h.decide(ev.request_id, Action.ALLOW_ONCE).ok
    assert output(p)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_deny_with_message_codex(h):
    p, _ = spawn_hook("codex")
    h.wait(lambda: h.added)
    h.decide(h.added[0].request_id, Action.DENY, message="not now")
    assert output(p)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "not now"}


def test_always_allow_echoes_native_suggestion(h):
    p, data = spawn_hook(suggest=True)
    h.wait(lambda: h.added)
    env = h.added[0].envelope
    assert env.suggestions
    h.decide(env.request_id, Action.ALLOW_ALWAYS, suggestion_index=env.suggestions[0].index)
    d = output(p)["hookSpecificOutput"]["decision"]
    assert d["updatedPermissions"] == data["permission_suggestions"]


def test_fallback_prints_nothing(h):
    p, _ = spawn_hook()
    h.wait(lambda: h.added)
    h.decide(h.added[0].request_id, Action.FALLBACK)
    assert output(p) is None


def test_ten_concurrent_agents_route_correctly(h):
    procs = []
    for i in range(10):
        agent = "codex" if i % 3 == 0 else "claude"
        p, data = spawn_hook(agent, tool_input={"command": f"echo {i}"})
        procs.append((p, data))
    h.wait(lambda: len(h.added) == 10)
    by_session = {ev.envelope.session_id: ev for ev in h.added}
    expected = {}
    for i, (_p, data) in enumerate(procs):
        action = Action.ALLOW_ONCE if i % 2 == 0 else Action.DENY
        expected[data["session_id"]] = "allow" if action is Action.ALLOW_ONCE else "deny"
        assert h.decide(by_session[data["session_id"]].request_id, action).ok
    for p, data in procs:
        assert (
            output(p)["hookSpecificOutput"]["decision"]["behavior"] == expected[data["session_id"]]
        )


def test_hook_killed_cancels_card(h):
    p, _ = spawn_hook()
    h.wait(lambda: h.added)
    p.kill()
    p.wait()
    h.wait(lambda: h.removed)
    assert h.removed[0].state is RequestState.CANCELLED
    assert not h.decide(h.added[0].request_id, Action.ALLOW_ONCE).ok


def test_expiry_falls_back_to_native():
    h = Harness(Settings(request_timeout_seconds=1))
    h.bt.start()
    try:
        p, _ = spawn_hook()
        h.wait(lambda: h.removed, timeout=10)
        assert h.removed[0].state is RequestState.EXPIRED
        assert output(p) is None
    finally:
        h.bt.stop()


def test_no_broker_means_fast_no_decision():
    start = time.monotonic()
    p, _ = spawn_hook()
    assert output(p) is None
    assert time.monotonic() - start < 5


def test_malformed_stdin_is_ignored():
    p = subprocess.run(
        [sys.executable, "-m", "agent_dock.hook", "claude"],
        input="{not json",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert p.returncode == 0 and p.stdout == ""


def test_socket_permissions_and_ping(h):
    sock = paths.socket_path()
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
    assert oct(os.stat(sock.parent).st_mode & 0o777) == "0o700"
    pong = probe(sock)
    assert pong and pong["broker_instance"] == h.broker.instance_id


def test_second_instance_refuses(h):
    other = BrokerThread(Broker(Settings()))
    with pytest.raises(AlreadyRunning):
        other.start()


def test_forged_decision_from_other_broker_instance_ignored(h):
    """A decision frame must carry the acking broker's instance id."""
    from agent_dock.adapters import ClaudeAdapter
    from agent_dock.hook import ask_broker

    data = build_stdin(agent="claude", tool_name="Bash", tool_input={"command": "x"}, cwd="/tmp")
    env = ClaudeAdapter().parse(data, Settings())
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("m", ask_broker(env.to_dict(), 5)))
    t.start()
    h.wait(lambda: h.added)
    rec = h.broker.registry.get(env.request_id)
    original = rec.send

    async def tamper(msg):
        return await original({**msg, "broker_instance": "someone-else"})

    rec.send = tamper
    h.decide(env.request_id, Action.ALLOW_ONCE)
    t.join(10)
    assert result["m"] is None


def test_garbage_client_does_not_break_broker(h):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(paths.socket_path()))
        s.sendall(b"\xff\xff\xff\xffgarbage")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(paths.socket_path()))
        transport.send_frame(s, {"type": "request", "envelope": {"protocol_version": 1}})
        ack = transport.recv_frame(s)
        assert ack["accepted"] is False and ack["reason"] == "malformed"
    assert probe(paths.socket_path())
