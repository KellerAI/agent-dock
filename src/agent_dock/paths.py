"""Filesystem locations used by Agent Dock.

Everything lives under the current user's home directory. Environment overrides
exist so tests (and the Linux dev sandbox) can isolate state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AgentDock"
LAUNCH_AGENT_LABEL = "local.agent-dock"

# macOS limits sun_path to 104 bytes (including NUL). Keep a margin.
MAX_SOCKET_PATH = 100


def is_macos() -> bool:
    return sys.platform == "darwin"


def home() -> Path:
    return Path(os.path.expanduser("~"))


def app_support_dir() -> Path:
    override = os.environ.get("AGENT_DOCK_HOME")
    if override:
        return Path(override)
    if is_macos():
        return home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home() / ".local" / "share") / "agent-dock"


def runtime_dir() -> Path:
    override = os.environ.get("AGENT_DOCK_RUNTIME_DIR")
    if override:
        return Path(override)
    return app_support_dir() / "run"


def socket_path() -> Path:
    override = os.environ.get("AGENT_DOCK_SOCKET")
    if override:
        return Path(override)
    return runtime_dir() / "dock.sock"


def settings_path() -> Path:
    return app_support_dir() / "settings.json"


def log_dir() -> Path:
    override = os.environ.get("AGENT_DOCK_LOG_DIR")
    if override:
        return Path(override)
    if is_macos():
        return home() / "Library" / "Logs" / APP_NAME
    return app_support_dir() / "logs"


def venv_python() -> Path:
    """Interpreter the installer places in hook commands and the LaunchAgent."""
    return app_support_dir() / "venv" / "bin" / "python"


def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else home() / ".claude"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else home() / ".codex"


def launch_agents_dir() -> Path:
    override = os.environ.get("AGENT_DOCK_LAUNCH_AGENTS_DIR")
    if override:
        return Path(override)
    return home() / "Library" / "LaunchAgents"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (mode 0700) if needed and tighten permissions if it exists."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
