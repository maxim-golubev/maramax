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
kVK_Space = 0x31


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


def _four_char_code(value: str) -> int:
    if len(value) != 4:
        raise ValueError("OSType signatures must be exactly 4 characters")
    return int.from_bytes(value.encode("ascii"), "big")


class GlobalHotKeyManager:
    _carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
    _handler_proc = ctypes.CFUNCTYPE(OSStatus, EventHandlerCallRef, EventRef, ctypes.c_void_p)

    _carbon.GetApplicationEventTarget.restype = EventTargetRef
    _carbon.InstallEventHandler.argtypes = [
        EventTargetRef,
        _handler_proc,
        UInt64,
        ctypes.POINTER(EventTypeSpec),
        ctypes.c_void_p,
        ctypes.POINTER(EventHandlerRef),
    ]
    _carbon.InstallEventHandler.restype = OSStatus
    _carbon.GetEventParameter.argtypes = [
        EventRef,
        UInt32,
        UInt32,
        ctypes.POINTER(UInt32),
        UInt64,
        ctypes.POINTER(UInt64),
        ctypes.c_void_p,
    ]
    _carbon.GetEventParameter.restype = OSStatus
    _carbon.RegisterEventHotKey.argtypes = [
        UInt32,
        UInt32,
        EventHotKeyID,
        EventTargetRef,
        OptionBits,
        ctypes.POINTER(EventHotKeyRef),
    ]
    _carbon.RegisterEventHotKey.restype = OSStatus
    _carbon.UnregisterEventHotKey.argtypes = [EventHotKeyRef]
    _carbon.UnregisterEventHotKey.restype = OSStatus
    _carbon.RemoveEventHandler.argtypes = [EventHandlerRef]
    _carbon.RemoveEventHandler.restype = OSStatus

    def __init__(self, handler):
        self._handler = handler
        self._target = self._carbon.GetApplicationEventTarget()
        self._event_handler_ref = EventHandlerRef()
        self._hotkey_refs: list[EventHotKeyRef] = []
        self._callback = self._handler_proc(self._handle_event)
        self._signature = _four_char_code("MRMX")
        self._install_event_handler()

    def register_default_overlay_shortcut(self) -> None:
        self.register(HotKeySpec(key_code=kVK_Space, modifiers=optionKey, identifier=1))

    def register(self, spec: HotKeySpec) -> None:
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

    def cleanup(self) -> None:
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
            AppHelper.callAfter(self._handler)
            return noErr
        return eventNotHandledErr
