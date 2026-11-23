"""Asyncio Unix-domain-socket broker.

One connection per waiting hook. The connection's lifetime *is* the request's
lifetime: if the hook disconnects, its card is cancelled.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import socket
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import PROTOCOL_VERSION, __version__, paths
from ..core.models import Decision, Envelope, SessionIdentity
from ..core.session_identity import SessionRegistry, TitleResolver
from ..settings import Settings
from . import transport
from .peercred import peer_uid
from .registry import DecisionResult, Registry, RegistryError, RequestAdded

log = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT = 5.0
TITLE_REFRESH_INTERVAL = 2.0


class AlreadyRunning(Exception):
    pass


@dataclass(frozen=True)
class TitleUpdated:
    provider: str
    session_id: str
    identity: SessionIdentity


def probe(path: Path, timeout: float = 0.5) -> dict[str, Any] | None:
    """Ping a broker at ``path``; return its pong or None."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            transport.send_frame(s, {"type": "ping", "protocol_version": PROTOCOL_VERSION})
            msg = transport.recv_frame(s)
            return msg if msg and msg.get("type") == "pong" else None
    except (OSError, transport.ProtocolError):
        return None


class Broker:
    def __init__(
        self,
        settings: Settings,
        *,
        socket_path: Path | None = None,
        sessions: SessionRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.socket_path = socket_path or paths.socket_path()
        self.instance_id = str(uuid.uuid4())
        self.registry = Registry(
            broker_instance=self.instance_id, max_queue=settings.max_queue, clock=clock
        )
        self.sessions = sessions or SessionRegistry(TitleResolver())
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._uid = os.getuid()

    # -- observers (called on the loop thread) --
    def add_observer(self, cb: Callable[[object], None]) -> None:
        self.registry.add_observer(cb)

    def _emit(self, event: object) -> None:
        self.registry._emit(event)

    # -- startup / shutdown --
    def _prepare_socket_path(self) -> None:
        path = self.socket_path
        if len(os.fsencode(str(path))) > paths.MAX_SOCKET_PATH:
            raise OSError(f"socket path too long for AF_UNIX: {path}")
        paths.ensure_private_dir(path.parent)
        if path.exists() or path.is_symlink():
            st = path.lstat()
            if not stat.S_ISSOCK(st.st_mode):
                raise OSError(f"refusing to replace non-socket file at {path}")
            if probe(path) is not None:
                raise AlreadyRunning(f"another Agent Dock broker is listening on {path}")
            path.unlink()

    async def start(self) -> None:
        self._prepare_socket_path()
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=str(self.socket_path), limit=transport.MAX_FRAME + 8
            )
        finally:
            os.umask(old)
        os.chmod(self.socket_path, 0o600)
        self._spawn(self._expiry_loop())
        self._spawn(self._title_loop())
        log.info("broker %s listening on %s", self.instance_id[:8], self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        await self.registry.shutdown()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 1.0)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                self.socket_path.unlink()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- public API (loop thread) --
    async def decide(self, decision: Decision) -> DecisionResult:
        result = await self.registry.decide(decision)
        log.info(
            "decision %s for %s -> %s%s",
            decision.action,
            decision.request_id[:8],
            "ok" if result.ok else "rejected",
            f" ({result.reason})" if result.reason else "",
        )
        return result

    def identity_for(self, provider: str, session_id: str) -> SessionIdentity | None:
        return self.sessions.get(provider, session_id)  # type: ignore[arg-type]

    # -- background loops --
    async def _expiry_loop(self) -> None:
        while True:
            await self.registry.expire_due()
            nxt = self.registry.next_deadline()
            delay = 0.5 if nxt is None else max(0.01, min(0.5, nxt - time.monotonic()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _title_loop(self) -> None:
        while True:
            await asyncio.sleep(TITLE_REFRESH_INTERVAL)
            seen: set[tuple[str, str]] = set()
            for rec in self.registry.actionable():
                env = rec.envelope
                key = (env.agent, env.session_id)
                if key in seen:
                    continue
                seen.add(key)
                await self._refresh_title(env)

    async def _refresh_title(self, env: Envelope) -> None:
        try:
            ident = await self.sessions.refresh(env.agent, env.session_id, env.transcript_path)
        except Exception:
            log.debug("title refresh failed", exc_info=True)
            return
        if ident is not None:
            self._emit(TitleUpdated(env.agent, env.session_id, ident))

    # -- connection handling --
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        if uid is not None and uid != self._uid:
            log.warning("rejected connection from uid %s", uid)
            writer.close()
            return
        try:
            first = await asyncio.wait_for(transport.read_frame(reader), FIRST_FRAME_TIMEOUT)
        except (TimeoutError, transport.ProtocolError) as exc:
            log.debug("bad first frame: %s", exc)
            writer.close()
            return
        if first is None:
            writer.close()
            return

        kind = first.get("type")
        try:
            if kind == "ping":
                await transport.write_frame(
                    writer,
                    {
                        "type": "pong",
                        "protocol_version": PROTOCOL_VERSION,
                        "version": __version__,
                        "broker_instance": self.instance_id,
                        "pending": self.registry.pending_count,
                    },
                )
            elif kind == "request":
                await self._handle_request(first, reader, writer)
            else:
                await transport.write_frame(writer, {"type": "error", "reason": "unknown_type"})
        except (ConnectionError, transport.ProtocolError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_request(
        self,
        first: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        async def reject(reason: str, rid: Any = None) -> None:
            await transport.write_frame(
                writer,
                {
                    "type": "ack",
                    "accepted": False,
                    "reason": reason,
                    "request_id": rid,
                    "broker_instance": self.instance_id,
                },
            )

        try:
            env = Envelope.from_dict(first.get("envelope"))  # type: ignore[arg-type]
        except (ValueError, KeyError, TypeError) as exc:
            log.info("rejected malformed request: %s", exc)
            await reject("malformed")
            return

        # The broker, not the hook, owns the deadline ceiling.
        env.deadline_ms = min(env.deadline_ms, int(self.settings.request_timeout_seconds * 1000))

        done = asyncio.Event()

        async def send(msg: dict[str, Any]) -> bool:
            if writer.is_closing():
                return False
            try:
                await transport.write_frame(writer, msg)
                return True
            except (ConnectionError, OSError):
                return False

        # Session identity: render immediately with a cached/fallback title.
        self.sessions.ensure(env.agent, env.session_id, env.cwd, env.tool_name)
        try:
            rec = self.registry.add(env, send)
        except RegistryError as exc:
            await reject(exc.reason, env.request_id)
            return
        rec.on_terminal.append(done.set)
        self._wake.set()
        log.info(
            "request %s from %s session …%s tool=%s",
            env.request_id[:8],
            env.agent,
            env.session_id[-4:],
            env.tool_name,
        )

        if not await send(
            {
                "type": "ack",
                "accepted": True,
                "request_id": env.request_id,
                "broker_instance": self.instance_id,
            }
        ):
            self.registry.disconnected(env.request_id)
            return
        self._spawn(self._refresh_title(env))

        async def watch_eof() -> None:
            # Hooks send nothing after the request; any data or EOF ends the wait.
            with contextlib.suppress(Exception):
                await reader.read(1)

        eof = asyncio.ensure_future(watch_eof())
        finished = asyncio.ensure_future(done.wait())
        try:
            await asyncio.wait({eof, finished}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not finished.done():
                self.registry.disconnected(env.request_id)
            for t in (eof, finished):
                t.cancel()


class BrokerThread:
    """Runs a Broker on a private asyncio loop in a daemon thread (UI stays on main thread)."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="agent-dock-broker", daemon=True)
        self._started = threading.Event()
        self._error: BaseException | None = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.broker.start())
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._started.set()
            return
        self._started.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self.broker.stop())
        self.loop.close()

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        self._started.wait(timeout)
        if self._error is not None:
            raise self._error

    def call(self, fn: Callable[[], Any]) -> None:
        self.loop.call_soon_threadsafe(fn)

    def submit(self, decision: Decision) -> concurrent.futures.Future[DecisionResult]:
        return asyncio.run_coroutine_threadsafe(self.broker.decide(decision), self.loop)

    def stop(self, timeout: float = 5.0) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)


__all__ = [
    "AlreadyRunning",
    "Broker",
    "BrokerThread",
    "RequestAdded",
    "TitleUpdated",
    "probe",
]
