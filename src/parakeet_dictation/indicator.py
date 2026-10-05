"""The compact dictation bar, which never takes focus from the app being typed in."""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from enum import StrEnum

import objc
from AppKit import (
    NSApplicationDidChangeScreenParametersNotification, NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor, NSEvent, NSFont, NSFontWeightSemibold,
    NSLineBreakByTruncatingTail, NSLineCapStyleRound, NSMakeRect, NSPanel, NSScreen, NSStatusWindowLevel, NSTextField, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSNotificationCenter, NSObject

from .capture import CaptureHealth
from .hotkeys import STOP
from .main_thread import call_later

WIDTH = 440
HEIGHT = 64
MARGIN = 18
# Where the bar opens until the user drags it elsewhere: centred, this far
# above the bottom of the screen's usable area.
DEFAULT_BOTTOM = 24
# A bar dropped this close to its default place goes back to it, so the
# default can be found again by hand.
SNAP_DISTANCE = 12
BUTTON = 30
BUTTON_GAP = 8
METER_BARS = 7
# While nothing said would be recorded, the meter is a slow orange wave
# instead of levels: one step per capture update (150 ms), about a second a cycle.
WAVE_STEP = 0.9
WAVE_SPREAD = 0.9
# How far the expand arrow is shifted down-left, as a share of its reach. At 0
# its outline is centred but the heavier arrowhead makes it look high and to
# the right; at 0.22 it visibly sat low and left. At 0.10 the outline and the
# ink are each off centre by under half a point, in opposite directions.
EXPAND_OPTICAL_SHIFT = 0.10
FINISHED_HINT = "Open Maramax for the transcript and recordings"
# How often the controller updates the bar while recording; the wave moves one step per update.
UPDATE_SECONDS = 0.15

# What the bar says while a dictation records, by the capture's health.
# Nothing said before the microphone delivers sound is recorded: Bluetooth
# headsets send 1.5–2.5 s of silence while they connect. Said the same way
# from the shortcut press until sound arrives (the title is orange then).
WAIT_TO_SPEAK_STATUS = "Don’t speak yet — connecting…"
HEALTH_STATUS = {
    CaptureHealth.WAITING: WAIT_TO_SPEAK_STATUS,
    CaptureHealth.RECEIVING: "Recording…",
    CaptureHealth.RECONNECTING: "Microphone lost — switching input…",
    CaptureHealth.SILENT: "No microphone signal — check your input",
    CaptureHealth.QUIET: "Microphone is silent — check your input",
    CaptureHealth.MISSING: "Microphone is not delivering audio",
    CaptureHealth.DISCONNECTED: "Microphone stopped delivering audio",
}
COPIED_STATUS = "Copied transcript to clipboard"
# How long the bar stays up after a dictation ends: briefly while it says the
# transcript was copied, long enough to read anything else.
BAR_SECONDS_AFTER_SUCCESS = 2.0
BAR_SECONDS_AFTER_PROBLEM = 8.0


def bar_origin(visible, placement: Sequence[float] | None) -> tuple[float, float]:
    """Where the bar's lower-left corner goes on a screen whose usable area is
    `visible`. `placement` is None for the default place, or where the user
    left it: the share of the room the screen leaves the bar across and up
    (0 to 1 each), so it opens whole on any display."""
    room_across, room_up = visible.size.width - WIDTH, visible.size.height - HEIGHT
    if placement is None:
        return visible.origin.x + room_across / 2, visible.origin.y + DEFAULT_BOTTOM
    across, up = placement
    return visible.origin.x + room_across * across, visible.origin.y + room_up * up


def placement_at(visible, origin: tuple[float, float]) -> tuple[float, float] | None:
    """The placement of a bar dropped with its lower-left corner at `origin`,
    as bar_origin() reads it: kept on the screen, and None (the default)
    when it was dropped beside its default place."""
    default_x, default_y = bar_origin(visible, None)
    if math.hypot(origin[0] - default_x, origin[1] - default_y) <= SNAP_DISTANCE:
        return None

    def share(offset: float, room: float) -> float:
        return min(1.0, max(0.0, offset / room)) if room > 0 else 0.5

    return (share(origin[0] - visible.origin.x, visible.size.width - WIDTH),
            share(origin[1] - visible.origin.y, visible.size.height - HEIGHT))


def split_status(message: str) -> tuple[str, str]:
    """A finished bar has two lines and no meter: put the outcome on the
    first and the explanation on the second, so a long status is read in
    full instead of being cut off."""
    cuts = [(message.index(separator), separator) for separator in (" — ", ": ") if separator in message]
    if not cuts:
        return message, FINISHED_HINT
    at, separator = min(cuts)  # Whichever comes first ends the outcome.
    tail = message[at + len(separator):]
    return (message[:at], tail[0].upper() + tail[1:]) if tail else (message, FINISHED_HINT)


class Glyph(StrEnum):
    STOP = "stop"
    CLOSE = "close"
    EXPAND = "expand"


class PassivePanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class IndicatorBackground(NSView):
    """The bar's background, which is also where it is dragged from: anywhere
    but its two buttons. `on_drop` is told when a drag that moved it ends."""

    def initWithFrame_(self, frame):
        self = objc.super(IndicatorBackground, self).initWithFrame_(frame)
        if self is not None:
            self.on_drop = None
            self._grab = None  # (mouse, window origin) where the drag began
            self._moved = False
        return self

    def viewDidChangeEffectiveAppearance(self):
        self.refresh_background()

    def hitTest_(self, point):
        hit = objc.super(IndicatorBackground, self).hitTest_(point)
        # Labels and the meter are part of the background as far as the mouse goes.
        return hit if hit is None or isinstance(hit, NSButton) else self

    def acceptsFirstMouse_(self, event):
        return True  # The bar is never key: the first click is the drag.

    def mouseDown_(self, event):
        self.begin_drag(NSEvent.mouseLocation())

    def mouseDragged_(self, event):
        self.drag_to(NSEvent.mouseLocation())

    def mouseUp_(self, event):
        self.end_drag()

    @objc.python_method
    def is_dragged(self):
        return self._grab is not None

    @objc.python_method
    def begin_drag(self, mouse):
        origin = self.window().frame().origin
        self._grab = ((mouse.x, mouse.y), (origin.x, origin.y))
        self._moved = False

    @objc.python_method
    def drag_to(self, mouse):
        if self._grab is None:
            return
        (start_x, start_y), (origin_x, origin_y) = self._grab
        self.window().setFrameOrigin_((origin_x + mouse.x - start_x, origin_y + mouse.y - start_y))
        self._moved = True

    @objc.python_method
    def end_drag(self):
        moved, self._grab, self._moved = self._moved, None, False
        if moved and self.on_drop is not None:
            self.on_drop()

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
            self.kind = Glyph.STOP
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
        stop = self.kind is Glyph.STOP
        fill = NSColor.systemRedColor() if stop else NSColor.labelColor().colorWithAlphaComponent_(0.10)
        if pressed:
            fill = fill.blendedColorWithFraction_ofColor_(0.25, NSColor.blackColor()) or fill
        if not self.isEnabled():
            fill = fill.colorWithAlphaComponent_(0.35)
        fill.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, side, side)).fill()

        ink = NSColor.whiteColor() if stop else NSColor.labelColor()
        if stop:
            half = round(side * 0.17)  # Whole points: crisp edges, equal margins.
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
        if self.kind is Glyph.CLOSE:
            path.moveToPoint_((cx - reach, cy - reach))
            path.lineToPoint_((cx + reach, cy + reach))
            path.moveToPoint_((cx - reach, cy + reach))
            path.lineToPoint_((cx + reach, cy - reach))
        else:  # Glyph.EXPAND: an arrow to the upper right
            up = -reach if self.isFlipped() else reach  # Controls draw with y pointing down.
            # The arrowhead puts more ink in the upper right, so the shape is
            # shifted a little the other way (EXPAND_OPTICAL_SHIFT).
            cx, cy = cx - reach * EXPAND_OPTICAL_SHIFT, cy - up * EXPAND_OPTICAL_SHIFT
            path.moveToPoint_((cx - reach, cy - up))
            path.lineToPoint_((cx + reach, cy + up))
            path.moveToPoint_((cx - reach * 0.35, cy + up))
            path.lineToPoint_((cx + reach, cy + up))
            path.lineToPoint_((cx + reach, cy - up * 0.35))
        path.stroke()


def wave_levels(phase: float) -> list[float]:
    """The meter while nothing said would be recorded: a wave travelling
    left to right, from a quarter of the height to just over half."""
    return [0.25 + 0.3 * (1 + math.sin(phase - index * WAVE_SPREAD)) / 2 for index in range(METER_BARS)]


class InputLevelView(NSView):
    """Recent input level as a short row of bars, the newest on the right; an
    orange wave while the microphone does not yet deliver sound."""

    def initWithFrame_(self, frame):
        self = objc.super(InputLevelView, self).initWithFrame_(frame)
        if self is not None:
            self.levels = [0.0] * METER_BARS
            self.wave: float | None = 0.0  # The wave's phase while waiting; None once sound arrives.
            self.setAccessibilityLabel_("Microphone input level")
        return self

    @objc.python_method
    def push(self, level):
        self.wave = None
        self.levels = (self.levels + [max(0.0, min(1.0, float(level)))])[-METER_BARS:]
        self.setNeedsDisplay_(True)

    @objc.python_method
    def wait(self):
        """Nothing said now would be recorded: move the wave on a step."""
        self.wave = (self.wave or 0.0) + WAVE_STEP
        self.setNeedsDisplay_(True)

    @objc.python_method
    def reset(self):
        self.levels = [0.0] * METER_BARS
        self.wave = 0.0
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        del rect
        height = self.bounds().size.height
        waiting = self.wave is not None
        for index, level in enumerate(wave_levels(self.wave) if waiting else self.levels):
            bar = max(4.0, level * height)
            if waiting:
                color = NSColor.systemOrangeColor()
            else:
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
        content.on_drop = self._dropped
        self._background = content  # Also once take_view() has moved it out of the panel.
        self._placement = None
        self.panel.setContentView_(content)

        # One row, everything centred on the bar's horizontal axis.
        meter_width = METER_BARS * 6 - 3
        self.meter = InputLevelView.alloc().initWithFrame_(NSMakeRect(MARGIN, (HEIGHT - 24) / 2, meter_width, 24))
        expand_x = WIDTH - MARGIN - BUTTON
        stop_x = expand_x - BUTTON_GAP - BUTTON
        button_y = (HEIGHT - BUTTON) / 2
        self.stop_button = self._button(NSMakeRect(stop_x, button_y, BUTTON, BUTTON), "stop:")
        self.expand_button = self._button(NSMakeRect(expand_x, button_y, BUTTON, BUTTON), "expand:")
        self.expand_button.set_kind(Glyph.EXPAND, "Open transcript and controls")
        self._text_right = stop_x - 12
        self._text_left_with_meter = MARGIN + meter_width + 12
        self.title = self._label(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        self.detail = self._label(NSFont.systemFontOfSize_(11))
        self.detail.setTextColor_(NSColor.secondaryLabelColor())
        self._layout_text(True)
        for view in (self.meter, self.title, self.detail, self.stop_button, self.expand_button):
            content.addSubview_(view)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", NSApplicationDidChangeScreenParametersNotification, None)
        return self

    @objc.python_method
    def _label(self, font):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setFont_(font)
        label.setLineBreakMode_(NSLineBreakByTruncatingTail)  # The tooltip keeps the full message.
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
    def show(self, shortcut, placement):
        """Bring the bar up for a new dictation, at `placement` (see bar_origin)."""
        self.begin(shortcut)
        self.place(placement)
        self.panel.orderFrontRegardless()

    @objc.python_method
    def begin(self, shortcut):
        """Lay the bar out for a new dictation: the microphone is not open yet."""
        self._token += 1
        self._finished = False
        self.stop_button.set_kind(Glyph.STOP, f"Finish dictation ({shortcut} or {STOP.label})")
        self.stop_button.setEnabled_(True)
        self.meter.reset()
        self._show_waiting(True)
        self._layout_text(True)
        self.detail.setStringValue_(f"{shortcut} or {STOP.label} to finish")
        self.detail.setToolTip_(None)

    @objc.python_method
    def place(self, placement):
        """Move the bar to `placement` on the screen in use."""
        self._placement = placement
        screen = NSScreen.mainScreen()
        if screen is not None:
            self.panel.setFrameOrigin_(bar_origin(screen.visibleFrame(), placement))

    def screensChanged_(self, notification):
        """A display was unplugged or rearranged: a bar on screen that is now
        on none is put back where it belongs. AppKit leaves a borderless panel
        where it was."""
        del notification
        if self.panel.isVisible() and self.panel.screen() is None:
            self.place(self._placement)

    @objc.python_method
    def _dropped(self):
        """A drag ended: settle the bar whole on the screen it was dropped on,
        snapped back if beside its default place, and say where it is now."""
        screen = self.panel.screen() or NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()
        origin = self.panel.frame().origin
        placement = placement_at(visible, (origin.x, origin.y))
        self.panel.setFrameOrigin_(bar_origin(visible, placement))
        self.delegate.bar_moved(placement)

    @objc.python_method
    def take_view(self):
        """The bar's view, to show a bar inside another window (the welcome's
        demonstration). This bar's own panel is then never shown."""
        view = self._background
        self.panel.setContentView_(NSView.alloc().initWithFrame_(view.frame()))
        return view

    @objc.python_method
    def set_status(self, message):
        self.title.setStringValue_(message)
        self.title.setToolTip_(message)

    @objc.python_method
    def set_capture(self, snapshot):
        detail = snapshot.summary()
        self.detail.setStringValue_(detail)
        self.detail.setToolTip_(detail)
        receiving = snapshot.health is CaptureHealth.RECEIVING
        if receiving:
            self.meter.push(snapshot.level)
        else:
            self.meter.wait()
        self._show_waiting(not receiving)

    @objc.python_method
    def _show_waiting(self, waiting):
        """Orange while what is said would not be recorded: before the
        microphone opens, while a Bluetooth headset sends only silence, and
        while an input is replaced or has gone quiet."""
        self.title.setTextColor_(NSColor.systemOrangeColor() if waiting else NSColor.labelColor())

    @objc.python_method
    def set_transcribing(self):
        self._show_waiting(False)
        self.set_status("Transcribing…")
        self.detail.setStringValue_("Your audio is saved")
        self._layout_text(False)
        self.stop_button.set_kind(Glyph.CLOSE, "Cancel transcription; captured audio is retained")

    @objc.python_method
    def is_finished(self):
        return self._finished and self.panel.isVisible()

    @objc.python_method
    def finish(self, message, duration):
        """Show the outcome, then hide after `duration`. Calling it again
        with a later status restarts the countdown for that status."""
        self._finished = True
        self._token += 1
        self._show_waiting(False)
        title, detail = split_status(message)
        self.title.setStringValue_(title)
        self.title.setToolTip_(message)
        self.detail.setStringValue_(detail)
        self.detail.setToolTip_(message)
        self._layout_text(False)
        self.stop_button.set_kind(Glyph.CLOSE, "Dismiss")
        call_later(duration, self._hide_if_current, self._token)

    @objc.python_method
    def _hide_if_current(self, token):
        if token != self._token or not self._finished:
            return
        if self._background.is_dragged():
            # Not from under the pointer: the drop still has to be remembered.
            call_later(UPDATE_SECONDS, self._hide_if_current, token)
            return
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
            self.delegate.dismiss_requested()

    def expand_(self, sender):
        del sender
        self.delegate.open_transcript_window()
