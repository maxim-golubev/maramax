"""Putting a transcript into the app the user was working in (needs Accessibility trust)."""

from __future__ import annotations

import ctypes
import os

from AppKit import (
    NSApplicationActivateIgnoringOtherApps,
    NSWorkspace,
    NSWorkspaceApplicationKey,
    NSWorkspaceDidActivateApplicationNotification,
)
from Foundation import NSOperationQueue

from .hotkeys import command_key_code

kCGHIDEventTap = 0
kCGEventFlagMaskCommand = 1 << 20


class PasteError(RuntimeError):
    pass


_core_graphics = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
)
_core_foundation = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_app_services = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

_core_graphics.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_core_graphics.CGEventCreateKeyboardEvent.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint16,
    ctypes.c_bool,
]
_core_graphics.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_core_graphics.CGEventSetFlags.restype = None
_core_graphics.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_core_graphics.CGEventPost.restype = None
_core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
_core_foundation.CFRelease.restype = None
_app_services.AXIsProcessTrusted.restype = ctypes.c_bool
_app_services.AXIsProcessTrusted.argtypes = []


def accessibility_trusted() -> bool:
    """True when this process may post keyboard events (Accessibility)."""
    return bool(_app_services.AXIsProcessTrusted())


def send_paste_keystroke() -> None:
    """Post a synthetic Cmd+V to the frontmost application, on the key that
    gives V with Command on the current layout (on Dvorak the US V key would
    be Cmd+K, a different command)."""
    key_code = command_key_code("v")
    if key_code is None:
        raise PasteError("No key gives Cmd+V on the current keyboard layout")
    events = []
    try:
        # Allocate both events before posting either: allocation failure for
        # key-up must not leave a lone key-down in the destination app.
        for key_down in (True, False):
            event = _core_graphics.CGEventCreateKeyboardEvent(None, key_code, key_down)
            if not event:
                raise PasteError("Could not create keyboard event")
            events.append(event)
            _core_graphics.CGEventSetFlags(event, kCGEventFlagMaskCommand)
        for event in events:
            _core_graphics.CGEventPost(kCGHIDEventTap, event)
    finally:
        for event in events:
            _core_foundation.CFRelease(event)


class PasteTarget:
    """The app the user was last working in, other than Maramax itself.

    Asking for the frontmost app at the moment a dictation starts is not
    enough: when a Maramax window is in front, that answer is Maramax, and
    whatever was remembered earlier may be an app the user left long ago."""

    def __init__(self, workspace=None, own_pid: int | None = None):
        self._workspace = workspace or NSWorkspace.sharedWorkspace()
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._last_other_app = None
        self._note(self._workspace.frontmostApplication())
        self._observer = self._workspace.notificationCenter().addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidActivateApplicationNotification, None, NSOperationQueue.mainQueue(), self._activated,
        )

    def _activated(self, notification) -> None:
        self._note((notification.userInfo() or {}).get(NSWorkspaceApplicationKey))

    def _note(self, app) -> None:
        if app is not None and app.processIdentifier() != self._own_pid:
            self._last_other_app = app

    def current(self):
        """The app to paste into, or None if there has not been one."""
        return self._last_other_app

    def is_frontmost(self, app) -> bool:
        front = self._workspace.frontmostApplication()
        return app is not None and front is not None and front.processIdentifier() == app.processIdentifier()

    @staticmethod
    def bring_forward(app) -> None:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)

    def stop(self) -> None:
        if self._observer is not None:
            self._workspace.notificationCenter().removeObserver_(self._observer)
            self._observer = None
