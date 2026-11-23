"""``agent-dock doctor``: environment and integration diagnostics."""

from __future__ import annotations

import os
import platform
import stat
import sys

from . import __version__, paths
from . import settings as settings_mod
from .broker.server import probe


def run() -> int:
    ok = True

    def line(status: str, text: str) -> None:
        print(f"[{status:^4}] {text}")

    line(
        "info", f"Agent Dock {__version__} · Python {platform.python_version()} · {sys.executable}"
    )
    line("info", f"Platform: {platform.platform()}")
    if paths.is_macos():
        try:
            import AppKit  # noqa: F401

            line("ok", "PyObjC/AppKit importable")
        except ImportError as exc:
            ok = False
            line("FAIL", f"PyObjC/AppKit not importable: {exc}")
    else:
        line("warn", "Not macOS: only the console UI (`agent-dock run --console`) is available")

    cfg = settings_mod.load()
    line(
        "info",
        f"Settings {paths.settings_path()}: request timeout {cfg.request_timeout_seconds:.0f}s, "
        f"hook timeout {cfg.hook_timeout_seconds}s, timings {cfg.expanded_seconds:.0f}/"
        f"{cfg.visible_seconds:.0f}/{cfg.hover_leave_seconds:.0f}s",
    )

    rd = paths.runtime_dir()
    if rd.exists():
        mode = stat.S_IMODE(rd.stat().st_mode)
        if mode & 0o077:
            ok = False
            line("FAIL", f"Runtime dir {rd} is mode {oct(mode)} (expected 0700)")
        else:
            line("ok", f"Runtime dir {rd} is private")
    sock = paths.socket_path()
    pong = probe(sock)
    if pong:
        line("ok", f"Broker running (pid unknown) on {sock}: {pong.get('pending', 0)} pending")
    else:
        line("warn", f"No broker answering on {sock} — hooks will fall back to native prompts")

    try:
        from .install import status_all

        for msg in status_all():
            line("info", msg)
    except Exception as exc:  # noqa: BLE001
        ok = False
        line("FAIL", f"Could not read install status: {exc}")

    if os.environ.get("CLAUDE_CONFIG_DIR"):
        line("info", f"CLAUDE_CONFIG_DIR={os.environ['CLAUDE_CONFIG_DIR']}")
    if os.environ.get("CODEX_HOME"):
        line("info", f"CODEX_HOME={os.environ['CODEX_HOME']}")
    line(
        "info",
        "Codex: new or changed hooks must be trusted once via `/hooks` inside Codex.",
    )
    return 0 if ok else 1
