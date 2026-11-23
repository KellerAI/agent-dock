from agent_dock.adapters import ClaudeAdapter, CodexAdapter
from agent_dock.core.models import Action, Decision, Envelope
from agent_dock.settings import Settings

SUGGESTION = {
    "type": "addRules",
    "rules": [{"toolName": "Bash", "ruleContent": "rm -rf node_modules"}],
    "behavior": "allow",
    "destination": "localSettings",
}

CLAUDE_INPUT = {
    "session_id": "abc123",
    "transcript_path": "/Users/x/.claude/projects/p/abc123.jsonl",
    "cwd": "/Users/x/proj",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf node_modules", "description": "Remove node_modules"},
    "permission_suggestions": [
        SUGGESTION,
        {"type": "setMode", "mode": "bypassPermissions", "destination": "session"},
    ],
}


def test_claude_parse_envelope_roundtrip():
    env = ClaudeAdapter().parse(CLAUDE_INPUT, Settings())
    assert env is not None
    assert env.summary == "rm -rf node_modules"
    assert "Remove node_modules" in env.detail
    # bypassPermissions suggestion is never offered
    assert [s.index for s in env.suggestions] == [0]
    assert "localSettings" not in env.suggestions[0].label
    assert "settings.local.json" in env.suggestions[0].label
    assert Action.ALLOW_ALWAYS in env.supported_actions
    again = Envelope.from_dict(env.to_dict())
    assert again.request_id == env.request_id and again.suggestions == env.suggestions


def test_claude_outputs():
    a = ClaudeAdapter()
    rid = "x"
    allow = a.format(Decision(rid, Action.ALLOW_ONCE), CLAUDE_INPUT)
    assert allow == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    deny = a.format(Decision(rid, Action.DENY, message="no"), CLAUDE_INPUT)
    assert deny["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "no"}
    always = a.format(Decision(rid, Action.ALLOW_ALWAYS, suggestion_index=0), CLAUDE_INPUT)
    assert always["hookSpecificOutput"]["decision"]["updatedPermissions"] == [SUGGESTION]
    # index pointing at a filtered (unsafe) suggestion -> no decision
    assert a.format(Decision(rid, Action.ALLOW_ALWAYS, suggestion_index=1), CLAUDE_INPUT) is None
    assert a.format(Decision(rid, Action.ALLOW_ALWAYS, suggestion_index=7), CLAUDE_INPUT) is None
    assert a.format(Decision(rid, Action.FALLBACK), CLAUDE_INPUT) is None


def test_claude_passthrough_and_plan_echo():
    a = ClaudeAdapter()
    assert a.parse({**CLAUDE_INPUT, "tool_name": "AskUserQuestion"}, Settings()) is None
    plan_input = {**CLAUDE_INPUT, "tool_name": "ExitPlanMode", "tool_input": {"plan": "1. do it"}}
    env = a.parse(plan_input, Settings())
    assert env.detail == "1. do it"
    out = a.format(Decision("x", Action.ALLOW_ONCE), plan_input)
    assert out["hookSpecificOutput"]["decision"]["updatedInput"] == {"plan": "1. do it"}


def test_claude_no_suggestions_no_always():
    data = {k: v for k, v in CLAUDE_INPUT.items() if k != "permission_suggestions"}
    env = ClaudeAdapter().parse(data, Settings())
    assert Action.ALLOW_ALWAYS not in env.supported_actions


def test_large_input_is_bounded():
    data = {
        **CLAUDE_INPUT,
        "tool_name": "Write",
        "tool_input": {"file_path": "a", "content": "x" * 2_000_000},
    }
    env = ClaudeAdapter().parse(data, Settings())
    assert len(env.detail) < 20_000 and env.detail_truncated
    assert len(env.tool_input["content"]) < 3000


def test_codex_never_offers_persistence():
    data = {
        "session_id": "s1",
        "turn_id": "t1",
        "cwd": "/tmp/p",
        "hook_event_name": "PermissionRequest",
        "model": "gpt",
        "tool_name": "Bash",
        "tool_input": {"command": "cargo test", "description": None},
        "permission_suggestions": [SUGGESTION],
    }
    a = CodexAdapter()
    env = a.parse(data, Settings())
    assert env.suggestions == [] and Action.ALLOW_ALWAYS not in env.supported_actions
    assert a.format(Decision("x", Action.ALLOW_ONCE), data) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    deny = a.format(Decision("x", Action.DENY), data)["hookSpecificOutput"]["decision"]
    assert deny["behavior"] == "deny" and deny["message"]
    assert "updatedPermissions" not in deny and "interrupt" not in deny
    assert a.format(Decision("x", Action.ALLOW_ALWAYS, suggestion_index=0), data) is None


def test_control_chars_sanitized():
    data = {**CLAUDE_INPUT, "tool_input": {"command": "echo hi‮; rm -rf /\x1b[2K"}}
    env = ClaudeAdapter().parse(data, Settings())
    assert "‮" not in env.detail and "\x1b" not in env.detail
