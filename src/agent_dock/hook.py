"""Hook entry point: ``python -m agent_dock.hook {claude|codex}``.

Contract with the agent: read one PermissionRequest JSON from stdin, and either
print a documented decision JSON or print nothing (=> the agent's native prompt
handles it). Every failure path prints nothing and exits 0. This process never
allows anything on its own.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, TextIO

from . import PROTOCOL_VERSION, paths
from . import settings as settings_mod
from .adapters import ADAPTERS
from .broker import transport
from .core.models import Decision

MAX_STDIN = 8 * 1024 * 1024
CONNECT_TIMEOUT = 0.5
ACK_TIMEOUT = 3.0
DECISION_GRACE = 5.0


def _debug(msg: str) -> None:
    if os.environ.get("AGENT_DOCK_HOOK_DEBUG"):
        print(f"agent-dock hook: {msg}", file=sys.stderr)


def ask_broker(envelope_dict: dict[str, Any], wait_seconds: float) -> dict[str, Any] | None:
    """Send the request and block for the final message. None means: no decision."""
    path = str(paths.socket_path())
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return None
    with sock:
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(path)
            sock.settimeout(ACK_TIMEOUT)
            transport.send_frame(
                sock,
                {
                    "type": "request",
                    "protocol_version": PROTOCOL_VERSION,
                    "envelope": envelope_dict,
                },
            )
            ack = transport.recv_frame(sock)
            if not ack or ack.get("type") != "ack" or not ack.get("accepted"):
                _debug(f"broker did not accept request: {ack and ack.get('reason')}")
                return None
            if ack.get("request_id") != envelope_dict["request_id"]:
                return None
            instance = ack.get("broker_instance")
            sock.settimeout(wait_seconds)
            msg = transport.recv_frame(sock)
        except (OSError, TimeoutError, transport.ProtocolError) as exc:
            _debug(f"broker unavailable: {exc!r}")
            return None
    if not msg or msg.get("type") != "decision":
        return None
    # Reject anything not addressed to *this* request from *this* broker instance.
    if (
        msg.get("request_id") != envelope_dict["request_id"]
        or msg.get("broker_instance") != instance
    ):
        return None
    return msg


def run(agent: str, stdin: TextIO, stdout: TextIO) -> int:
    adapter = ADAPTERS.get(agent)
    if adapter is None:
        _debug(f"unknown agent {agent!r}")
        return 0
    try:
        raw = stdin.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            return 0
        data = json.loads(raw)
        if not isinstance(data, dict):
            return 0
    except (ValueError, OSError):
        return 0

    cfg = settings_mod.load()
    try:
        envelope = adapter.parse(data, cfg)
    except Exception as exc:  # adapter bug must never block the agent
        _debug(f"adapter parse failed: {exc!r}")
        return 0
    if envelope is None:
        return 0

    env_dict = envelope.to_dict()
    try:
        transport.encode({"type": "request", "envelope": env_dict})
    except (transport.ProtocolError, TypeError, ValueError):
        return 0

    msg = ask_broker(env_dict, envelope.deadline_ms / 1000.0 + DECISION_GRACE)
    if msg is None:
        return 0
    try:
        decision = Decision.from_dict(msg)
        out = adapter.format(decision, data)
    except (ValueError, KeyError, TypeError):
        return 0
    if out is None:
        return 0
    stdout.write(json.dumps(out))
    stdout.write("\n")
    stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m agent_dock.hook {claude|codex}", file=sys.stderr)
        return 0  # still never block the agent
    try:
        return run(argv[0], sys.stdin, sys.stdout)
    except Exception as exc:  # last-resort guard
        _debug(f"unexpected error: {exc!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
