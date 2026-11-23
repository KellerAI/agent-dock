"""Synthetic permission requests: run the real hook subprocess with fake agent stdin.

Used by ``agent-dock simulate`` and ``agent-dock run --demo`` so the UI can be
exercised without starting Claude Code or Codex.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

SAMPLES: list[dict[str, Any]] = [
    {
        "agent": "claude",
        "tool_name": "Bash",
        "tool_input": {"command": "npm run build", "description": "Build the frontend"},
        "cwd": "~/projects/macro-tracker",
        "suggest": True,
    },
    {
        "agent": "codex",
        "tool_name": "Bash",
        "tool_input": {"command": "cargo test --workspace", "description": "Run all tests"},
        "cwd": "~/projects/ledger-rs",
    },
    {
        "agent": "claude",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "src/app/routes.ts",
            "old_string": "const LIMIT = 10;",
            "new_string": "const LIMIT = 50;",
        },
        "cwd": "~/projects/macro-tracker",
    },
    {
        "agent": "claude",
        "tool_name": "WebFetch",
        "tool_input": {"url": "https://docs.python.org/3/library/asyncio.html", "prompt": "x"},
        "cwd": "~/projects/agent-dock",
    },
]


def build_stdin(
    *,
    agent: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str,
    session_id: str | None = None,
    suggest: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "session_id": session_id or f"demo-{uuid.uuid4().hex[:12]}",
        "transcript_path": None,
        "cwd": os.path.expanduser(cwd),
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if agent == "codex":
        data["turn_id"] = f"turn-{uuid.uuid4().hex[:8]}"
        data["model"] = "demo"
    if suggest and agent == "claude":
        rule: dict[str, Any] = {"toolName": tool_name}
        if isinstance(tool_input.get("command"), str):
            rule["ruleContent"] = tool_input["command"]
        data["permission_suggestions"] = [
            {
                "type": "addRules",
                "rules": [rule],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ]
    return data


@dataclass
class HookRun:
    agent: str
    tool_name: str
    returncode: int
    stdout: str
    seconds: float


def run_hook(
    agent: str, stdin_data: dict[str, Any], *, python: str | None = None, timeout: float = 120
) -> HookRun:
    start = time.monotonic()
    proc = subprocess.run(
        [python or sys.executable, "-m", "agent_dock.hook", agent],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return HookRun(
        agent,
        stdin_data["tool_name"],
        proc.returncode,
        proc.stdout.strip(),
        time.monotonic() - start,
    )


def describe(run: HookRun) -> str:
    if not run.stdout:
        verdict = "no decision (native prompt would take over)"
    else:
        try:
            d = json.loads(run.stdout)["hookSpecificOutput"]["decision"]
            verdict = d["behavior"]
            if d.get("updatedPermissions"):
                verdict += " + persistent rule"
            if d.get("message"):
                verdict += f" ({d['message']})"
        except (ValueError, KeyError, TypeError):
            verdict = f"unexpected output: {run.stdout[:200]}"
    return f"[{run.agent}] {run.tool_name}: {verdict} after {run.seconds:.1f}s"


def run_samples(count: int, interval: float, *, printer=print) -> list[HookRun]:
    results: list[HookRun] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        s = SAMPLES[i % len(SAMPLES)]
        data = build_stdin(
            agent=s["agent"],
            tool_name=s["tool_name"],
            tool_input=s["tool_input"],
            cwd=s["cwd"],
            suggest=s.get("suggest", False),
        )
        r = run_hook(s["agent"], data)
        with lock:
            results.append(r)
        printer(describe(r))

    threads = []
    for i in range(count):
        t = threading.Thread(target=worker, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        if interval and i < count - 1:
            time.sleep(interval)
    for t in threads:
        t.join()
    return results
