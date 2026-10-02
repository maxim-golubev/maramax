"""Global shortcuts: what makes a good one, what it is called, and registering it through Carbon (ctypes)."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from PyObjCTools import AppHelper


OSStatus = ctypes.c_int32
EventRef = ctypes.c_void_p
EventHandlerCallRef = ctypes.c_void_p
EventHandlerRef = ctypes.c_void_p
EventHotKeyRef = ctypes.c_void_p
EventTargetRef = ctypes.c_void_p
OptionBits = ctypes.c_uint32
UInt32 = ctypes.c_uint32
UInt64 = ctypes.c_uint64

noErr = 0
eventNotHandledErr = -9874

kEventClassKeyboard = 0x6B657962
kEventHotKeyPressed = 5
kEventParamDirectObject = 0x2D2D2D2D
typeEventHotKeyID = 0x686B6964

cmdKey = 1 << 8
shiftKey = 1 << 9
optionKey = 1 << 11
controlKey = 1 << 12
kVK_Space = 0x31
kVK_ANSI_R = 0x0F

# Names of the keys a shortcut may use, by virtual key code (positions of a US
# keyboard; on other layouts a letter can sit elsewhere, the code is what counts).
KEY_NAMES = {
    0x00: "A", 0x0B: "B", 0x08: "C", 0x02: "D", 0x0E: "E", 0x03: "F", 0x05: "G", 0x04: "H", 0x22: "I",
    0x26: "J", 0x28: "K", 0x25: "L", 0x2E: "M", 0x2D: "N", 0x1F: "O", 0x23: "P", 0x0C: "Q", 0x0F: "R",
    0x01: "S", 0x11: "T", 0x20: "U", 0x09: "V", 0x0D: "W", 0x07: "X", 0x10: "Y", 0x06: "Z",
    0x1D: "0", 0x12: "1", 0x13: "2", 0x14: "3", 0x15: "4", 0x17: "5", 0x16: "6", 0x1A: "7", 0x1C: "8", 0x19: "9",
    0x1B: "-", 0x18: "=", 0x21: "[", 0x1E: "]", 0x2A: "\\", 0x29: ";", 0x27: "'", 0x2B: ",", 0x2F: ".", 0x2C: "/",
    0x32: "`", kVK_Space: "Space",
    0x7A: "F1", 0x78: "F2", 0x63: "F3", 0x76: "F4", 0x60: "F5", 0x61: "F6", 0x62: "F7", 0x64: "F8",
    0x65: "F9", 0x6D: "F10", 0x67: "F11", 0x6F: "F12",
}
_FUNCTION_KEYS = {code for code, name in KEY_NAMES.items() if name.startswith("F") and name[1:].isdigit()}
# In the order macOS writes them.
_MODIFIER_NAMES = ((controlKey, "Control"), (optionKey, "Option"), (shiftKey, "Shift"), (cmdKey, "Cmd"))
_ALL_MODIFIERS = controlKey | optionKey | shiftKey | cmdKey
# Shortcuts macOS keeps for itself out of the box: registering one of these
# either fails or never fires.
_RESERVED = {
    (kVK_Space, cmdKey): "Spotlight",
    (kVK_Space, cmdKey | optionKey): "Finder search",
    (kVK_Space, controlKey): "switching keyboard input sources",
    (kVK_Space, controlKey | optionKey): "switching keyboard input sources",
}


class HotKeyError(RuntimeError):
    pass


def shortcut_label(key_code: int, modifiers: int) -> str:
    """How a shortcut is written for people: 'Option+Space', 'Control+Shift+D'."""
    names = [name for flag, name in _MODIFIER_NAMES if modifiers & flag]
    return "+".join([*names, KEY_NAMES.get(key_code, f"Key {key_code}")])


def shortcut_problem(key_code: int, modifiers: int) -> str | None:
    """Why a key combination cannot be the dictation shortcut, or None if it can."""
    if key_code not in KEY_NAMES or modifiers & ~_ALL_MODIFIERS:
        return "Use a letter, digit, Space, or F-key with Control, Option, or Cmd."
    label = shortcut_label(key_code, modifiers)
    if (key_code, modifiers) in _RESERVED:
        return f"macOS uses {label} for {_RESERVED[key_code, modifiers]}."
    if key_code in _FUNCTION_KEYS:
        return None
    if not modifiers & (controlKey | optionKey | cmdKey):
        return f"{label} would stop that key from typing. Add Control, Option, or Cmd."
    if not modifiers & (controlKey | optionKey) and key_code != kVK_Space:
        return f"Apps use {label} for their own commands. Add Control or Option."
    return None


def carbon_modifiers(event_flags: int) -> int:
    """Carbon's modifier bits for an AppKit event's modifier flags."""
    pairs = ((1 << 18, controlKey), (1 << 19, optionKey), (1 << 17, shiftKey), (1 << 20, cmdKey))
    return sum(carbon for appkit, carbon in pairs if event_flags & appkit)


class EventTypeSpec(ctypes.Structure):
    _fields_ = [
        ("eventClass", UInt32),
        ("eventKind", UInt32),
    ]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [
        ("signature", UInt32),
        ("id", UInt32),
    ]


@dataclass(frozen=True)
class HotKeySpec:
    key_code: int
    modifiers: int
    identifier: int
    label: str  # How the shortcut is named to the user.


DICTATION_ID = 1


def dictation_shortcut(key_code: int, modifiers: int) -> HotKeySpec:
    return HotKeySpec(key_code, modifiers, DICTATION_ID, shortcut_label(key_code, modifiers))


# The dictation shortcut until the user picks another, then the other choices
# offered; none of them collides with a shortcut macOS sets up by default.
DEFAULT_DICTATE = dictation_shortcut(kVK_Space, optionKey)
DICTATE_PRESETS = (
    DEFAULT_DICTATE,
    dictation_shortcut(kVK_Space, controlKey | shiftKey),
    dictation_shortcut(kVK_Space, cmdKey | shiftKey),
)
# Finishes a recording from the compact bar; registered only while one runs.
STOP = HotKeySpec(key_code=kVK_ANSI_R, modifiers=cmdKey, identifier=2, label="Cmd+R")


def _four_char_code(value: str) -> int:
    if len(value) != 4:
        raise ValueError("OSType signatures must be exactly 4 characters")
    return int.from_bytes(value.encode("ascii"), "big")


_HANDLER_PROC = ctypes.CFUNCTYPE(OSStatus, EventHandlerCallRef, EventRef, ctypes.c_void_p)


def _load_carbon():
    carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.GetApplicationEventTarget.restype = EventTargetRef
    carbon.InstallEventHandler.argtypes = [
        EventTargetRef,
        _HANDLER_PROC,
        UInt64,
        ctypes.POINTER(EventTypeSpec),
        ctypes.c_void_p,
        ctypes.POINTER(EventHandlerRef),
    ]
    carbon.InstallEventHandler.restype = OSStatus
    carbon.GetEventParameter.argtypes = [
        EventRef,
        UInt32,
        UInt32,
        ctypes.POINTER(UInt32),
        UInt64,
        ctypes.POINTER(UInt64),
        ctypes.c_void_p,
    ]
    carbon.GetEventParameter.restype = OSStatus
    carbon.RegisterEventHotKey.argtypes = [
        UInt32,
        UInt32,
        EventHotKeyID,
        EventTargetRef,
        OptionBits,
        ctypes.POINTER(EventHotKeyRef),
    ]
    carbon.RegisterEventHotKey.restype = OSStatus
    carbon.UnregisterEventHotKey.argtypes = [EventHotKeyRef]
    carbon.UnregisterEventHotKey.restype = OSStatus
    carbon.RemoveEventHandler.argtypes = [EventHandlerRef]
    carbon.RemoveEventHandler.restype = OSStatus
    return carbon


class GlobalHotKeyManager:
    def __init__(self, handler):
        self._carbon = _load_carbon()
        self._handler = handler
        self._target = self._carbon.GetApplicationEventTarget()
        self._event_handler_ref = EventHandlerRef()
        self._hotkey_refs: list[EventHotKeyRef] = []
        self._recording_ref: EventHotKeyRef | None = None
        self._dictation: tuple[HotKeySpec, EventHotKeyRef] | None = None
        self._stop_handler = None
        self._callback = _HANDLER_PROC(self._handle_event)
        self._signature = _four_char_code("MRMX")
        self._install_event_handler()

    def set_dictation_shortcut(self, spec: HotKeySpec | None) -> None:
        """Make `spec` the dictation shortcut, or none while one is being
        recorded. If `spec` cannot be registered, the previous one is put
        back and HotKeyError raised."""
        previous = self._dictation
        if previous is not None:
            self._unregister(previous[1])
            self._dictation = None
        if spec is None:
            return
        try:
            self._dictation = (spec, self.register(spec))
        except HotKeyError:
            if previous is not None:
                self._dictation = (previous[0], self.register(previous[0]))
            raise

    def _unregister(self, hotkey_ref: EventHotKeyRef) -> None:
        self._carbon.UnregisterEventHotKey(hotkey_ref)
        self._hotkey_refs.remove(hotkey_ref)

    def register(self, spec: HotKeySpec) -> EventHotKeyRef:
        hotkey_id = EventHotKeyID(self._signature, spec.identifier)
        hotkey_ref = EventHotKeyRef()
        status = self._carbon.RegisterEventHotKey(
            spec.key_code,
            spec.modifiers,
            hotkey_id,
            self._target,
            0,
            ctypes.byref(hotkey_ref),
        )
        if status != noErr:
            raise HotKeyError(f"RegisterEventHotKey failed with OSStatus {status}")
        self._hotkey_refs.append(hotkey_ref)
        return hotkey_ref

    def set_recording_shortcut(self, handler=None) -> None:
        """Cmd+R belongs to other apps except during our own recording."""
        self._stop_handler = handler
        if handler is not None and self._recording_ref is None:
            self._recording_ref = self.register(STOP)
        elif handler is None and self._recording_ref is not None:
            self._unregister(self._recording_ref)
            self._recording_ref = None

    def cleanup(self) -> None:
        self._stop_handler = None
        self._recording_ref = None
        self._dictation = None
        while self._hotkey_refs:
            hotkey_ref = self._hotkey_refs.pop()
            self._carbon.UnregisterEventHotKey(hotkey_ref)

        if self._event_handler_ref:
            self._carbon.RemoveEventHandler(self._event_handler_ref)
            self._event_handler_ref = EventHandlerRef()

    def _install_event_handler(self) -> None:
        event_spec = EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
        status = self._carbon.InstallEventHandler(
            self._target,
            self._callback,
            1,
            ctypes.byref(event_spec),
            None,
            ctypes.byref(self._event_handler_ref),
        )
        if status != noErr:
            raise HotKeyError(f"InstallEventHandler failed with OSStatus {status}")

    def _handle_event(self, _next_handler, event, _user_data) -> int:
        hotkey_id = EventHotKeyID()
        actual_size = UInt64()
        status = self._carbon.GetEventParameter(
            event,
            kEventParamDirectObject,
            typeEventHotKeyID,
            None,
            ctypes.sizeof(hotkey_id),
            ctypes.byref(actual_size),
            ctypes.byref(hotkey_id),
        )
        if status == noErr and hotkey_id.signature == self._signature:
            if hotkey_id.id == DICTATION_ID:
                AppHelper.callAfter(self._handler)
            elif hotkey_id.id == STOP.identifier and self._stop_handler is not None:
                AppHelper.callAfter(self._stop_handler)
            return noErr
        return eventNotHandledErr
