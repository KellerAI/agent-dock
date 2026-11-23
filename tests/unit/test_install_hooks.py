from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_dock import install
from agent_dock.install import hooks
from agent_dock.install.backup import atomic_write_json, backup, read_json_object
from agent_dock.install.errors import ConfigError, InstallError

PY = Path("/Applications Support/AgentDock/venv/bin/python")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_DOCK_RUNTIME_DIR", str(tmp_path / "home" / "run"))
    monkeypatch.setenv("AGENT_DOCK_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENT_DOCK_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    return tmp_path


def backups(tmp_path) -> list[Path]:
    d = tmp_path / "home" / "backups"
    return sorted(d.iterdir()) if d.exists() else []


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))


# ---------------------------------------------------------------- pure functions


def test_build_hook_command_quotes_spaces():
    cmd = hooks.build_hook_command(PY, "claude")
    assert cmd == "'/Applications Support/AgentDock/venv/bin/python' -m agent_dock.hook claude"
    assert hooks.HOOK_MODULE_MARKER in cmd
    with pytest.raises(ValueError):
        hooks.build_hook_command(PY, "other")  # type: ignore[arg-type]


def test_merge_into_empty():
    new, changed = hooks.merge_hook({}, "x -m agent_dock.hook claude", 60)
    assert changed
    assert new == {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "x -m agent_dock.hook claude", "timeout": 60}
                    ],
                }
            ]
        }
    }


def test_merge_status_message():
    new, _ = hooks.merge_hook({}, "x -m agent_dock.hook codex", 60, "Waiting for Agent Dock")
    assert (
        new["hooks"]["PermissionRequest"][0]["hooks"][0]["statusMessage"]
        == "Waiting for Agent Dock"
    )


def test_merge_does_not_mutate_input():
    cfg = {"hooks": {"PermissionRequest": []}}
    hooks.merge_hook(cfg, "x -m agent_dock.hook claude", 60)
    assert cfg == {"hooks": {"PermissionRequest": []}}


def test_merge_idempotent():
    cmd = "x -m agent_dock.hook claude"
    first, _ = hooks.merge_hook({"a": 1}, cmd, 60)
    second, changed = hooks.merge_hook(first, cmd, 60)
    assert not changed
    assert json.dumps(second) == json.dumps(first)


def test_merge_replaces_mixed_group_entry_and_keeps_siblings():
    other = {"type": "command", "command": "other-tool"}
    cfg = {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        other,
                        {"type": "command", "command": "old -m agent_dock.hook claude"},
                    ],
                },
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "older -m agent_dock.hook claude"}],
                },
            ]
        }
    }
    new, changed = hooks.merge_hook(cfg, "new -m agent_dock.hook claude", 60)
    assert changed
    groups = new["hooks"]["PermissionRequest"]
    assert groups[0] == {"matcher": "Bash", "hooks": [other]}
    assert len(groups) == 2
    assert hooks.find_managed(new) == ["new -m agent_dock.hook claude"]


def test_merge_duplicate_managed_entries_collapsed():
    cmd = "x -m agent_dock.hook claude"
    one, _ = hooks.merge_hook({}, cmd, 60)
    dup = {"hooks": {"PermissionRequest": one["hooks"]["PermissionRequest"] * 2}}
    new, changed = hooks.merge_hook(dup, cmd, 60)
    assert changed
    assert hooks.find_managed(new) == [cmd]


def test_merge_timeout_change_is_a_change():
    cmd = "x -m agent_dock.hook claude"
    first, _ = hooks.merge_hook({}, cmd, 60)
    _, changed = hooks.merge_hook(first, cmd, 90)
    assert changed


@pytest.mark.parametrize(
    "cfg",
    [
        {"hooks": []},
        {"hooks": "x"},
        {"hooks": {"PermissionRequest": {}}},
        {"hooks": {"PermissionRequest": "x"}},
    ],
)
def test_merge_rejects_wrong_types(cfg):
    with pytest.raises(ConfigError):
        hooks.merge_hook(cfg, "x -m agent_dock.hook claude", 60)


def test_remove_only_managed_and_prunes_emptied_containers():
    cmd = "x -m agent_dock.hook claude"
    installed, _ = hooks.merge_hook({"model": "opus"}, cmd, 60)
    new, removed = hooks.remove_hook(installed)
    assert removed == 1
    assert new == {"model": "opus"}


def test_remove_keeps_preexisting_empty_containers():
    cfg = {"hooks": {"PermissionRequest": []}}
    new, removed = hooks.remove_hook(cfg)
    assert removed == 0 and new == cfg
    cfg2 = {"hooks": {}}
    assert hooks.remove_hook(cfg2) == (cfg2, 0)


def test_remove_keeps_other_events_and_groups():
    other_group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-me"}]}
    cfg = {
        "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "pre"}]}],
            "PermissionRequest": [other_group],
        }
    }
    installed, _ = hooks.merge_hook(cfg, "x -m agent_dock.hook claude", 60)
    new, removed = hooks.remove_hook(installed)
    assert removed == 1
    assert new == cfg


# ---------------------------------------------------------------- file wrappers


def test_fresh_install_creates_file(env):
    res = hooks.install_claude(PY, 60)
    path = env / "claude" / "settings.json"
    assert res.changed and res.path == path and res.backup_path is None
    data = json.loads(path.read_text())
    assert hooks.find_managed(data) == [hooks.build_hook_command(PY, "claude")]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert backups(env) == []


def test_codex_install_has_status_message_and_trust_hint(env):
    res = hooks.install_codex(PY, 60)
    assert res.path == env / "codex" / "hooks.json"
    assert "/hooks" in res.message
    hook = json.loads(res.path.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert hook["statusMessage"] == "Waiting for Agent Dock"
    assert hook["command"].endswith("-m agent_dock.hook codex")


def test_install_preserves_existing_settings(env):
    path = env / "claude" / "settings.json"
    existing = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "lint"}]}],
            "PermissionRequest": [
                {
                    "matcher": "Edit",
                    "hooks": [{"type": "command", "command": "notify", "timeout": 5}],
                }
            ],
        },
    }
    write(path, existing)
    os.chmod(path, 0o644)
    res = hooks.install_claude(PY, 60)
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["permissions"] == existing["permissions"]
    assert data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert data["hooks"]["PermissionRequest"][0] == existing["hooks"]["PermissionRequest"][0]
    assert len(data["hooks"]["PermissionRequest"]) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # mode preserved
    assert res.backup_path is not None
    assert json.loads(res.backup_path.read_text()) == existing
    assert stat.S_IMODE(res.backup_path.stat().st_mode) == 0o600


def test_install_twice_is_idempotent(env):
    path = env / "claude" / "settings.json"
    write(path, {"model": "opus"})
    hooks.install_claude(PY, 60)
    before = path.read_bytes()
    ino = path.stat().st_ino
    n_backups = len(backups(env))
    res = hooks.install_claude(PY, 60)
    assert not res.changed and res.backup_path is None
    assert path.read_bytes() == before
    assert path.stat().st_ino == ino  # not rewritten
    assert len(backups(env)) == n_backups


def test_python_path_change_replaces_entry(env):
    hooks.install_claude(PY, 60)
    new_py = Path("/opt/other/python")
    res = hooks.install_claude(new_py, 60)
    assert res.changed
    data = json.loads(res.path.read_text())
    assert hooks.find_managed(data) == [hooks.build_hook_command(new_py, "claude")]
    assert len(data["hooks"]["PermissionRequest"]) == 1


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"str"', "null"])
def test_bad_file_rejected_and_untouched(env, content):
    path = env / "claude" / "settings.json"
    write(path, content)
    with pytest.raises(ConfigError):
        hooks.install_claude(PY, 60)
    assert path.read_text() == content
    with pytest.raises(ConfigError):
        hooks.uninstall_claude()
    assert path.read_text() == content
    assert backups(env) == []
    assert "not installed" not in hooks.status_claude().message


def test_empty_file_treated_as_empty_object(env):
    path = env / "claude" / "settings.json"
    write(path, "  \n")
    assert hooks.install_claude(PY, 60).changed


def test_uninstall_removes_only_managed(env):
    path = env / "codex" / "hooks.json"
    existing = {
        "hooks": {
            "PermissionRequest": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "mine"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "bye"}]}],
        }
    }
    write(path, existing)
    hooks.install_codex(PY, 60)
    res = hooks.uninstall_codex()
    assert res.changed and res.backup_path is not None
    assert json.loads(path.read_text()) == existing
    res2 = hooks.uninstall_codex()
    assert not res2.changed


def test_uninstall_missing_file_does_not_create(env):
    res = hooks.uninstall_claude()
    assert not res.changed
    assert not (env / "claude" / "settings.json").exists()


def test_symlinked_settings_stays_symlink(env):
    real = env / "dotfiles" / "claude-settings.json"
    write(real, {"model": "opus"})
    link = env / "claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    res = hooks.install_claude(PY, 60)
    assert res.changed
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert hooks.find_managed(json.loads(real.read_text()))
    assert json.loads(res.backup_path.read_text()) == {"model": "opus"}
    hooks.uninstall_claude()
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"model": "opus"}


def test_status(env, tmp_path):
    assert not hooks.status_claude().installed
    py = tmp_path / "py dir" / "python"
    py.parent.mkdir()
    py.write_text("")
    hooks.install_claude(py, 60)
    st = hooks.status_claude()
    assert st.installed and st.python_ok
    py.unlink()
    st = hooks.status_claude()
    assert st.installed and not st.python_ok and "missing" in st.message


# ---------------------------------------------------------------- backup helpers


def test_backup_absent_returns_none(env):
    assert backup(env / "nope.json") is None


def test_backups_unique_and_private(env):
    p = env / "x" / "settings.json"
    write(p, {})
    b1, b2 = backup(p), backup(p)
    assert b1 != b2
    for b in (b1, b2):
        assert stat.S_IMODE(b.stat().st_mode) == 0o600


def test_atomic_write_creates_parent_and_default_mode(env):
    p = env / "new" / "dir" / "f.json"
    atomic_write_json(p, {"a": 1})
    assert read_json_object(p) == {"a": 1}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in p.parent.iterdir()] == ["f.json"]


# ---------------------------------------------------------------- install_all


def test_install_all_validates_python(env):
    with pytest.raises(InstallError):
        install.install_all(python=Path("relative/python"), timeout=60)
    with pytest.raises(InstallError):
        install.install_all(python=env / "missing", timeout=60)


def test_install_all_and_uninstall_all(env, tmp_path):
    py = tmp_path / "python"
    py.write_text("")
    msgs = install.install_all(python=py, timeout=60, launch_agent=False)
    assert len(msgs) == 2
    assert (env / "home" / "run").is_dir()
    assert stat.S_IMODE((env / "home" / "run").stat().st_mode) == 0o700
    status = install.status_all()
    assert status[0].startswith("claude: installed") and status[1].startswith("codex: installed")
    install.uninstall_all(launch_agent=False)
    assert hooks.find_managed(read_json_object(env / "claude" / "settings.json")) == []
    assert hooks.find_managed(read_json_object(env / "codex" / "hooks.json")) == []


def test_install_all_malformed_codex_touches_nothing(env, tmp_path):
    py = tmp_path / "python"
    py.write_text("")
    write(env / "codex" / "hooks.json", "{bad")
    with pytest.raises(ConfigError):
        install.install_all(python=py, timeout=60, launch_agent=False)
    assert not (env / "claude" / "settings.json").exists()
