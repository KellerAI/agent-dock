"""Merge/remove the Agent Dock PermissionRequest hook in Claude Code and Codex config.

Pure functions operate on parsed JSON dicts; the ``install_*``/``uninstall_*``/
``status_*`` wrappers add locking, backups and atomic writes.
"""

from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .. import paths
from .backup import atomic_write_json, backup, locked, read_json_object
from .errors import ConfigError

Agent = Literal["claude", "codex"]

HOOK_MODULE_MARKER = "-m agent_dock.hook"
EVENT = "PermissionRequest"
CODEX_STATUS_MESSAGE = "Waiting for Agent Dock"
CODEX_TRUST_HINT = (
    "Codex only runs new or changed hooks after you trust them: start Codex, run /hooks "
    "and approve the Agent Dock PermissionRequest hook."
)


@dataclass
class HookResult:
    agent: str
    path: Path
    changed: bool
    backup_path: Path | None = None
    message: str = ""


@dataclass
class HookStatus:
    agent: str
    path: Path
    installed: bool
    commands: list[str] = field(default_factory=list)
    python_ok: bool = False
    message: str = ""


# --------------------------------------------------------------------------- pure


def build_hook_command(python: Path, agent: Agent) -> str:
    if agent not in ("claude", "codex"):
        raise ValueError(f"unknown agent {agent!r}")
    return f"{shlex.quote(str(python))} {HOOK_MODULE_MARKER} {agent}"


def _is_managed(hook: object) -> bool:
    return (
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and HOOK_MODULE_MARKER in hook["command"]
    )


def _event_list(config: dict, *, create: bool) -> list | None:
    hooks = config.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = config["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ConfigError(f'"hooks" must be a JSON object, found {type(hooks).__name__}')
    groups = hooks.get(EVENT)
    if groups is None:
        if not create:
            return None
        groups = hooks[EVENT] = []
    if not isinstance(groups, list):
        raise ConfigError(f'"hooks.{EVENT}" must be a JSON array, found {type(groups).__name__}')
    return groups


def _managed_locations(groups: list) -> list[tuple[dict, dict]]:
    """(group, hook) pairs for every managed hook entry."""
    found = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            for hook in group["hooks"]:
                if _is_managed(hook):
                    found.append((group, hook))
    return found


def _strip_managed(groups: list) -> tuple[list, int]:
    removed = 0
    out = []
    for group in groups:
        if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)):
            out.append(group)
            continue
        kept = [h for h in group["hooks"] if not _is_managed(h)]
        n = len(group["hooks"]) - len(kept)
        if n == 0:
            out.append(group)
            continue
        removed += n
        if kept:
            out.append({**group, "hooks": kept})
        # else: the group held only our entries -> drop it
    return out, removed


def desired_hook(command: str, timeout: int, status_message: str | None = None) -> dict:
    hook: dict = {"type": "command", "command": command, "timeout": int(timeout)}
    if status_message:
        hook["statusMessage"] = status_message
    return hook


def merge_hook(
    config: dict, command: str, timeout: int, status_message: str | None = None
) -> tuple[dict, bool]:
    """Ensure exactly one managed PermissionRequest hook equal to the desired one."""
    want = desired_hook(command, timeout, status_message)
    probe = _event_list(config, create=False)
    if probe is not None:
        found = _managed_locations(probe)
        if len(found) == 1:
            group, hook = found[0]
            if hook == want and group.get("matcher") == "*" and len(group["hooks"]) == 1:
                return copy.deepcopy(config), False
    new = copy.deepcopy(config)
    groups = _event_list(new, create=True)
    stripped, _ = _strip_managed(groups)
    stripped.append({"matcher": "*", "hooks": [want]})
    new["hooks"][EVENT] = stripped
    return new, True


def remove_hook(config: dict) -> tuple[dict, int]:
    new = copy.deepcopy(config)
    groups = _event_list(new, create=False)
    if groups is None:
        return new, 0
    stripped, removed = _strip_managed(groups)
    if not removed:
        return new, 0
    if stripped:
        new["hooks"][EVENT] = stripped
    else:
        del new["hooks"][EVENT]
        if not new["hooks"]:
            del new["hooks"]
    return new, removed


def find_managed(config: dict) -> list[str]:
    groups = _event_list(config, create=False)
    if groups is None:
        return []
    return [hook["command"] for _, hook in _managed_locations(groups)]


def _command_python(command: str) -> Path | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    return Path(parts[0]) if parts else None


# --------------------------------------------------------------------------- files


def claude_settings_file() -> Path:
    return paths.claude_config_dir() / "settings.json"


def codex_hooks_file() -> Path:
    return paths.codex_home() / "hooks.json"


def _install(
    agent: Agent, path: Path, python: Path, timeout: int, status_message: str | None
) -> HookResult:
    command = build_hook_command(python, agent)
    with locked(path):
        config = read_json_object(path)
        new, changed = merge_hook(config, command, timeout, status_message)
        if not changed:
            msg = f"{agent}: hook already installed in {path}"
            if agent == "codex":
                msg += ". If Codex still asks natively, run /hooks in Codex and trust it."
            return HookResult(agent, path, False, None, msg)
        bak = backup(path)
        atomic_write_json(path, new)
    msg = f"{agent}: installed PermissionRequest hook in {path}"
    if bak:
        msg += f" (backup: {bak})"
    if agent == "codex":
        msg += ". " + CODEX_TRUST_HINT
    return HookResult(agent, path, True, bak, msg)


def _uninstall(agent: Agent, path: Path) -> HookResult:
    if not path.exists():
        return HookResult(
            agent, path, False, None, f"{agent}: {path} does not exist; nothing to remove"
        )
    with locked(path):
        config = read_json_object(path)
        new, removed = remove_hook(config)
        if not removed:
            return HookResult(agent, path, False, None, f"{agent}: no Agent Dock hook in {path}")
        bak = backup(path)
        atomic_write_json(path, new)
    msg = f"{agent}: removed {removed} Agent Dock hook entr{'y' if removed == 1 else 'ies'} from {path}"
    if bak:
        msg += f" (backup: {bak})"
    return HookResult(agent, path, True, bak, msg)


def _status(agent: Agent, path: Path) -> HookStatus:
    try:
        config = read_json_object(path)
        commands = find_managed(config)
    except ConfigError as exc:
        return HookStatus(agent, path, False, [], False, f"{agent}: {exc}")
    if not commands:
        return HookStatus(agent, path, False, [], False, f"{agent}: not installed ({path})")
    pythons = [_command_python(c) for c in commands]
    python_ok = all(p is not None and p.is_absolute() and p.exists() for p in pythons)
    msg = f"{agent}: installed in {path}"
    if len(commands) > 1:
        msg += f" ({len(commands)} entries; reinstall to dedupe)"
    if not python_ok:
        missing = ", ".join(str(p) for p in pythons if p is None or not p.exists())
        msg += f"; WARNING hook interpreter missing: {missing}"
    if agent == "codex":
        msg += " (trust state is managed by Codex: check /hooks)"
    return HookStatus(agent, path, True, commands, python_ok, msg)


def install_claude(python: Path, timeout: int, settings_file: Path | None = None) -> HookResult:
    return _install("claude", settings_file or claude_settings_file(), python, timeout, None)


def install_codex(python: Path, timeout: int, hooks_file: Path | None = None) -> HookResult:
    return _install(
        "codex", hooks_file or codex_hooks_file(), python, timeout, CODEX_STATUS_MESSAGE
    )


def uninstall_claude(settings_file: Path | None = None) -> HookResult:
    return _uninstall("claude", settings_file or claude_settings_file())


def uninstall_codex(hooks_file: Path | None = None) -> HookResult:
    return _uninstall("codex", hooks_file or codex_hooks_file())


def status_claude(settings_file: Path | None = None) -> HookStatus:
    return _status("claude", settings_file or claude_settings_file())


def status_codex(hooks_file: Path | None = None) -> HookStatus:
    return _status("codex", hooks_file or codex_hooks_file())
