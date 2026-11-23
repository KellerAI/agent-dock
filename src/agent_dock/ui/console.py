"""Terminal front-end for the broker: development on non-macOS hosts and diagnostics.

Commands:  a N (allow once) | d N [message] (deny) | A N K (always-allow suggestion K)
           f N (answer in terminal) | l (list) | q (quit)
"""

from __future__ import annotations

import sys
import threading
from typing import TextIO

from ..broker.registry import RequestAdded, RequestRemoved
from ..broker.server import BrokerThread, TitleUpdated
from ..core.models import Action, Decision, Envelope


class ConsoleUI:
    def __init__(self, broker_thread: BrokerThread, out: TextIO = sys.stdout):
        self.bt = broker_thread
        self.out = out
        self._lock = threading.Lock()
        self._numbers: dict[int, Envelope] = {}
        self._next = 1
        self._titles: dict[tuple[str, str], str] = {}
        broker_thread.broker.add_observer(self._on_event)

    def _print(self, text: str) -> None:
        with self._lock:
            print(text, file=self.out, flush=True)

    def _title(self, env: Envelope) -> str:
        t = self._titles.get((env.agent, env.session_id))
        if t:
            return t
        ident = self.bt.broker.identity_for(env.agent, env.session_id)
        return ident.display_title if ident and ident.display_title else env.session_id

    def _on_event(self, ev: object) -> None:  # broker loop thread
        if isinstance(ev, RequestAdded):
            with self._lock:
                n = self._next
                self._next += 1
                self._numbers[n] = ev.envelope
            env = ev.envelope
            lines = [f"#{n} [{env.agent}] {self._title(env)} · {env.tool_name}: {env.summary}"]
            for s in env.suggestions:
                lines.append(f"    always-allow option {s.index}: {s.label}")
            self._print("\n".join(lines))
        elif isinstance(ev, RequestRemoved):
            with self._lock:
                gone = [k for k, e in self._numbers.items() if e.request_id == ev.request_id]
                for k in gone:
                    del self._numbers[k]
            for k in gone:
                self._print(f"#{k} {ev.state}")
        elif isinstance(ev, TitleUpdated):
            self._titles[(ev.provider, ev.session_id)] = ev.identity.display_title or ""
            self._print(f"title: {ev.provider} …{ev.session_id[-4:]} → {ev.identity.display_title}")

    def handle(self, line: str) -> bool:
        parts = line.strip().split(maxsplit=2)
        if not parts:
            return True
        cmd = parts[0]
        if cmd == "q":
            return False
        if cmd == "l":
            with self._lock:
                items = list(self._numbers.items())
            self._print(
                "\n".join(f"#{k} {e.agent} {e.tool_name}: {e.summary}" for k, e in items)
                or "(none)"
            )
            return True
        if cmd not in ("a", "d", "A", "f") or len(parts) < 2 or not parts[1].isdigit():
            self._print(__doc__ or "")
            return True
        with self._lock:
            env = self._numbers.get(int(parts[1]))
        if env is None:
            self._print("no such pending request")
            return True
        if cmd == "a":
            dec = Decision(env.request_id, Action.ALLOW_ONCE)
        elif cmd == "d":
            dec = Decision(
                env.request_id, Action.DENY, message=parts[2] if len(parts) > 2 else None
            )
        elif cmd == "f":
            dec = Decision(env.request_id, Action.FALLBACK)
        else:
            if len(parts) < 3 or not parts[2].isdigit():
                self._print("usage: A N K")
                return True
            dec = Decision(env.request_id, Action.ALLOW_ALWAYS, suggestion_index=int(parts[2]))
        result = self.bt.submit(dec).result(timeout=5)
        if not result.ok:
            self._print(f"rejected: {result.reason}")
        return True

    def run(self, stdin: TextIO = sys.stdin) -> None:
        self._print("Agent Dock console. Waiting for requests… (type 'l', 'a N', 'd N msg', 'q')")
        for line in stdin:
            if not self.handle(line):
                break
