"""Run a callable on the main thread after a delay, even while a modal dialog is open."""

from __future__ import annotations

from Foundation import NSRunLoop, NSRunLoopCommonModes, NSThread, NSTimer
from PyObjCTools import AppHelper


def call_later(delay: float, function, *args) -> None:
    """Like AppHelper.callLater, which is built on performSelector:afterDelay:
    and therefore fires only in the default run-loop mode. Global hotkeys keep
    working while an alert or file panel is open, so a dictation started
    behind one needs its level meter, disconnect watchdog, and auto-hide
    timers to keep running: these timers are scheduled in the common modes."""

    def schedule() -> None:
        timer = NSTimer.timerWithTimeInterval_repeats_block_(delay, False, lambda _timer: function(*args))
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)

    if NSThread.isMainThread():
        schedule()
    else:
        AppHelper.callAfter(schedule)
