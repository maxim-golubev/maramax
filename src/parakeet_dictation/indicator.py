"""A passive dictation bar. Showing it never activates Maramax."""

from __future__ import annotations

import objc
import warnings
from AppKit import (
    NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor, NSFont, NSFontWeightSemibold,
    NSLineCapStyleRound, NSMakeRect, NSPanel, NSScreen, NSStatusWindowLevel, NSTextField, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

WIDTH = 380
HEIGHT = 64
MARGIN = 18
BUTTON = 30
BUTTON_GAP = 8
METER_BARS = 7


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
        self.layer().setBorderWidth_(1)

        def apply_color():
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
                self.layer().setBackgroundColor_(NSColor.windowBackgroundColor().CGColor())
                self.layer().setBorderColor_(NSColor.separatorColor().CGColor())

        self.effectiveAppearance().performAsCurrentDrawingAppearance_(apply_color)


class RoundIconButton(NSButton):
    """A circular button whose glyph is drawn as geometry, so it is centred
    by construction rather than by the metrics of a font's symbol glyph."""

    def initWithFrame_(self, frame):
        self = objc.super(RoundIconButton, self).initWithFrame_(frame)
        if self is not None:
            self.kind = "stop"
            self.setBordered_(False)
            self.setTitle_("")
        return self

    @objc.python_method
    def set_kind(self, kind, label):
        self.kind = kind
        self.setToolTip_(label)
        self.setAccessibilityLabel_(label)
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        del rect
        bounds = self.bounds()
        side = min(bounds.size.width, bounds.size.height)
        x = bounds.origin.x + (bounds.size.width - side) / 2
        y = bounds.origin.y + (bounds.size.height - side) / 2
        cx, cy = x + side / 2, y + side / 2
        pressed = bool(self.cell().isHighlighted())
        stop = self.kind == "stop"
        fill = NSColor.systemRedColor() if stop else NSColor.labelColor().colorWithAlphaComponent_(0.10)
        if pressed:
            fill = fill.blendedColorWithFraction_ofColor_(0.25, NSColor.blackColor()) or fill
        if not self.isEnabled():
            fill = fill.colorWithAlphaComponent_(0.35)
        fill.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, side, side)).fill()

        ink = NSColor.whiteColor() if stop else NSColor.labelColor()
        if stop:
            half = side * 0.17
            ink.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(cx - half, cy - half, half * 2, half * 2), 2, 2,
            ).fill()
            return
        ink.setStroke()
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(1.8)
        path.setLineCapStyle_(NSLineCapStyleRound)
        reach = side * 0.16
        if self.kind == "close":
            path.moveToPoint_((cx - reach, cy - reach))
            path.lineToPoint_((cx + reach, cy + reach))
            path.moveToPoint_((cx - reach, cy + reach))
            path.lineToPoint_((cx + reach, cy - reach))
        else:  # "expand": an arrow to the upper right
            up = -reach if self.isFlipped() else reach  # Controls draw with y pointing down.
            path.moveToPoint_((cx - reach, cy - up))
            path.lineToPoint_((cx + reach, cy + up))
            path.moveToPoint_((cx - reach * 0.35, cy + up))
            path.lineToPoint_((cx + reach, cy + up))
            path.lineToPoint_((cx + reach, cy - up * 0.35))
        path.stroke()


class InputLevelView(NSView):
    """Recent input level as a short row of bars; the newest is on the right."""

    def initWithFrame_(self, frame):
        self = objc.super(InputLevelView, self).initWithFrame_(frame)
        if self is not None:
            self.levels = [0.0] * METER_BARS
            self.setAccessibilityLabel_("Microphone input level")
        return self

    @objc.python_method
    def push(self, level):
        self.levels = (self.levels + [max(0.0, min(1.0, float(level)))])[-METER_BARS:]
        self.setNeedsDisplay_(True)

    @objc.python_method
    def reset(self):
        self.levels = [0.0] * METER_BARS
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        del rect
        height = self.bounds().size.height
        for index, level in enumerate(self.levels):
            bar = max(4.0, level * height)
            color = NSColor.systemGreenColor() if level > 0.02 else NSColor.tertiaryLabelColor()
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(index * 6, (height - bar) / 2, 3, bar), 1.5, 1.5,
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
            NSMakeRect(0, 0, WIDTH, HEIGHT),
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
        content = IndicatorBackground.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        content.refresh_background()
        self.panel.setContentView_(content)

        # One row, everything centred on the bar's horizontal axis.
        meter_width = METER_BARS * 6 - 3
        self.meter = InputLevelView.alloc().initWithFrame_(NSMakeRect(MARGIN, (HEIGHT - 24) / 2, meter_width, 24))
        expand_x = WIDTH - MARGIN - BUTTON
        stop_x = expand_x - BUTTON_GAP - BUTTON
        button_y = (HEIGHT - BUTTON) / 2
        self.stop_button = self._button(NSMakeRect(stop_x, button_y, BUTTON, BUTTON), "stop:")
        self.expand_button = self._button(NSMakeRect(expand_x, button_y, BUTTON, BUTTON), "expand:")
        self.expand_button.set_kind("expand", "Open transcript and controls")
        self._text_right = stop_x - 12
        self._text_left_with_meter = MARGIN + meter_width + 12
        self.title = self._label(13, NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        self.detail = self._label(11, NSFont.systemFontOfSize_(11))
        self.detail.setTextColor_(NSColor.secondaryLabelColor())
        self._layout_text(True)
        for view in (self.meter, self.title, self.detail, self.stop_button, self.expand_button):
            content.addSubview_(view)
        return self

    @objc.python_method
    def _label(self, size, font):
        del size
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setFont_(font)
        label.setLineBreakMode_(4)  # truncate tail; tooltip keeps the full message
        return label

    @objc.python_method
    def _layout_text(self, with_meter):
        left = self._text_left_with_meter if with_meter else MARGIN
        width = self._text_right - left
        # Title (18) over detail (15) with a 1 pt gap, centred as a block.
        bottom = (HEIGHT - 34) / 2
        self.detail.setFrame_(NSMakeRect(left, bottom, width, 15))
        self.title.setFrame_(NSMakeRect(left, bottom + 16, width, 18))
        self.meter.setHidden_(not with_meter)

    @objc.python_method
    def _button(self, frame, action):
        button = RoundIconButton.alloc().initWithFrame_(frame)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.python_method
    def show(self):
        self._token += 1
        self._finished = False
        self.stop_button.set_kind("stop", "Finish dictation (Option+Space or Cmd+R)")
        self.stop_button.setEnabled_(True)
        self.meter.reset()
        self._layout_text(True)
        self.set_status("Connecting microphone…")
        self.detail.setStringValue_("Option+Space or Cmd+R to finish")
        screen = NSScreen.mainScreen()
        if screen is not None:
            visible = screen.visibleFrame()
            self.panel.setFrame_display_(NSMakeRect(
                visible.origin.x + (visible.size.width - WIDTH) / 2,
                visible.origin.y + 24, WIDTH, HEIGHT,
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
        self.meter.push(snapshot.level)

    @objc.python_method
    def set_transcribing(self):
        self.set_status("Transcribing…")
        self.detail.setStringValue_("Your audio is saved")
        self._layout_text(False)
        self.stop_button.set_kind("close", "Cancel transcription; captured audio is retained")

    @objc.python_method
    def finish(self, message, duration=8):
        self._finished = True
        self.set_status(message)
        self.detail.setStringValue_("Open Maramax for transcript and recordings")
        self._layout_text(False)
        self.stop_button.set_kind("close", "Dismiss")
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
