"""Global shortcuts in Carbon's terms (virtual key codes, Carbon modifier bits): the one module that asks Carbon about keys."""

from __future__ import annotations

import ctypes
import functools
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace

import objc
from PyObjCTools import AppHelper

from .logger_config import logger


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
kUCKeyActionDown = 0
kUCKeyTranslateNoDeadKeysMask = 1

cmdKey = 1 << 8
shiftKey = 1 << 9
optionKey = 1 << 11
controlKey = 1 << 12
# Set on macOS's own shortcuts for function keys and for Globe-key combinations.
_FN_KEY = 1 << 17
kVK_Space = 0x31
kVK_ANSI_R = 0x0F

# Names of the keys a shortcut may use, by virtual key code. A code is a
# position on the keyboard, named here as on a US keyboard; layout_key_names()
# names them for the layout in use.
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
# The keys that type a character: letters, digits, and punctuation.
_TYPING_KEYS = tuple(code for code in KEY_NAMES if code != kVK_Space and code not in _FUNCTION_KEYS)
# What the typing keys give on a US keyboard, for when the layout in use cannot be read.
_US_CHARACTERS = {code: KEY_NAMES[code].lower() for code in _TYPING_KEYS}
# In the order macOS writes them.
_MODIFIER_NAMES = ((controlKey, "Control"), (optionKey, "Option"), (shiftKey, "Shift"), (cmdKey, "Cmd"))
_ALL_MODIFIERS = controlKey | optionKey | shiftKey | cmdKey
# What macOS's best-known shortcuts are for, to say so when one is refused;
# also what is assumed taken when macOS will not list its shortcuts.
_MACOS_PURPOSES = {
    (kVK_Space, cmdKey): "Spotlight",
    (kVK_Space, cmdKey | optionKey): "Finder search",
    (kVK_Space, controlKey): "switching keyboard input sources",
    (kVK_Space, controlKey | optionKey): "switching keyboard input sources",
}


class HotKeyError(RuntimeError):
    pass


def shortcut_label(key_code: int, modifiers: int, key_names: Mapping[int, str]) -> str:
    """How a shortcut is written for people: 'Option+Space', 'Control+Shift+D'."""
    names = [name for flag, name in _MODIFIER_NAMES if modifiers & flag]
    return "+".join([*names, key_names.get(key_code, f"Key {key_code}")])


def shortcut_problem(key_code: int, modifiers: int) -> str | None:
    """Why a key combination cannot be the dictation shortcut on any Mac, or
    None if it can. Whether macOS uses it on this Mac is macos_problem()."""
    if key_code not in KEY_NAMES:
        return "That key cannot be a shortcut. Use a letter, digit, punctuation key, Space, or F1–F12."
    if modifiers & ~_ALL_MODIFIERS:
        return "Use only Control, Option, Shift, and Cmd."
    if key_code in _FUNCTION_KEYS:
        return None
    held = [flag for flag in (controlKey, optionKey, cmdKey) if modifiers & flag]
    if key_code == kVK_Space:
        return None if held else "That would stop Space from typing. Add Control, Option, or Cmd."
    # A letter, digit, or punctuation key.
    if len(held) >= 2:
        return None
    if not held:
        if modifiers & shiftKey:
            return "That would stop the key from typing. Add Control or Cmd."
        return "That would stop the key from typing. Add Control+Shift, Cmd+Shift, or two of Control, Option, and Cmd."
    if held == [optionKey]:
        return "Option with a single key types a character on many keyboards. Add Control or Cmd."
    if modifiers & shiftKey:
        return None  # Control+Shift or Cmd+Shift.
    if held == [controlKey]:
        return "Control with a single key edits text and runs Terminal commands. Add Shift, Option, or Cmd."
    return "Apps use Cmd with a single key for their own commands. Add Shift, Control, or Option."


def macos_problem(key_code: int, modifiers: int, macos_shortcuts: Collection[tuple[int, int]]) -> str | None:
    """Why macOS keeps a key combination for itself, or None. `macos_shortcuts`
    is what macos_shortcuts() read on this Mac."""
    if (key_code, modifiers) not in macos_shortcuts:
        return None
    purpose = _MACOS_PURPOSES.get((key_code, modifiers))  # macOS lists which keys, not what for.
    if purpose is None:
        return "macOS uses this shortcut itself (see Keyboard Shortcuts in System Settings)."
    return f"macOS uses this shortcut for {purpose}."


def enabled_combinations(entries: Iterable[tuple[int, int, bool]]) -> frozenset[tuple[int, int]]:
    """The (key code, modifiers) of each enabled (key code, Carbon modifiers,
    enabled) entry that CopySymbolicHotKeys() lists. Its function keys carry
    the fn bit, which a shortcut registered here never has, so it is dropped;
    any other key with the fn bit is a Globe-key shortcut, which cannot
    collide with one registered here."""
    combinations = set()
    for key_code, modifiers, enabled in entries:
        if not enabled or (modifiers & _FN_KEY and key_code not in _FUNCTION_KEYS):
            continue
        combinations.add((key_code, modifiers & _ALL_MODIFIERS))
    return frozenset(combinations)


def key_names_for(typed: Mapping[int, str]) -> dict[int, str]:
    """KEY_NAMES with each typing key named by the character `typed` gives
    for it, in capitals as on a key cap. A key that gives nothing printable
    keeps its US name."""
    names = dict(KEY_NAMES)
    for key_code in _TYPING_KEYS:
        character = typed.get(key_code, "")  # A layout may leave a key without a character.
        if len(character) == 1 and character.isprintable() and not character.isspace():
            capital = character.upper()
            names[key_code] = capital if len(capital) == 1 else character  # "ß" stays "ß", not "SS".
    return names


def key_typing(character: str, typed: Mapping[int, str]) -> int | None:
    """The key code that gives `character` according to `typed`, or None."""
    matches = [key_code for key_code, given in typed.items() if given == character]
    return min(matches) if matches else None


# AppKit's modifier flags (NSEventModifierFlag…) and Carbon's bits for the same keys.
_APPKIT_MODIFIERS = ((1 << 18, controlKey), (1 << 19, optionKey), (1 << 17, shiftKey), (1 << 20, cmdKey))
# What AppKit calls F1 in a key equivalent (NSF1FunctionKey); F2–F12 follow it.
_APPKIT_F1 = 0xF704


def carbon_modifiers(event_flags: int) -> int:
    """Carbon's modifier bits for an AppKit event's modifier flags."""
    return sum(carbon for appkit, carbon in _APPKIT_MODIFIERS if event_flags & appkit)


def menu_key_equivalent(key_code: int, modifiers: int, key_names: Mapping[int, str]) -> tuple[str, int]:
    """How a menu item shows a shortcut on its right: AppKit's key equivalent
    (the key's character, lower case) and its modifier flags. `key_names` is
    layout_key_names(), so the key is the one this layout prints on it."""
    name = key_names[key_code]
    if key_code == kVK_Space:
        key = " "
    elif key_code in _FUNCTION_KEYS:
        key = chr(_APPKIT_F1 + int(name[1:]) - 1)
    else:
        key = name.lower()
    return key, sum(appkit for appkit, carbon in _APPKIT_MODIFIERS if modifiers & carbon)


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
    # How the shortcut is named to the user. The same keys are the same
    # shortcut whichever keyboard layout named them.
    label: str = field(compare=False)


DICTATION_ID = 1


def dictation_shortcut(key_code: int, modifiers: int, key_names: Mapping[int, str]) -> HotKeySpec:
    return HotKeySpec(key_code, modifiers, DICTATION_ID, shortcut_label(key_code, modifiers, key_names))


# The dictation shortcut until the user picks another, then the other choices
# offered; none of them collides with a shortcut macOS sets up by default.
# Space has the same name on every layout.
DEFAULT_DICTATE = dictation_shortcut(kVK_Space, optionKey, KEY_NAMES)
DICTATE_PRESETS = (
    DEFAULT_DICTATE,
    dictation_shortcut(kVK_Space, controlKey | shiftKey, KEY_NAMES),
    dictation_shortcut(kVK_Space, cmdKey | shiftKey, KEY_NAMES),
)
# Finishes a recording from the compact bar; registered only while one runs,
# on the key that gives R with Command (set_recording_shortcut).
STOP = HotKeySpec(key_code=kVK_ANSI_R, modifiers=cmdKey, identifier=2, label="Cmd+R")


@functools.cache
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
    carbon.CopySymbolicHotKeys.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    carbon.CopySymbolicHotKeys.restype = OSStatus
    carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.argtypes = []
    carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    carbon.CFDataGetBytePtr.restype = ctypes.c_void_p
    carbon.CFRelease.argtypes = [ctypes.c_void_p]
    carbon.CFRelease.restype = None
    carbon.LMGetKbdType.argtypes = []
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    carbon.UCKeyTranslate.argtypes = [
        ctypes.c_void_p,                    # const UCKeyboardLayout *
        ctypes.c_uint16,                    # virtual key code
        ctypes.c_uint16,                    # key action
        UInt32,                             # modifier key state
        UInt32,                             # keyboard type
        OptionBits,
        ctypes.POINTER(UInt32),             # dead key state
        ctypes.c_ulong,                     # room in the buffer
        ctypes.POINTER(ctypes.c_ulong),     # characters written
        ctypes.POINTER(ctypes.c_uint16),    # UniChar buffer
    ]
    carbon.UCKeyTranslate.restype = OSStatus
    return carbon


# -- What macOS and the keyboard layout say about keys --


def macos_shortcuts() -> frozenset[tuple[int, int]]:
    """The shortcuts macOS has turned on for itself on this Mac (Keyboard
    Shortcuts in System Settings, defaults included), as (key code,
    modifiers). If macOS will not list them, its best-known defaults."""
    carbon = _load_carbon()
    array = ctypes.c_void_p()
    status = carbon.CopySymbolicHotKeys(ctypes.byref(array))
    if status != noErr or not array.value:
        logger.warning(f"CopySymbolicHotKeys failed with OSStatus {status}; assuming macOS's default shortcuts")
        return frozenset(_MACOS_PURPOSES)
    entries = objc.objc_object(c_void_p=array.value)  # The proxy holds its own reference.
    carbon.CFRelease(array)
    return enabled_combinations(
        (int(entry["kHISymbolicHotKeyCode"]), int(entry["kHISymbolicHotKeyModifiers"]),
         bool(entry["kHISymbolicHotKeyEnabled"]))
        for entry in entries
    )


def layout_key_names() -> dict[int, str]:
    """What each shortcut key is called on the current keyboard layout. Main thread only."""
    return key_names_for(_typed_characters(0))


def command_key_code(character: str) -> int | None:
    """The key that gives `character` with Command held on the current
    keyboard layout, which is how apps match their Command shortcuts; None if
    no key does. Main thread only."""
    return key_typing(character, _typed_characters(cmdKey))


def _typed_characters(modifiers: int) -> dict[int, str]:
    """What each typing key gives with `modifiers` held, on the current
    keyboard layout with Latin letters (while a Russian or Greek layout is in
    use, the Latin one used last, which macOS also uses for Command
    shortcuts). A US keyboard's characters if that layout cannot be read."""
    carbon = _load_carbon()
    source = carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource()
    if not source:
        return dict(_US_CHARACTERS)
    try:
        key_layout = carbon.TISGetInputSourceProperty(
            source, ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData"))
        if not key_layout:
            return dict(_US_CHARACTERS)
        layout = carbon.CFDataGetBytePtr(key_layout)
        keyboard = carbon.LMGetKbdType()
        return {key_code: _translate(carbon, layout, keyboard, key_code, modifiers) for key_code in _TYPING_KEYS}
    finally:
        carbon.CFRelease(source)


def _translate(carbon, layout, keyboard: int, key_code: int, modifiers: int) -> str:
    """What one key press gives; a dead key gives its accent as is."""
    dead_key_state = UInt32(0)
    length = ctypes.c_ulong(0)
    characters = (ctypes.c_uint16 * 4)()
    status = carbon.UCKeyTranslate(
        layout, key_code, kUCKeyActionDown, (modifiers >> 8) & 0xFF, keyboard, kUCKeyTranslateNoDeadKeysMask,
        ctypes.byref(dead_key_state), len(characters), ctypes.byref(length), characters,
    )
    if status != noErr:
        logger.warning(f"UCKeyTranslate failed for key {key_code} with OSStatus {status}; using its US name")
        return ""
    return "".join(chr(unit) for unit in characters[:length.value])


# -- Registering global shortcuts --


def _four_char_code(value: str) -> int:
    if len(value) != 4:
        raise ValueError("OSType signatures must be exactly 4 characters")
    return int.from_bytes(value.encode("ascii"), "big")


_HANDLER_PROC = ctypes.CFUNCTYPE(OSStatus, EventHandlerCallRef, EventRef, ctypes.c_void_p)


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
        recorded. If Carbon refuses `spec`, the previous one is put back and
        HotKeyError raised. Carbon does not refuse a shortcut another app or
        macOS uses; it refuses only one this app already holds."""
        previous = self._dictation
        if previous is not None:
            self._unregister(previous[1])
            self._dictation = None
        if spec is None:
            return
        try:
            self._dictation = (spec, self._register(spec))
        except HotKeyError:
            if previous is not None:
                self._dictation = (previous[0], self._register(previous[0]))
            raise

    def _unregister(self, hotkey_ref: EventHotKeyRef) -> None:
        self._carbon.UnregisterEventHotKey(hotkey_ref)
        self._hotkey_refs.remove(hotkey_ref)

    def _register(self, spec: HotKeySpec) -> EventHotKeyRef:
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
        """Cmd+R belongs to other apps except during our own recording. It is
        registered on the key that gives R with Command on the current layout,
        which is the key apps answer Cmd+R on (on Dvorak, not the US R key)."""
        self._stop_handler = handler
        if handler is not None and self._recording_ref is None:
            key_code = command_key_code(KEY_NAMES[STOP.key_code].lower())
            if key_code is None:
                raise HotKeyError(f"No key gives {STOP.label} on the current keyboard layout")
            self._recording_ref = self._register(replace(STOP, key_code=key_code))
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
