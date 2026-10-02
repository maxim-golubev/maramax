"""Global shortcuts through the Carbon hot-key API via ctypes."""

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

optionKey = 1 << 11
cmdKey = 1 << 8
kVK_Space = 0x31
kVK_ANSI_R = 0x0F


class HotKeyError(RuntimeError):
    pass


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


# The two shortcuts and their names live here and nowhere else.
DICTATE = HotKeySpec(key_code=kVK_Space, modifiers=optionKey, identifier=1, label="Option+Space")
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
        self._stop_handler = None
        self._callback = _HANDLER_PROC(self._handle_event)
        self._signature = _four_char_code("MRMX")
        self._install_event_handler()

    def register_dictation_shortcut(self) -> None:
        self.register(DICTATE)

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
            self._carbon.UnregisterEventHotKey(self._recording_ref)
            self._hotkey_refs.remove(self._recording_ref)
            self._recording_ref = None

    def cleanup(self) -> None:
        self._stop_handler = None
        self._recording_ref = None
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
            if hotkey_id.id == DICTATE.identifier:
                AppHelper.callAfter(self._handler)
            elif hotkey_id.id == STOP.identifier and self._stop_handler is not None:
                AppHelper.callAfter(self._stop_handler)
            return noErr
        return eventNotHandledErr
