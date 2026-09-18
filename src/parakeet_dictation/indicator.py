"""A passive dictation bar. Showing it never activates Maramax."""

from __future__ import annotations

import objc
import warnings
from AppKit import (
    NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor, NSFont, NSMakeRect,
    NSPanel, NSScreen, NSStatusWindowLevel, NSTextField, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject
from PyObjCTools import AppHelper


class PassivePanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class IndicatorBackground(NSView):
    def viewDidChangeEffectiveAppearance(self):
        self.refresh_background()

    @objc.python_method
    def refresh_background(self):
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(18)

        def apply_color():
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
                self.layer().setBackgroundColor_(NSColor.windowBackgroundColor().CGColor())

        self.effectiveAppearance().performAsCurrentDrawingAppearance_(apply_color)


class InputLevelView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(InputLevelView, self).initWithFrame_(frame)
        if self is not None:
            self.level = 0.0
            self.setAccessibilityLabel_("Microphone input level")
        return self

    def drawRect_(self, rect):
        del rect
        for index in range(12):
            active = index < round(self.level * 12)
            color = NSColor.systemGreenColor() if active else NSColor.quaternaryLabelColor()
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(index * 6, 2, 3, 8), 1.5, 1.5,
            ).fill()


class DictationIndicator(NSObject):
    def initWithDelegate_(self, delegate):
        self = objc.super(DictationIndicator, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self._token = 0
        self._finished = False
        self.panel = PassivePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 340, 70),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        content = IndicatorBackground.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 70))
        content.refresh_background()
        self.panel.setContentView_(content)
        self.title = self._label(NSMakeRect(16, 39, 230, 18), 12, True)
        self.detail = self._label(NSMakeRect(16, 20, 235, 15), 10, False)
        self.detail.setTextColor_(NSColor.secondaryLabelColor())
        self.meter = InputLevelView.alloc().initWithFrame_(NSMakeRect(16, 6, 74, 12))
        self.stop_button = self._button(NSMakeRect(254, 21, 34, 30), "■", "stop:")
        self.stop_button.setToolTip_("Stop recording (Option+Space or Cmd+R)")
        self.expand_button = self._button(NSMakeRect(292, 21, 32, 30), "↗", "expand:")
        self.expand_button.setToolTip_("Open transcript and controls")
        for view in (self.title, self.detail, self.meter, self.stop_button, self.expand_button):
            content.addSubview_(view)
        return self

    @objc.python_method
    def _label(self, frame, size, bold):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        label.setLineBreakMode_(4)  # truncate tail; tooltip keeps the full message
        return label

    @objc.python_method
    def _button(self, frame, title, action):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.python_method
    def show(self):
        self._token += 1
        self._finished = False
        self.stop_button.setTitle_("■")
        self.stop_button.setEnabled_(True)
        self.stop_button.setToolTip_("Stop recording (Option+Space or Cmd+R)")
        self.meter.setHidden_(False)
        self.set_status("Connecting microphone…")
        self.detail.setStringValue_("Option+Space or Cmd+R to finish")
        screen = NSScreen.mainScreen()
        if screen is not None:
            visible = screen.visibleFrame()
            self.panel.setFrame_display_(NSMakeRect(
                visible.origin.x + (visible.size.width - 340) / 2,
                visible.origin.y + 24, 340, 70,
            ), True)
        self.panel.orderFrontRegardless()

    @objc.python_method
    def set_status(self, message):
        self.title.setStringValue_(message)
        self.title.setToolTip_(message)

    @objc.python_method
    def set_capture(self, snapshot):
        seconds = int(snapshot.audio_seconds)
        detail = f"{snapshot.device_name} · {seconds // 60}:{seconds % 60:02d}"
        self.detail.setStringValue_(detail)
        self.detail.setToolTip_(detail)
        self.meter.level = snapshot.level
        self.meter.setNeedsDisplay_(True)

    @objc.python_method
    def set_transcribing(self):
        self.set_status("Transcribing…")
        self.meter.setHidden_(True)
        self.stop_button.setTitle_("×")
        self.stop_button.setToolTip_("Cancel transcription; captured audio is retained")

    @objc.python_method
    def finish(self, message, duration=8):
        self._finished = True
        self.set_status(message)
        self.detail.setStringValue_("Open Maramax for transcript and recovery")
        self.meter.setHidden_(True)
        self.stop_button.setTitle_("×")
        self.stop_button.setToolTip_("Dismiss")
        token = self._token
        AppHelper.callLater(duration, self._hide_if_current, token)

    @objc.python_method
    def _hide_if_current(self, token):
        if token == self._token and self._finished:
            self.hide()

    @objc.python_method
    def hide(self):
        self._token += 1
        self.panel.orderOut_(None)

    def stop_(self, sender):
        del sender
        if self._finished:
            self.hide()
        else:
            self.delegate.hide_overlay()

    def expand_(self, sender):
        del sender
        self.delegate.show_overlay()
