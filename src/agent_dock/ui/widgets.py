"""Thin PyObjC/AppKit building blocks (macOS only).

Kept deliberately small: all wording/state lives in viewmodel.py and
notification_state.py, which are unit-tested on any OS.
"""

from __future__ import annotations

from collections.abc import Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSControlSizeSmall,
    NSFont,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutPriorityDefaultLow,
    NSLineBreakByTruncatingTail,
    NSPanel,
    NSScrollView,
    NSStackView,
    NSStatusWindowLevel,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialPopover,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject

# ---- callbacks ---------------------------------------------------------------


class ActionTarget(NSObject):
    """Target/action bridge to a Python callable. Keep a reference while in use."""

    def initWithCallback_(self, cb):
        self = objc.super(ActionTarget, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    @objc.IBAction
    def fire_(self, sender):
        self._cb()

    # NSTextField delegate: keep drafts in sync without re-rendering.
    def controlTextDidChange_(self, notification):
        field = notification.object()
        on_text = getattr(self, "_on_text", None)
        if on_text is not None:
            on_text(str(field.stringValue()))


def target(cb: Callable[[], None], keep: list) -> ActionTarget:
    t = ActionTarget.alloc().initWithCallback_(cb)
    keep.append(t)
    return t


class Ticker(NSObject):
    """One cancellable one-shot NSTimer (the single presentation timer)."""

    def initWithCallback_(self, cb):
        self = objc.super(Ticker, self).init()
        if self is None:
            return None
        self._cb = cb
        self._timer = None
        return self

    @objc.python_method
    def schedule(self, delay: float | None) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        if delay is None:
            return
        from Foundation import NSTimer

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            max(0.01, delay), self, "fire:", None, False
        )

    def fire_(self, timer):
        self._timer = None
        self._cb()


# ---- windows & views ---------------------------------------------------------


class DockPanel(NSPanel):
    """Borderless, non-activating floating panel.

    It can become key only when the user deliberately asks for keyboard input
    (pinned inbox or deny-note field), so merely appearing never steals focus.
    """

    def canBecomeKeyWindow(self):
        return bool(getattr(self, "allow_key", False))

    def canBecomeMainWindow(self):
        return False

    def cancelOperation_(self, sender):
        cb = getattr(self, "on_escape", None)
        if cb is not None:
            cb()


def make_panel() -> DockPanel:
    panel = DockPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 440, 120),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.allow_key = False
    panel.setLevel_(NSStatusWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle
    )
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setBecomesKeyOnlyIfNeeded_(True)
    panel.setWorksWhenModal_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setReleasedWhenClosed_(False)
    panel.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorNone (we never animate focus)
    return panel


class TrackingView(NSView):
    """Root view that reports pointer enter/exit and clicks on empty space."""

    def updateTrackingAreas(self):
        objc.super(TrackingView, self).updateTrackingAreas()
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(area)

    def acceptsFirstMouse_(self, event):
        return True

    def mouseEntered_(self, event):
        cb = getattr(self, "on_enter", None)
        if cb:
            cb()

    def mouseExited_(self, event):
        cb = getattr(self, "on_exit", None)
        if cb:
            cb()

    def mouseDown_(self, event):
        cb = getattr(self, "on_click", None)
        if cb:
            cb()


class FlippedView(NSView):
    def isFlipped(self):
        return True


def effect_background() -> NSVisualEffectView:
    v = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    v.setMaterial_(NSVisualEffectMaterialPopover)
    v.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    v.setState_(NSVisualEffectStateActive)
    v.setWantsLayer_(True)
    v.layer().setCornerRadius_(14.0)
    v.layer().setMasksToBounds_(True)
    return v


def pin_edges(child: NSView, parent: NSView, inset: float = 0.0) -> None:
    child.setTranslatesAutoresizingMaskIntoConstraints_(False)
    child.leadingAnchor().constraintEqualToAnchor_constant_(
        parent.leadingAnchor(), inset
    ).setActive_(True)
    child.trailingAnchor().constraintEqualToAnchor_constant_(
        parent.trailingAnchor(), -inset
    ).setActive_(True)
    child.topAnchor().constraintEqualToAnchor_constant_(parent.topAnchor(), inset).setActive_(True)
    child.bottomAnchor().constraintEqualToAnchor_constant_(
        parent.bottomAnchor(), -inset
    ).setActive_(True)


def vstack(views: list, spacing: float = 6.0, insets=(0, 0, 0, 0)) -> NSStackView:
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    s.setAlignment_(NSLayoutAttributeLeading)
    s.setSpacing_(spacing)
    s.setEdgeInsets_(insets)
    return s


def hstack(views: list, spacing: float = 8.0) -> NSStackView:
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
    s.setAlignment_(NSLayoutAttributeCenterY)
    s.setSpacing_(spacing)
    return s


def spacer() -> NSView:
    v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
    v.setContentHuggingPriority_forOrientation_(NSLayoutPriorityDefaultLow - 1, 0)
    return v


def label(
    text: str,
    *,
    size: float = 12.0,
    bold: bool = False,
    secondary: bool = False,
    mono: bool = False,
    lines: int = 1,
    width: float | None = None,
    selectable: bool = False,
) -> NSTextField:
    f = (
        NSTextField.wrappingLabelWithString_(text)
        if lines != 1
        else NSTextField.labelWithString_(text)
    )
    if mono:
        f.setFont_(NSFont.monospacedSystemFontOfSize_weight_(size, NSFontWeightRegular))
    elif bold:
        f.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold))
    else:
        f.setFont_(NSFont.systemFontOfSize_(size))
    f.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    f.setMaximumNumberOfLines_(lines)
    f.setLineBreakMode_(NSLineBreakByTruncatingTail)
    if lines != 1:
        f.cell().setTruncatesLastVisibleLine_(True)
    if width is not None:
        f.setPreferredMaxLayoutWidth_(width)
        f.widthAnchor().constraintLessThanOrEqualToConstant_(width).setActive_(True)
    f.setContentHuggingPriority_forOrientation_(NSLayoutPriorityDefaultLow, 0)
    f.setContentCompressionResistancePriority_forOrientation_(NSLayoutPriorityDefaultLow, 0)
    f.setSelectable_(selectable)
    return f


def code_box(text: str, width: float, lines: int) -> NSView:
    f = label(text, size=11.0, mono=True, lines=lines, width=width - 16, selectable=True)
    box = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 20))
    box.setWantsLayer_(True)
    box.layer().setCornerRadius_(6.0)
    box.layer().setBackgroundColor_(NSColor.quaternaryLabelColor().CGColor())
    box.addSubview_(f)
    pin_edges(f, box, 8.0)
    box.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    return box


def button(
    title: str,
    cb: Callable[[], None],
    keep: list,
    *,
    primary: bool = False,
    link: bool = False,
    small: bool = False,
    enabled: bool = True,
    key: str | None = None,
    a11y: str | None = None,
) -> NSButton:
    b = NSButton.buttonWithTitle_target_action_(title, target(cb, keep), "fire:")
    if link:
        b.setBordered_(False)
        b.setContentTintColor_(NSColor.linkColor())
    if small or link:
        b.setControlSize_(NSControlSizeSmall)
        b.setFont_(NSFont.systemFontOfSize_(11.0))
    if primary:
        b.setBezelColor_(NSColor.controlAccentColor())
    if key is not None:
        b.setKeyEquivalent_(key)
    b.setEnabled_(enabled)
    if a11y:
        b.setAccessibilityLabel_(a11y)
    return b


def text_input(placeholder: str, value: str, keep: list, on_text, on_submit) -> NSTextField:
    f = NSTextField.textFieldWithString_(value)
    f.setPlaceholderString_(placeholder)
    t = target(on_submit, keep)
    t._on_text = on_text
    f.setTarget_(t)
    f.setAction_("fire:")
    f.setDelegate_(t)
    return f


def scroll_container(content: NSView, width: float, max_height: float) -> NSScrollView:
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)
    scroll.setAutohidesScrollers_(True)
    doc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
    doc.setTranslatesAutoresizingMaskIntoConstraints_(False)
    scroll.setDocumentView_(doc)
    doc.addSubview_(content)
    pin_edges(content, doc)
    clip = scroll.contentView()
    doc.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()).setActive_(True)
    doc.trailingAnchor().constraintEqualToAnchor_(clip.trailingAnchor()).setActive_(True)
    doc.topAnchor().constraintEqualToAnchor_(clip.topAnchor()).setActive_(True)
    content.layoutSubtreeIfNeeded()
    height = min(max_height, max(60.0, content.fittingSize().height))
    scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
    scroll.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    scroll.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
    return scroll
