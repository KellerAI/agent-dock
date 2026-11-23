"""macOS menu-bar app: NSStatusItem badge + non-activating drop-down panel.

Threading: AppKit runs on the main thread. Broker events arrive on the broker
loop thread and are marshalled here with ``AppHelper.callAfter``. Decisions go
back through ``BrokerThread.submit``; a card disappears only when the broker
confirms the request left the pending state (RequestRemoved).
"""

from __future__ import annotations

import logging
import subprocess
import time

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseDown,
    NSEventMaskRightMouseUp,
    NSEventModifierFlagControl,
    NSEventTypeRightMouseDown,
    NSEventTypeRightMouseUp,
    NSImage,
    NSImageLeft,
    NSMenu,
    NSMenuItem,
    NSScreen,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from PyObjCTools import AppHelper

from ..broker.registry import RequestAdded, RequestRemoved
from ..broker.server import BrokerThread, TitleUpdated
from ..core.models import Action, Decision, Envelope
from ..settings import Settings
from . import viewmodel as vm
from . import widgets as w
from .notification_state import Mode, PresentationController

log = logging.getLogger(__name__)

WIDTH = 440.0
COMPACT_WIDTH = 380.0
TOP_GAP = 6.0


class DockController:
    def __init__(self, bt: BrokerThread, settings: Settings):
        self.bt = bt
        self.settings = settings
        self.ctl = PresentationController(
            expanded_seconds=settings.expanded_seconds,
            visible_seconds=settings.visible_seconds,
            hover_leave_seconds=settings.hover_leave_seconds,
            badge_only=settings.badge_only,
        )
        self.envelopes: dict[str, Envelope] = {}
        self.titles: dict[tuple[str, str], str] = {}
        self.sections: dict[str, str] = {}  # request_id -> "deny" | "always"
        self.drafts: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.inflight: set[str] = set()
        self._locked = False
        self._keep: list = []  # retains ObjC targets for the current render
        self._signature: object = None
        self._monitor = None
        self._note_field = None
        self._render_pending = False
        self._was_pinned = False

        self.panel = w.make_panel()
        self.panel.on_escape = self.close_panel
        self.ticker = w.Ticker.alloc().initWithCallback_(self.on_tick)

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        btn = self.status_item.button()
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "hand.raised", "Agent Dock"
        )
        if img is not None:
            img.setTemplate_(True)
            btn.setImage_(img)
            btn.setImagePosition_(NSImageLeft)
        else:
            btn.setTitle_("◈")
        self._status_target = w.ActionTarget.alloc().initWithCallback_(self.on_status_click)
        btn.setTarget_(self._status_target)
        btn.setAction_("fire:")
        btn.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        self._menu_keep: list = []
        self._update_status()

    # ---- broker events (main thread) -----------------------------------------
    def on_broker_event(self, ev: object) -> None:
        now = time.monotonic()
        if isinstance(ev, RequestAdded):
            if ev.request_id in self.envelopes:
                return
            self.envelopes[ev.request_id] = ev.envelope
            ident = self.bt.broker.identity_for(ev.envelope.agent, ev.envelope.session_id)
            if ident and ident.display_title:
                self.titles.setdefault(
                    (ev.envelope.agent, ev.envelope.session_id), ident.display_title
                )
            self.ctl.request_added(ev.request_id, now)
        elif isinstance(ev, RequestRemoved):
            self.envelopes.pop(ev.request_id, None)
            self.sections.pop(ev.request_id, None)
            self.drafts.pop(ev.request_id, None)
            self.errors.pop(ev.request_id, None)
            self.inflight.discard(ev.request_id)
            self.ctl.request_removed(ev.request_id, now)
        elif isinstance(ev, TitleUpdated):
            if ev.identity.display_title:
                self.titles[(ev.provider, ev.session_id)] = ev.identity.display_title
        self.render()

    # ---- user input -------------------------------------------------------------
    def on_status_click(self) -> None:
        event = NSApplication.sharedApplication().currentEvent()
        if event is not None and (
            event.type() in (NSEventTypeRightMouseUp, NSEventTypeRightMouseDown)
            or event.modifierFlags() & NSEventModifierFlagControl
        ):
            self._show_status_menu()
            return
        self._clear_sections()
        self.ctl.menu_clicked(time.monotonic())
        self.render()

    def _show_status_menu(self) -> None:
        menu = NSMenu.alloc().init()
        self._menu_keep = []
        for title, cb in (
            ("Open inbox", self.open_inbox),
            ("Hide panel", self.close_panel),
            (None, None),
            ("Quit Agent Dock", self.quit),
        ):
            if title is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "fire:", "")
            item.setTarget_(w.target(cb, self._menu_keep))
            menu.addItem_(item)
        self.status_item.setMenu_(menu)
        self.status_item.button().performClick_(None)
        self.status_item.setMenu_(None)

    def open_inbox(self) -> None:
        if self.ctl.mode is not Mode.PINNED:
            self._clear_sections()
            self.ctl.menu_clicked(time.monotonic())
        self.render()

    def close_panel(self) -> None:
        self._clear_sections()
        self.ctl.close(time.monotonic())
        self.render()

    def quit(self) -> None:
        shutdown(self.bt)

    def on_enter(self) -> None:
        self.ctl.pointer_entered(time.monotonic())
        self.render()

    def on_exit(self) -> None:
        self.ctl.pointer_exited(time.monotonic())
        self.render()

    def on_chip_click(self) -> None:
        if self.ctl.mode in (Mode.COMPACT, Mode.HOVER_EXPANDED, Mode.EXPANDED):
            self.open_inbox()

    def on_tick(self) -> None:
        self.ctl.tick(time.monotonic())
        self.render()

    def dismiss(self, rid: str) -> None:
        self._close_section(rid)
        self.ctl.dismiss(time.monotonic(), rid)
        self.render()

    def select(self, rid: str) -> None:
        self.ctl.select(rid, time.monotonic())
        self.render()

    def toggle_section(self, rid: str, name: str) -> None:
        if self.sections.get(rid) == name:
            self._close_section(rid)
        else:
            self.sections[rid] = name
        self._sync_lock()
        self.render()
        if self.sections.get(rid) == "deny":
            # Runs after the queued render, which creates the field.
            AppHelper.callAfter(self._focus_note_field)

    def _clear_sections(self) -> None:
        """Closing/hiding the panel abandons open sub-forms (the controller resets its lock)."""
        self.sections.clear()
        self._locked = False
        self._note_field = None

    def _close_section(self, rid: str) -> None:
        self.sections.pop(rid, None)
        self._sync_lock()

    def _sync_lock(self) -> None:
        want = bool(self.sections)
        now = time.monotonic()
        if want and not self._locked:
            self.ctl.interaction_began(now)
        elif not want and self._locked:
            self.ctl.interaction_ended(now)
        self._locked = want

    def decide(self, rid: str, action: Action, **kw) -> None:
        if rid in self.inflight or rid not in self.envelopes:
            return
        self.inflight.add(rid)
        self.errors.pop(rid, None)
        if action is Action.DENY and kw.get("message") is None and self.drafts.get(rid):
            kw["message"] = self.drafts[rid]
        fut = self.bt.submit(Decision(rid, action, **kw))
        fut.add_done_callback(lambda f: AppHelper.callAfter(self._decided, rid, f))
        self.render()

    def _decided(self, rid: str, fut) -> None:
        self.inflight.discard(rid)
        try:
            result = fut.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("decision submit failed")
            self.errors[rid] = f"Could not deliver the answer ({exc})."
        else:
            if not result.ok:
                self.errors[rid] = vm.delivery_error(result.reason)
            else:
                self._close_section(rid)
        self.render()

    def open_terminal(self, rid: str) -> None:
        env = self.envelopes.get(rid)
        if env is None:
            return
        c = self._card(env)
        try:
            if c.terminal_bundle:
                subprocess.Popen(["/usr/bin/open", "-b", c.terminal_bundle])
            elif c.cwd:
                NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(c.cwd, "")
        except OSError:
            log.warning("could not open terminal for %s", rid[:8])

    # ---- rendering -----------------------------------------------------------------
    def _card(self, env: Envelope, *, reveal: bool = True) -> vm.CardVM:
        view = self.ctl.view()
        return vm.card(
            env,
            title=self.titles.get((env.agent, env.session_id)),
            dismissed=env.request_id in view.dismissed,
            reveal_detail=reveal or not self.settings.sensitive_mode,
        )

    def _update_status(self) -> None:
        n = self.ctl.view().badge
        btn = self.status_item.button()
        btn.setTitle_(f" {vm.badge_text(n)}" if n else "")
        btn.setAccessibilityLabel_(vm.status_accessibility(n))
        btn.setToolTip_(vm.status_accessibility(n))

    def render(self) -> None:
        """Coalesce renders and never rebuild views inside the callback of a view being replaced."""
        if not self._render_pending:
            self._render_pending = True
            AppHelper.callAfter(self._render_now)

    def _render_now(self) -> None:
        self._render_pending = False
        view = self.ctl.view()
        self._update_status()
        deadline = self.ctl.next_deadline()
        self.ticker.schedule(None if deadline is None else deadline - time.monotonic())

        if view.mode is Mode.HIDDEN or (view.mode.transient and view.selected is None):
            self._signature = None
            self._hide_panel()
            return

        sig = (
            view,
            tuple(sorted(self.titles.items())),
            tuple(sorted(self.sections.items())),
            tuple(sorted(self.errors.items())),
            frozenset(self.inflight),
        )
        if sig == self._signature and self.panel.isVisible():
            return
        self._signature = sig

        old_keep = self._keep
        self._keep = []
        self._note_field = None
        if view.mode is Mode.COMPACT:
            content, width = self._build_compact(view), COMPACT_WIDTH
        elif view.mode is Mode.PINNED:
            content, width = self._build_inbox(view), WIDTH
        else:
            content, width = self._build_expanded(view), WIDTH
        self._install(content, width, pinned=view.mode is Mode.PINNED)
        del old_keep  # previous targets may be released now that views are gone
        self._reconcile_pointer()

    def _reconcile_pointer(self) -> None:
        """Rebuilding views can drop a mouseExited; re-check the real pointer position."""
        if not self.ctl._pointer_inside or not self.panel.isVisible():
            return
        loc = NSEvent.mouseLocation()
        f = self.panel.frame()
        inside = (
            f.origin.x <= loc.x <= f.origin.x + f.size.width
            and f.origin.y <= loc.y <= f.origin.y + f.size.height
        )
        if not inside:
            self.on_exit()

    def _hide_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        self.panel.allow_key = False
        self._was_pinned = False
        if self.sections:
            self._clear_sections()
        self._remove_click_monitor()

    def _install(self, content, width: float, *, pinned: bool) -> None:
        root = w.TrackingView.alloc().initWithFrame_(((0, 0), (width, 100)))
        root.on_enter = self.on_enter
        root.on_exit = self.on_exit
        root.on_click = self.on_chip_click if not pinned else None
        bg = w.effect_background()
        root.addSubview_(bg)
        w.pin_edges(bg, root)
        bg.addSubview_(content)
        w.pin_edges(content, bg)
        content.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        root.setAccessibilityRole_("AXGroup")
        root.setAccessibilityLabel_("Agent Dock")

        self.panel.setContentView_(root)
        root.layoutSubtreeIfNeeded()
        size = root.fittingSize()
        frame = self._frame_for(width, size.height)
        self.panel.setFrame_display_(frame, True)

        if pinned:
            self.panel.allow_key = True
            if not self._was_pinned:
                # Only the user's click on the menu-bar icon may take keyboard focus.
                self.panel.makeKeyAndOrderFront_(None)
            else:
                self.panel.orderFrontRegardless()
            self._was_pinned = True
            self._install_click_monitor()
        else:
            self._was_pinned = False
            # orderFrontRegardless never activates the app or moves Spaces.
            self.panel.allow_key = bool(self.sections)
            self.panel.orderFrontRegardless()
            self._remove_click_monitor()

    def _frame_for(self, width: float, height: float):
        screen = self._target_screen()
        vf = screen.visibleFrame()
        x = vf.origin.x + (vf.size.width - width) / 2.0
        y = vf.origin.y + vf.size.height - height - TOP_GAP
        return ((x, y), (width, height))

    def _target_screen(self):
        screens = list(NSScreen.screens())
        if self.settings.display == "primary" and screens:
            return screens[0]
        if self.settings.display == "mouse":
            loc = NSEvent.mouseLocation()
            for s in screens:
                f = s.frame()
                if (
                    f.origin.x <= loc.x < f.origin.x + f.size.width
                    and f.origin.y <= loc.y < f.origin.y + f.size.height
                ):
                    return s
        return NSScreen.mainScreen() or screens[0]

    def _install_click_monitor(self) -> None:
        if self._monitor is not None:
            return

        def handler(event):
            # Clicks in *other* apps close the pinned inbox (never a decision).
            AppHelper.callAfter(self.close_panel)

        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, handler
        )

    def _remove_click_monitor(self) -> None:
        if self._monitor is not None:
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None

    def _focus_note_field(self) -> None:
        field = self._note_field
        if field is None or not self.panel.isVisible():
            return
        self.panel.allow_key = True
        self.panel.makeKeyWindow()
        self.panel.makeFirstResponder_(field)
        editor = field.currentEditor()
        if editor is not None:  # caret at end instead of select-all, so typing keeps the draft
            editor.setSelectedRange_((len(field.stringValue()), 0))

    # ---- view builders ------------------------------------------------------------
    def _build_compact(self, view):
        env = self.envelopes.get(view.selected or "")
        c = self._card(env, reveal=False) if env else None
        text = w.label(vm.compact_line(c, view.badge), size=12.5, bold=True)
        dot = w.label("●", size=10.0)
        close = w.button(
            "✕",
            lambda rid=view.selected: self.dismiss(rid),
            self._keep,
            link=True,
            a11y="Dismiss notification (does not answer the request)",
        )
        row = w.hstack([dot, text, w.spacer(), close], width=COMPACT_WIDTH - 24)
        return w.vstack([row], insets=(8, 14, 8, 10))

    def _build_expanded(self, view):
        env = self.envelopes.get(view.selected or "")
        if env is None:
            return w.vstack(
                [w.label("Request no longer pending", secondary=True)], insets=(12, 14, 12, 14)
            )
        reveal = view.mode is Mode.HOVER_EXPANDED
        views = self._card_views(env, view, inner_width=WIDTH - 28, reveal=reveal, transient=True)
        if view.others_waiting:
            views.append(
                w.button(
                    f"{vm.others_waiting_text(view.others_waiting)} — open inbox",
                    self.open_inbox,
                    self._keep,
                    link=True,
                )
            )
        return w.vstack(views, spacing=7, insets=(12, 14, 12, 14))

    def _build_inbox(self, view):
        header = w.hstack(
            [
                w.label("Agent Dock", size=13, bold=True),
                w.label(vm.inbox_header(view.badge), secondary=True),
                w.spacer(),
                w.button("Hide panel", self.close_panel, self._keep, link=True, key="\x1b"),
            ],
            width=WIDTH - 28,
        )
        # Leave room for an always-visible scroller so cards never overflow the clip view.
        card_inner = WIDTH - 28 - w.scroller_width() - 16
        cards = []
        for rid in view.pending:
            env = self.envelopes.get(rid)
            if env is None:
                continue
            cv = w.vstack(
                self._card_views(env, view, inner_width=card_inner, reveal=True, transient=False),
                spacing=6,
                insets=(10, 8, 10, 8),
            )
            cv.setWantsLayer_(True)
            cv.layer().setCornerRadius_(10.0)
            if rid == view.selected:
                cv.layer().setBorderWidth_(1.5)
                cv.layer().setBorderColor_(NSColor.controlAccentColor().CGColor())
            cards.append(cv)
        if not cards:
            cards.append(w.label("Nothing is waiting for approval.", secondary=True))
        body = w.vstack(cards, spacing=10)
        screen_h = self._target_screen().visibleFrame().size.height
        scroll = w.scroll_container(body, WIDTH - 28, max(200.0, screen_h * 0.7))
        return w.vstack([header, scroll], spacing=10, insets=(12, 14, 12, 14))

    def _card_views(self, env: Envelope, view, *, inner_width, reveal, transient) -> list:
        rid = env.request_id
        c = self._card(env, reveal=reveal)
        busy = rid in self.inflight
        k = self._keep
        lines = vm.DETAIL_LINES_TRANSIENT if transient else vm.DETAIL_LINES_INBOX

        title_row = w.hstack(
            [
                w.label(c.title, size=13.5, bold=True, width=inner_width - 40),
                w.spacer(),
                w.button(
                    "✕",
                    lambda: self.dismiss(rid),
                    k,
                    link=True,
                    a11y="Dismiss (hide without answering)",
                ),
            ],
            width=inner_width,
        )
        sub = c.subtitle + ("  ·  dismissed" if c.dismissed and not transient else "")
        views = [title_row, w.label(sub, size=11, secondary=True, width=inner_width)]
        views.append(w.code_box(c.detail, inner_width, lines))
        if c.detail_truncated:
            views.append(w.label("Preview truncated.", size=10.5, secondary=True))

        is_selected_in_inbox = view.mode is Mode.PINNED and rid == view.selected
        allow = w.button(
            "Allow once",
            lambda: self.decide(rid, Action.ALLOW_ONCE),
            k,
            primary=True,
            enabled=not busy,
            key="\r" if is_selected_in_inbox else None,
            a11y=f"Allow once: {c.summary}",
        )
        deny = w.button("Deny", lambda: self.decide(rid, Action.DENY), k, enabled=not busy)
        row = [allow, deny]
        if not transient and rid != view.selected:
            row.append(w.button("Select", lambda: self.select(rid), k, link=True))
        views.append(w.hstack(row + [w.spacer()], width=inner_width))

        extras = [
            w.button("Deny with note…", lambda: self.toggle_section(rid, "deny"), k, link=True),
        ]
        if c.can_allow_always:
            extras.append(
                w.button("Always allow…", lambda: self.toggle_section(rid, "always"), k, link=True)
            )
        extras.append(
            w.button(
                "Answer in terminal",
                lambda: self.decide(rid, Action.FALLBACK),
                k,
                link=True,
                enabled=not busy,
            )
        )
        if c.terminal_bundle or c.cwd:
            extras.append(
                w.button("Open terminal ↗", lambda: self.open_terminal(rid), k, link=True)
            )
        views.append(w.hstack(extras + [w.spacer()], spacing=10, width=inner_width))

        section = self.sections.get(rid)
        if section == "deny":
            field = w.text_input(
                "Tell the agent why (optional)",
                self.drafts.get(rid, ""),
                k,
                on_text=lambda s: self.drafts.__setitem__(rid, s),
                on_submit=lambda: self.decide(rid, Action.DENY),
            )
            field.widthAnchor().constraintEqualToConstant_(inner_width).setActive_(True)
            self._note_field = field
            views.append(field)
            views.append(
                w.hstack(
                    [
                        w.button(
                            "Send deny", lambda: self.decide(rid, Action.DENY), k, enabled=not busy
                        ),
                        w.button("Cancel", lambda: self.toggle_section(rid, "deny"), k, link=True),
                        w.spacer(),
                    ],
                    width=inner_width,
                )
            )
        elif section == "always":
            views.append(
                w.label(
                    "Saves a rule Claude Code proposed for this request. Review the exact rule and where it is stored:",
                    size=11,
                    secondary=True,
                    lines=3,
                    width=inner_width,
                )
            )
            for s in c.suggestions:
                views.append(
                    w.label(s.label, size=11.5, lines=3, width=inner_width, selectable=True)
                )
                views.append(
                    w.button(
                        "Save rule and allow",
                        lambda idx=s.index: self.decide(
                            rid, Action.ALLOW_ALWAYS, suggestion_index=idx
                        ),
                        k,
                        small=True,
                        enabled=not busy,
                    )
                )
            views.append(
                w.button("Cancel", lambda: self.toggle_section(rid, "always"), k, link=True)
            )

        if rid in self.errors:
            views.append(
                w.label(self.errors[rid], size=11, secondary=True, lines=3, width=inner_width)
            )
        return views

    # ---- startup sync ------------------------------------------------------------------
    def replay_existing(self) -> None:
        """Requests that arrived before the UI attached (dedupe makes replays harmless)."""

        def collect():
            for rec in self.bt.broker.registry.actionable():
                ev = RequestAdded(
                    request_id=rec.request_id,
                    envelope=rec.envelope,
                    seq=rec.seq,
                    received_at=rec.received_at,
                    deadline_at=rec.deadline_at,
                )
                AppHelper.callAfter(self.on_broker_event, ev)

        self.bt.call(collect)


def run_app(bt: BrokerThread, settings: Settings) -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    controller = DockController(bt, settings)
    bt.broker.add_observer(lambda ev: AppHelper.callAfter(controller.on_broker_event, ev))
    controller.replay_existing()

    try:
        import signal

        from PyObjCTools import MachSignals

        # runEventLoop(installInterrupt=True) is a no-op once NSApp exists, so do it here.
        for sig in (signal.SIGTERM, signal.SIGINT):
            MachSignals.signal(sig, lambda _sig: shutdown(bt))
    except Exception:  # noqa: BLE001
        log.warning("could not install signal handlers", exc_info=True)

    def on_unexpected_error() -> bool:
        # Default shows a modal alert that activates the app; log and keep running instead.
        log.exception("unexpected error in UI callback")
        return True

    log.info("Agent Dock menu-bar UI running")
    AppHelper.runEventLoop(unexpectedErrorAlert=on_unexpected_error)


def shutdown(bt: BrokerThread) -> None:
    """Stop the broker (expires pending requests -> agents show native prompts, socket removed)
    before AppKit terminates the process with exit(), which skips Python finally blocks."""
    try:
        bt.stop()
    finally:
        AppHelper.stopEventLoop()
