"""Putting a transcript into the app the user was working in (needs Accessibility trust)."""

from __future__ import annotations

import ctypes
import os
import subprocess

import objc
from AppKit import (
    NSApplication,
    NSRunningApplication,
    NSWorkspace,
    NSWorkspaceApplicationKey,
    NSWorkspaceDidActivateApplicationNotification,
)
from Foundation import NSDictionary, NSOperationQueue

from .hotkeys import command_key_code
from .logger_config import logger

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
_core_graphics.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                          ctypes.POINTER(ctypes.c_uint16)]
_core_graphics.CGEventKeyboardSetUnicodeString.restype = None
_core_graphics.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_core_graphics.CGEventPost.restype = None
_core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
_core_foundation.CFRelease.restype = None
_core_foundation.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_app_services.AXIsProcessTrusted.restype = ctypes.c_bool
_app_services.AXIsProcessTrusted.argtypes = []


_app_services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_app_services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
_app_services.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
_app_services.AXUIElementCreateSystemWide.argtypes = []
_app_services.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
_app_services.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
_app_services.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
_app_services.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                        ctypes.POINTER(ctypes.c_void_p)]
_app_services.AXUIElementCopyParameterizedAttributeValue.restype = ctypes.c_int32
_app_services.AXUIElementCopyParameterizedAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                                     ctypes.POINTER(ctypes.c_void_p)]
_app_services.AXValueGetValue.restype = ctypes.c_bool
_app_services.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_app_services.AXValueCreate.restype = ctypes.c_void_p
_app_services.AXValueCreate.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_AX_VALUE_CF_RANGE = 4  # kAXValueCFRangeType
# Bounds the one call to tccutil, which answers in milliseconds.
_TCCUTIL_SECONDS = 5
# How long the app being pasted into may take to say what is before the
# cursor: the read runs on the main thread, just before Cmd+V.
_CURSOR_READ_SECONDS = 0.25
# After one of these a transcript follows on without a space.
_OPENERS = "([{<“‘«/\\-@#"
# Straight quotes open and close alike: one after a word closes ("yes"), one
# at the start or after a space opens.
_STRAIGHT_QUOTES = "\"'"


class _CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


def _separates(character: str) -> bool:
    """Whether text after `character` is a new word: it ends one, or a sentence."""
    return not character.isspace() and character not in _OPENERS


def space_before(before: str | None) -> bool:
    """Whether a transcript pasted after `before`, the (up to) two characters
    before the cursor, needs a space first: after a word or a sentence it
    does; at the start of a field ("") or a line, after a space or an opening
    bracket or quote, it does not. None, an app that does not say, leaves it
    as it is."""
    if not before:
        return False
    last = before[-1]
    if last in _STRAIGHT_QUOTES:
        return len(before) == 2 and _separates(before[0]) and before[0] not in _STRAIGHT_QUOTES
    return _separates(last)


def accessibility_trusted() -> bool:
    """True when this process may post keyboard events (Accessibility)."""
    return bool(_app_services.AXIsProcessTrusted())


def request_accessibility(bundle_identifier: str | None) -> None:
    """Have macOS ask the user to let Maramax paste, in its own prompt, which
    also lists Maramax under Privacy & Security → Accessibility.

    A grant made for an earlier build can be recorded against that build's
    code hashes: System Settings then shows Maramax switched on while macOS
    refuses this copy, and no prompt appears. So Maramax's own entry is
    cleared first. `bundle_identifier` is None when running from source,
    where the entry would belong to Python or the terminal, not Maramax."""
    if bundle_identifier is not None:
        try:
            result = subprocess.run(["/usr/bin/tccutil", "reset", "Accessibility", bundle_identifier],
                                    capture_output=True, text=True, timeout=_TCCUTIL_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # The prompt below is still worth showing; never hang the UI on this.
            logger.warning(f"Could not clear the Accessibility entry for {bundle_identifier}: {exc}")
        else:
            if result.returncode != 0:
                logger.warning(f"tccutil reset Accessibility {bundle_identifier} failed: {result.stderr.strip()}")
    options = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    _app_services.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options))


def text_before_cursor() -> str | None:
    """The two characters just before the insertion point in the focused
    text field of the frontmost app (one right after the start of a field, ""
    at its start), or None when the app does not say (terminals and some
    Electron apps) or takes longer than a quarter of a second to."""
    owned = []
    try:
        system = _app_services.AXUIElementCreateSystemWide()
        if not system:
            return None
        owned.append(system)
        _app_services.AXUIElementSetMessagingTimeout(system, _CURSOR_READ_SECONDS)  # For every element.
        focused = _copy_attribute(system, "AXFocusedUIElement", owned)
        selection = focused and _copy_attribute(focused, "AXSelectedTextRange", owned)
        cursor = _CFRange()
        if not selection or not _app_services.AXValueGetValue(selection, _AX_VALUE_CF_RANGE, ctypes.byref(cursor)):
            return None
        if cursor.location <= 0:
            return "" if cursor.location == 0 else None
        length = min(2, cursor.location)
        before = _CFRange(cursor.location - length, length)
        span = _app_services.AXValueCreate(_AX_VALUE_CF_RANGE, ctypes.byref(before))
        if not span:
            return None
        owned.append(span)
        value = ctypes.c_void_p()
        if _app_services.AXUIElementCopyParameterizedAttributeValue(
                focused, _name("AXStringForRange", owned), span, ctypes.byref(value)) != 0 or not value.value:
            return None
        owned.append(value.value)
        text = str(objc.objc_object(c_void_p=value.value))  # The proxy keeps its own reference.
        return text if 1 <= len(text) <= 2 else None  # Two UTF-16 units can be one character.
    finally:
        for reference in owned:
            _core_foundation.CFRelease(reference)


def _copy_attribute(element, name: str, owned: list):
    """An Accessibility attribute's value, released with `owned`; None when the app has none."""
    value = ctypes.c_void_p()
    if _app_services.AXUIElementCopyAttributeValue(element, _name(name, owned), ctypes.byref(value)) != 0:
        return None
    if value.value:
        owned.append(value.value)
    return value.value


def _name(name: str, owned: list):
    """An Accessibility attribute's name as the CFString the API takes, released with `owned`."""
    reference = _core_foundation.CFStringCreateWithCString(None, name.encode(), _UTF8)
    owned.append(reference)
    return reference


def send_paste_keystroke(lead: str = "") -> None:
    """Type `lead` (a space between this transcript and the text before it),
    then post a synthetic Cmd+V to the frontmost application, on the key that
    gives V with Command on the current layout (on Dvorak the US V key would
    be Cmd+K, a different command)."""
    key_code = command_key_code("v")
    if key_code is None:
        raise PasteError("No key gives Cmd+V on the current keyboard layout")
    events = []
    try:
        # Allocate every event before posting any: allocation failure for a
        # key-up must not leave a lone key-down in the destination app.
        if lead:
            units = lead.encode("utf-16-le")
            characters = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
            for key_down in (True, False):
                event = _keyboard_event(0, key_down, flags=0)
                events.append(event)
                _core_graphics.CGEventKeyboardSetUnicodeString(event, len(characters), characters)
        for key_down in (True, False):
            events.append(_keyboard_event(key_code, key_down, flags=kCGEventFlagMaskCommand))
        for event in events:
            _core_graphics.CGEventPost(kCGHIDEventTap, event)
    finally:
        for event in events:
            _core_foundation.CFRelease(event)


def _keyboard_event(key_code: int, key_down: bool, *, flags: int):
    event = _core_graphics.CGEventCreateKeyboardEvent(None, key_code, key_down)
    if not event:
        raise PasteError("Could not create keyboard event")
    # Explicit, so a modifier still held from the shortcut does not ride along.
    _core_graphics.CGEventSetFlags(event, flags)
    return event


class PasteTarget:
    """The app the user was last working in, other than Maramax itself.

    Asking for the frontmost app at the moment a dictation starts is not
    enough: when a Maramax window is in front, that answer is Maramax, and
    whatever was remembered earlier may be an app the user left long ago."""

    def __init__(self, workspace=None, own_pid: int | None = None, application=None):
        self._workspace = workspace or NSWorkspace.sharedWorkspace()
        self._application = application or NSApplication.sharedApplication()
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

    def maramax_is_frontmost(self) -> bool:
        front = self._workspace.frontmostApplication()
        return front is not None and front.processIdentifier() == self._own_pid

    def bring_forward(self, app) -> None:
        """Hand the keyboard back to `app`. Since macOS 14 an app is activated
        only with the consent of the one in front: Maramax yields to it first
        (activating while ignoring other apps no longer does anything)."""
        self._application.yieldActivationToApplication_(app)
        app.activateFromApplication_options_(NSRunningApplication.currentApplication(), 0)

    def stop(self) -> None:
        if self._observer is not None:
            self._workspace.notificationCenter().removeObserver_(self._observer)
            self._observer = None
