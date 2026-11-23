from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from agent_dock.install import launchagent
from agent_dock.install.errors import InstallError

PY = Path("/Users/me/Library/Application Support/AgentDock/venv/bin/python")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_DOCK_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AGENT_DOCK_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    return tmp_path


class FakeRunner:
    def __init__(
        self, loaded: bool = False, fail: set[str] | None = None, fail_times: int | None = None
    ):
        """Verbs in ``fail`` return rc=5; ``fail_times`` limits how often (None = always)."""
        self.loaded = loaded
        self.fail = fail or set()
        self.fail_times = fail_times
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        assert kwargs.get("check") is False
        self.calls.append(list(args))
        verb = args[1]
        rc = 0
        if verb == "print":
            rc = 0 if self.loaded else 113
        elif verb in self.fail and (self.fail_times is None or self.fail_times > 0):
            rc = 5
            if self.fail_times is not None:
                self.fail_times -= 1
        if verb == "bootstrap" and rc == 0:
            self.loaded = True
        if verb == "bootout" and rc == 0:
            self.loaded = False
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="boom" if rc else "")


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(launchagent, "_is_macos", lambda: True)


def target():
    return f"gui/{os.getuid()}/local.agent-dock"


def test_build_plist(env):
    pl = launchagent.build_plist(PY, env / "logs")
    assert pl["Label"] == "local.agent-dock"
    assert pl["ProgramArguments"] == [str(PY), "-m", "agent_dock", "run"]
    assert pl["RunAtLoad"] is True
    assert pl["KeepAlive"] == {"SuccessfulExit": False}
    assert pl["ProcessType"] == "Interactive"
    assert pl["LimitLoadToSessionType"] == "Aqua"
    assert pl["StandardOutPath"].startswith(str(env / "logs"))
    assert pl["StandardErrorPath"].startswith(str(env / "logs"))
    assert pl["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1"}


def test_write_plist_roundtrip(env):
    path = launchagent.write_plist(PY)
    assert path == env / "LaunchAgents" / "local.agent-dock.plist"
    with open(path, "rb") as fh:
        data = plistlib.load(fh)
    assert data == launchagent.build_plist(PY, env / "logs")
    assert (env / "logs").is_dir()
    # rewrite is fine
    launchagent.write_plist(PY)
    assert launchagent.remove_plist() is True
    assert launchagent.remove_plist() is False


def test_non_macos_is_noop(monkeypatch):
    monkeypatch.setattr(launchagent, "_is_macos", lambda: False)
    runner = FakeRunner()
    assert "macOS-only" in launchagent.load(runner=runner)
    assert "macOS-only" in launchagent.unload(runner=runner)
    assert "macOS-only" in launchagent.restart(runner=runner)
    assert launchagent.is_loaded(runner=runner) is False
    assert runner.calls == []


def test_load_fresh(mac, env):
    path = launchagent.write_plist(PY)
    runner = FakeRunner(loaded=False)
    launchagent.load(path, runner=runner)
    assert runner.calls == [
        ["launchctl", "print", target()],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
    ]


def test_load_when_loaded_boots_out_first(mac, env):
    path = launchagent.write_plist(PY)
    runner = FakeRunner(loaded=True)
    launchagent.load(path, runner=runner)
    verbs = [c[1] for c in runner.calls]
    assert verbs == ["print", "print", "bootout", "bootstrap"]
    assert ["launchctl", "bootout", target()] in runner.calls


def test_load_retries_then_fails(mac, env):
    path = launchagent.write_plist(PY)
    sleeps = []
    runner = FakeRunner(fail={"bootstrap"}, fail_times=10)
    with pytest.raises(InstallError, match="bootstrap"):
        launchagent.load(path, runner=runner, retries=3, sleep=sleeps.append)
    assert [c[1] for c in runner.calls].count("bootstrap") == 3
    assert len(sleeps) == 2


def test_load_retry_succeeds(mac, env):
    path = launchagent.write_plist(PY)
    runner = FakeRunner(fail={"bootstrap"}, fail_times=1)
    msg = launchagent.load(path, runner=runner, sleep=lambda _s: None)
    assert "loaded" in msg
    assert [c[1] for c in runner.calls].count("bootstrap") == 2


def test_load_missing_plist(mac, env):
    with pytest.raises(InstallError):
        launchagent.load(env / "nope.plist", runner=FakeRunner())


def test_unload(mac):
    runner = FakeRunner(loaded=False)
    assert "not loaded" in launchagent.unload(runner=runner)
    assert [c[1] for c in runner.calls] == ["print"]
    runner = FakeRunner(loaded=True)
    launchagent.unload(runner=runner)
    assert runner.calls[-1] == ["launchctl", "bootout", target()]


def test_restart(mac):
    runner = FakeRunner(loaded=True)
    launchagent.restart(runner=runner)
    assert runner.calls == [["launchctl", "kickstart", "-k", target()]]


def test_restart_failure(mac):
    runner = FakeRunner(fail={"kickstart"})
    with pytest.raises(InstallError, match="kickstart"):
        launchagent.restart(runner=runner)
