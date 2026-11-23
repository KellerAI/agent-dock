"""Per-user LaunchAgent that starts the Agent Dock menu-bar app at login."""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .. import paths
from .backup import atomic_write_bytes
from .errors import InstallError

LABEL = paths.LAUNCH_AGENT_LABEL
NOT_MACOS = "launchctl is macOS-only; skipped"

Runner = Callable[..., subprocess.CompletedProcess]


def _is_macos() -> bool:
    return paths.is_macos()


def plist_path() -> Path:
    return paths.launch_agents_dir() / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def build_plist(python: Path, log_dir: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "agent_dock", "run"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(Path(log_dir) / "agent-dock.out.log"),
        "StandardErrorPath": str(Path(log_dir) / "agent-dock.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def write_plist(python: Path, log_dir: Path | None = None, path: Path | None = None) -> Path:
    log_dir = paths.ensure_private_dir(log_dir or paths.log_dir())
    path = path or plist_path()
    data = plistlib.dumps(build_plist(python, log_dir), fmt=plistlib.FMT_XML)
    atomic_write_bytes(path, data, default_mode=0o644)
    return path


def remove_plist(path: Path | None = None) -> bool:
    path = path or plist_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _run(runner: Runner | None, args: list[str]) -> subprocess.CompletedProcess:
    runner = runner or subprocess.run
    return runner(args, capture_output=True, text=True, check=False)


def _err(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip() or f"exit {proc.returncode}"


def is_loaded(runner: Runner | None = None) -> bool:
    if not _is_macos():
        return False
    return _run(runner, ["launchctl", "print", service_target()]).returncode == 0


def unload(runner: Runner | None = None) -> str:
    if not _is_macos():
        return NOT_MACOS
    if not is_loaded(runner):
        return f"LaunchAgent {LABEL} not loaded"
    proc = _run(runner, ["launchctl", "bootout", service_target()])
    if proc.returncode != 0:
        raise InstallError(f"launchctl bootout {service_target()} failed: {_err(proc)}")
    return f"LaunchAgent {LABEL} unloaded"


def load(
    path: Path | None = None,
    runner: Runner | None = None,
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Bootstrap the agent, booting out any loaded copy first (idempotent)."""
    if not _is_macos():
        return NOT_MACOS
    path = path or plist_path()
    if not path.exists():
        raise InstallError(f"LaunchAgent plist {path} does not exist")
    if is_loaded(runner):
        unload(runner)
    proc = None
    for attempt in range(max(1, retries)):
        proc = _run(runner, ["launchctl", "bootstrap", domain(), str(path)])
        if proc.returncode == 0:
            return f"LaunchAgent {LABEL} loaded from {path}"
        # bootout is asynchronous; bootstrap can briefly fail with EIO afterwards.
        if attempt + 1 < retries:
            sleep(0.5)
    raise InstallError(f"launchctl bootstrap {domain()} {path} failed: {_err(proc)}")


def restart(runner: Runner | None = None) -> str:
    if not _is_macos():
        return NOT_MACOS
    proc = _run(runner, ["launchctl", "kickstart", "-k", service_target()])
    if proc.returncode != 0:
        raise InstallError(f"launchctl kickstart -k {service_target()} failed: {_err(proc)}")
    return f"LaunchAgent {LABEL} restarted"


def status(runner: Runner | None = None) -> str:
    path = plist_path()
    present = "present" if path.exists() else "missing"
    if not _is_macos():
        return f"LaunchAgent plist {path}: {present}; {NOT_MACOS}"
    loaded = "loaded" if is_loaded(runner) else "not loaded"
    return f"LaunchAgent {LABEL}: plist {present} ({path}), {loaded}"
