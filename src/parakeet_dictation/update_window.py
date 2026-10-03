"""The window that shows an update downloading, then Maramax restarting."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSFontWeightSemibold, NSImageView, NSMakeRect,
    NSPanel, NSProgressIndicator, NSProgressIndicatorStyleBar, NSTextField, NSWindowStyleMaskTitled,
)
from Foundation import NSObject

WIDTH = 480
HEIGHT = 136
MARGIN = 20
ICON = 64
# The text column starts right of the app icon, as in the Software Update window.
TEXT_LEFT = MARGIN + ICON + 16


def download_size(size: int) -> str:
    """A byte count as people read it: '200 MB', '3.4 MB', '840 KB'."""
    megabytes = size / (1024 * 1024)
    if megabytes >= 10:
        return f"{round(megabytes)} MB"
    if megabytes >= 1:
        return f"{megabytes:.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def progress_state(received: int, expected: int) -> tuple[str, float | None]:
    """What the window says and how full the bar is (None: still working,
    with no measure of how far along)."""
    if received >= expected:
        return "Checking the download…", None
    return f"{download_size(received)} of {download_size(expected)}", received / expected


class UpdateProgressWindow(NSObject):
    def initWithCancel_(self, on_cancel):
        self = objc.super(UpdateProgressWindow, self).init()
        if self is None:
            return None
        self.on_cancel = on_cancel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
        self.panel.setTitle_("Updating Maramax")
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        content = self.panel.contentView()
        inner = WIDTH - TEXT_LEFT - MARGIN
        icon = NSImageView.imageViewWithImage_(NSApplication.sharedApplication().applicationIconImage())
        icon.setFrame_(NSMakeRect(MARGIN, HEIGHT - MARGIN - ICON, ICON, ICON))
        self.title = NSTextField.labelWithString_("")
        self.title.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        self.title.setFrame_(NSMakeRect(TEXT_LEFT, HEIGHT - 40, inner, 18))
        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(TEXT_LEFT, HEIGHT - 68, inner, 20))
        self.bar.setStyle_(NSProgressIndicatorStyleBar)
        self.bar.setMinValue_(0.0)
        self.bar.setMaxValue_(1.0)
        self.detail = NSTextField.labelWithString_("")
        self.detail.setFont_(NSFont.systemFontOfSize_(11))
        self.detail.setTextColor_(NSColor.secondaryLabelColor())
        self.detail.setFrame_(NSMakeRect(TEXT_LEFT, HEIGHT - 90, inner, 16))
        self.cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "cancel:")
        self.cancel.setKeyEquivalent_("\x1b")
        size = self.cancel.fittingSize()
        self.cancel.setFrame_(NSMakeRect(WIDTH - MARGIN - size.width, 12, size.width, size.height))
        for view in (icon, self.title, self.bar, self.detail, self.cancel):
            content.addSubview_(view)
        return self

    @objc.python_method
    def show(self, version):
        self.title.setStringValue_(f"Downloading Maramax {version}…")
        self.detail.setStringValue_("Starting the download…")
        self._determinate(0.0)
        self.cancel.setEnabled_(True)
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def show_progress(self, received, expected):
        text, fraction = progress_state(received, expected)
        self.detail.setStringValue_(text)
        if fraction is None:
            self._indeterminate()
        else:
            self._determinate(fraction)

    @objc.python_method
    def show_ready(self, version, busy):
        self.title.setStringValue_(f"Maramax {version} is ready")
        self.detail.setStringValue_("Maramax restarts as soon as it is idle." if busy
                                    else "Maramax restarts in a moment.")
        # Also after "Restarting…" gave way to waiting again: the wait can still be cancelled.
        self.cancel.setEnabled_(True)
        self._indeterminate()

    @objc.python_method
    def bring_forward(self):
        if self.panel.isVisible():
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def show_restarting(self):
        self.title.setStringValue_("Restarting Maramax…")
        self.detail.setStringValue_("It opens again in a moment.")
        self.cancel.setEnabled_(False)
        self._indeterminate()

    @objc.python_method
    def close(self):
        self.bar.stopAnimation_(None)
        self.panel.orderOut_(None)

    @objc.python_method
    def _determinate(self, fraction):
        self.bar.stopAnimation_(None)
        self.bar.setIndeterminate_(False)
        self.bar.setDoubleValue_(fraction)

    @objc.python_method
    def _indeterminate(self):
        self.bar.setIndeterminate_(True)
        self.bar.startAnimation_(None)

    def cancel_(self, sender):
        del sender
        self.on_cancel()
