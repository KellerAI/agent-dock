from agent_dock.core.models import Envelope, PermissionSuggestion
from agent_dock.ui import viewmodel as vm


def env(**kw):
    base = dict(
        agent="claude",
        session_id="sess-a9f2",
        tool_name="Bash",
        tool_input={"command": "npm run build"},
        cwd="/Users/x/projects/macro-tracker/",
        summary="npm run build",
        detail="npm run build",
        terminal_program="iTerm.app",
    )
    base.update(kw)
    return Envelope(**base)


def test_card_with_title():
    c = vm.card(env(), title="Implement macro tracker")
    assert c.title == "Implement macro tracker"
    assert c.subtitle == "Claude Code · macro-tracker · Bash"
    assert c.terminal_bundle == "com.googlecode.iterm2"
    assert not c.can_allow_always


def test_card_fallback_title_and_suggestions():
    e = env(
        agent="codex",
        cwd=None,
        supported_actions=["allow_once", "deny", "allow_always"],
        suggestions=[PermissionSuggestion(0, "Always allow Bash(npm run build)", "localSettings")],
    )
    c = vm.card(e, title=None)
    assert c.title == "Codex · Bash approval"
    assert c.can_allow_always and c.suggestions[0].index == 0


def test_sensitive_mode_hides_detail():
    c = vm.card(env(detail="export TOKEN=abc"), title="t", reveal_detail=False)
    assert "abc" not in c.detail


def test_compact_and_badges():
    c = vm.card(env(), title="Implement macro tracker")
    assert vm.compact_line(c, 1) == "Implement macro tracker · needs approval"
    assert vm.compact_line(c, 3).startswith("3 · ")
    assert vm.badge_text(0) == "" and vm.badge_text(5) == "5" and vm.badge_text(150) == "99+"
    assert "150 pending" in vm.status_accessibility(150)
    assert vm.others_waiting_text(2) == "2 more waiting"


def test_title_is_sanitized():
    c = vm.card(env(), title="evil‮title\nline2")
    assert "‮" not in c.title and "\n" not in c.title
