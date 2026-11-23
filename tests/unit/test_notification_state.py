from agent_dock.ui.notification_state import Mode, PresentationController


def ctl(**kw):
    return PresentationController(
        expanded_seconds=10, visible_seconds=30, hover_leave_seconds=1, **kw
    )


def run_to(c, t):
    """Advance time by firing every deadline up to t, like the AppKit timer would."""
    while (d := c.next_deadline()) is not None and d <= t:
        c.tick(d)
    c.tick(t)


def test_timed_states():
    c = ctl()
    c.request_added("a", 0)
    assert c.mode is Mode.EXPANDED and c.view().selected == "a"
    run_to(c, 9.9)
    assert c.mode is Mode.EXPANDED
    run_to(c, 10)
    assert c.mode is Mode.COMPACT
    run_to(c, 29.9)
    assert c.mode is Mode.COMPACT
    run_to(c, 30)
    assert c.mode is Mode.HIDDEN
    assert c.view().badge == 1  # hidden, not resolved


def test_hover_expands_and_collapses_after_one_second():
    c = ctl()
    c.request_added("a", 0)
    run_to(c, 12)
    c.pointer_entered(12)
    assert c.mode is Mode.HOVER_EXPANDED
    run_to(c, 40)  # hover holds past the 30s hide deadline
    assert c.mode is Mode.HOVER_EXPANDED
    c.pointer_exited(40)
    run_to(c, 40.9)
    assert c.mode is Mode.HOVER_EXPANDED
    run_to(c, 41)
    assert c.mode is Mode.HIDDEN  # past 30s from arrival, so deferred hide applies


def test_hover_leave_before_30s_returns_to_compact():
    c = ctl()
    c.request_added("a", 0)
    run_to(c, 12)
    c.pointer_entered(12)
    c.pointer_exited(13)
    c.pointer_entered(13.5)  # returning cancels collapse
    run_to(c, 15)
    assert c.mode is Mode.HOVER_EXPANDED
    c.pointer_exited(15)
    run_to(c, 16)
    assert c.mode is Mode.COMPACT
    run_to(c, 30)
    assert c.mode is Mode.HIDDEN


def test_dismiss_hides_without_resolving():
    c = ctl()
    c.request_added("a", 0)
    c.dismiss(1)
    v = c.view()
    assert v.mode is Mode.HIDDEN and v.badge == 1 and "a" in v.dismissed
    c.menu_clicked(2)
    assert c.mode is Mode.PINNED and c.view().selected == "a"


def test_pinned_never_auto_hides_and_escape_only_closes():
    c = ctl()
    c.request_added("a", 0)
    c.menu_clicked(1)
    run_to(c, 500)
    assert c.mode is Mode.PINNED
    c.request_added("b", 501)
    assert c.view().selected == "a"  # no abrupt selection change
    c.close(502)
    assert c.mode is Mode.HIDDEN and c.view().badge == 2


def test_arrival_mid_hover_does_not_replace_content():
    c = ctl()
    c.request_added("a", 0)
    c.pointer_entered(2)
    c.request_added("b", 3)
    v = c.view()
    assert v.selected == "a" and v.badge == 2 and v.others_waiting == 1
    run_to(c, 100)
    assert c.view().selected == "a"
    c.pointer_exited(100)
    run_to(c, 101)
    # after leaving, the queued arrival is presented fresh
    assert c.mode is Mode.EXPANDED and c.view().selected == "b"
    run_to(c, 111)
    assert c.mode is Mode.COMPACT


def test_new_arrival_restarts_timers():
    c = ctl()
    c.request_added("a", 0)
    run_to(c, 25)
    c.request_added("b", 25)
    assert c.mode is Mode.EXPANDED and c.view().selected == "b"
    run_to(c, 54)
    assert c.mode is Mode.COMPACT
    run_to(c, 55)
    assert c.mode is Mode.HIDDEN


def test_resolution_shows_next_or_hides():
    c = ctl()
    c.request_added("a", 0)
    c.request_added("b", 1)
    c.request_removed("b", 2)
    assert c.view().selected == "a" and c.mode is Mode.EXPANDED
    c.request_removed("a", 3)
    assert c.mode is Mode.HIDDEN and c.view().badge == 0


def test_interaction_lock_blocks_collapse():
    c = ctl()
    c.request_added("a", 0)
    c.interaction_began(5)
    run_to(c, 60)
    assert c.mode is Mode.EXPANDED
    c.interaction_ended(60)
    run_to(c, 61)
    assert c.mode is Mode.HIDDEN


def test_badge_only_mode():
    c = ctl(badge_only=True)
    c.request_added("a", 0)
    assert c.mode is Mode.HIDDEN and c.view().badge == 1


def test_menu_click_toggles():
    c = ctl()
    c.menu_clicked(0)
    assert c.mode is Mode.PINNED and c.view().selected is None
    c.menu_clicked(1)
    assert c.mode is Mode.HIDDEN


def test_removed_in_pinned_selects_oldest():
    c = ctl()
    for i, r in enumerate("abc"):
        c.request_added(r, i)
    c.menu_clicked(5)
    c.select("b", 6)
    c.request_removed("b", 7)
    assert c.view().selected == "a"
