"""Installer: hook config for Claude Code / Codex, and the login LaunchAgent."""

from __future__ import annotations

import os
from pathlib import Path

from .. import paths
from . import hooks, launchagent
from .backup import read_json_object
from .errors import ConfigError, InstallError

__all__ = ["ConfigError", "InstallError", "install_all", "uninstall_all", "status_all"]


def _manage_launch_agent() -> bool:
    # Off macOS only touch a plist when tests/dev point us at a scratch dir.
    return paths.is_macos() or bool(os.environ.get("AGENT_DOCK_LAUNCH_AGENTS_DIR"))


def install_all(
    *,
    python: Path,
    claude: bool = True,
    codex: bool = True,
    launch_agent: bool = True,
    timeout: int,
) -> list[str]:
    python = Path(python)
    if not python.is_absolute():
        raise InstallError(f"Interpreter path must be absolute: {python}")
    if not python.exists():
        raise InstallError(f"Interpreter does not exist: {python}")
    if timeout <= 0:
        raise InstallError(f"Hook timeout must be positive, got {timeout}")
    paths.ensure_private_dir(paths.runtime_dir())
    messages: list[str] = []
    # Parse both files before touching either, so a malformed Codex file
    # doesn't leave a half-finished install. (Each install re-reads under lock.)
    if claude:
        hooks.find_managed(read_json_object(hooks.claude_settings_file()))
    if codex:
        hooks.find_managed(read_json_object(hooks.codex_hooks_file()))
    if claude:
        messages.append(hooks.install_claude(python, timeout).message)
    if codex:
        messages.append(hooks.install_codex(python, timeout).message)
    if launch_agent:
        if _manage_launch_agent():
            plist = launchagent.write_plist(python)
            messages.append(f"LaunchAgent plist written to {plist}")
            messages.append(launchagent.load(plist))
        else:
            messages.append(f"LaunchAgent: {launchagent.NOT_MACOS}")
    return messages


def uninstall_all(
    *, claude: bool = True, codex: bool = True, launch_agent: bool = True
) -> list[str]:
    messages: list[str] = []
    errors: list[str] = []
    if launch_agent:
        if _manage_launch_agent():
            try:
                messages.append(launchagent.unload())
            except InstallError as exc:
                errors.append(str(exc))
            if launchagent.remove_plist():
                messages.append(f"Removed {launchagent.plist_path()}")
        else:
            messages.append(f"LaunchAgent: {launchagent.NOT_MACOS}")
    for enabled, fn in ((claude, hooks.uninstall_claude), (codex, hooks.uninstall_codex)):
        if not enabled:
            continue
        try:
            messages.append(fn().message)
        except ConfigError as exc:
            errors.append(str(exc))
    if errors:
        raise InstallError("\n".join(messages + errors))
    return messages


def status_all() -> list[str]:
    messages = [hooks.status_claude().message, hooks.status_codex().message]
    try:
        messages.append(launchagent.status())
    except OSError as exc:
        messages.append(f"LaunchAgent: status unavailable ({exc})")
    return messages
