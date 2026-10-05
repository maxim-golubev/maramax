"""A dictation played on the bar's own views, so the welcome can show what the first one will look like."""

from __future__ import annotations

import math
from collections.abc import Callable

from AppKit import NSBezierPath, NSColor, NSMakeRect, NSView

from .capture import CaptureSnapshot
from .indicator import (BAR_SECONDS_AFTER_SUCCESS, COPIED_STATUS, HEALTH_STATUS, HEIGHT, UPDATE_SECONDS, WIDTH,
                        DictationIndicator)
from .main_thread import call_later

# The parts of the dictation shown, as one with AirPods goes.
OPENING_SECONDS = 0.6      # the shortcut was pressed; the microphone is not open yet
CONNECTING_SECONDS = 1.6   # open, but a Bluetooth headset sends only silence while it connects
SPEAKING_SECONDS = 4.2
TRANSCRIBING_SECONDS = 0.6
# Room around the bar on the stage it is shown on.
STAGE_PADDING = 16
STAGE_WIDTH = WIDTH + 2 * STAGE_PADDING
STAGE_HEIGHT = HEIGHT + 2 * STAGE_PADDING

Frame = Callable[[DictationIndicator], None]


def capture(seconds: float, level: float, sound: bool = True) -> CaptureSnapshot:
    """What the meter reports `seconds` into a recording; without `sound`, the
    silence a microphone sends while it connects."""
    return CaptureSnapshot(elapsed=seconds, audio_seconds=seconds, first_frame_delay=0.26, last_frame_age=0.02,
                           last_signal_age=0.02 if sound else None, peak=0.4 if sound else 0.0, level=level,
                           nonzero_samples=1 if sound else 0, callbacks=1, overflow_count=0,
                           device_name="MacBook Pro Microphone")


def speech_level(seconds: float) -> float:
    """The meter's level for someone speaking steadily, shaped like the app's
    meter over real dictations: mostly between 0.5 and 0.75, moving about a
    tenth from one update to the next."""
    return 0.62 + 0.08 * math.sin(2 * math.pi * 0.5 * seconds) + 0.06 * math.sin(2 * math.pi * 1.9 * seconds + 0.7)


def _updates(seconds: float) -> int:
    return round(seconds / UPDATE_SECONDS)


def _recording(snapshot: CaptureSnapshot) -> Frame:
    def frame(bar: DictationIndicator) -> None:
        bar.set_capture(snapshot)
        bar.set_status(HEALTH_STATUS[snapshot.health])
    return frame


def _unchanged(bar: DictationIndicator) -> None:
    del bar


def _transcribing(bar: DictationIndicator) -> None:
    bar.set_transcribing()


def dictation(shortcut: str) -> list[Frame]:
    """One dictation from the shortcut press to "copied", one frame per update
    of the bar (UPDATE_SECONDS apart), as the app makes them."""
    def opening(bar: DictationIndicator) -> None:
        bar.begin(shortcut)
        bar.set_status(HEALTH_STATUS[capture(0.0, 0.0, sound=False).health])

    def finished(bar: DictationIndicator) -> None:
        bar.finish(COPIED_STATUS, BAR_SECONDS_AFTER_SUCCESS)

    speaking = [_recording(capture(step * UPDATE_SECONDS, speech_level(step * UPDATE_SECONDS)))
                for step in range(_updates(SPEAKING_SECONDS))]
    return [
        opening, *[_unchanged] * (_updates(OPENING_SECONDS) - 1),
        *[_recording(capture(0.0, 0.0, sound=False))] * _updates(CONNECTING_SECONDS),
        *speaking,
        _transcribing, *[_unchanged] * (_updates(TRANSCRIBING_SECONDS) - 1),
        finished, *[_unchanged] * (_updates(BAR_SECONDS_AFTER_SUCCESS) - 1),
    ]


class _Stage(NSView):
    """A patch of screen for the bar to sit on. It takes no clicks: the bar
    on it is only shown, never used."""

    def hitTest_(self, point):
        return None

    def drawRect_(self, rect):
        del rect
        NSColor.quaternaryLabelColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()


class _Shown:
    """The demonstration bar's delegate. The mouse never reaches its buttons,
    but VoiceOver can press them: nothing happens."""

    def dismiss_requested(self) -> None:
        pass

    def open_transcript_window(self) -> None:
        pass

    def bar_moved(self, placement) -> None:
        del placement


class Demonstration:
    """`view` shows a bar of its own playing dictation() over and over while started."""

    def __init__(self) -> None:
        self._bar = DictationIndicator.alloc().initWithDelegate_(_Shown())
        bar_view = self._bar.take_view()
        bar_view.setFrameOrigin_((STAGE_PADDING, STAGE_PADDING))
        layer = bar_view.layer()
        layer.setShadowOpacity_(0.2)
        layer.setShadowRadius_(6)
        layer.setShadowOffset_((0, -2))
        self.view = _Stage.alloc().initWithFrame_(NSMakeRect(0, 0, STAGE_WIDTH, STAGE_HEIGHT))
        self.view.addSubview_(bar_view)
        self.view.widthAnchor().constraintEqualToConstant_(STAGE_WIDTH).setActive_(True)
        self.view.heightAnchor().constraintEqualToConstant_(STAGE_HEIGHT).setActive_(True)
        self._generation = 0  # Each start() begins a new run; an older one stops at its next frame.

    def start(self, shortcut: str) -> None:
        self._generation += 1
        self._play(self._generation, dictation(shortcut), 0)

    def stop(self) -> None:
        self._generation += 1

    def _play(self, generation: int, frames: list[Frame], index: int) -> None:
        if generation != self._generation:
            return
        frames[index](self._bar)
        call_later(UPDATE_SECONDS, self._play, generation, frames, (index + 1) % len(frames))
