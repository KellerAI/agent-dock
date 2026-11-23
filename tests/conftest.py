import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep every test away from the real home directory and sockets."""
    home = tmp_path / "home"
    home.mkdir()
    # AF_UNIX paths must be short (104 bytes on macOS), so use a short temp dir.
    run_dir = Path(tempfile.mkdtemp(prefix="ad-", dir="/tmp"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_DOCK_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("AGENT_DOCK_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("AGENT_DOCK_SOCKET", str(run_dir / "d.sock"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.delenv("AGENT_DOCK_LAUNCH_AGENTS_DIR", raising=False)
    yield
    for p in run_dir.iterdir():
        p.unlink()
    os.rmdir(run_dir)
