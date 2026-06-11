from __future__ import annotations

import ctypes

kCGHIDEventTap = 0
kCGEventFlagMaskCommand = 1 << 20
kVK_ANSI_V = 9


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
    """Post a synthetic Cmd+V to the frontmost application."""
    for key_down in (True, False):
        event = _core_graphics.CGEventCreateKeyboardEvent(None, kVK_ANSI_V, key_down)
        if not event:
            raise PasteError("Could not create keyboard event")
        _core_graphics.CGEventSetFlags(event, kCGEventFlagMaskCommand)
        _core_graphics.CGEventPost(kCGHIDEventTap, event)
        _core_foundation.CFRelease(event)
