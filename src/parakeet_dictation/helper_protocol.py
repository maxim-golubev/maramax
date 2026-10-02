"""The line protocol between the app and its audio helper process.

Each message is one JSON object per line: requests on the helper's stdin,
events on its stdout. EOF on stdin means the app is gone.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import NamedTuple


class Operation(StrEnum):
    RECORD = "record"    # device, prefer_builtin → READY, AUDIO…, then on STOP: DONE, IDLE or WARM
    STOP = "stop"        # keep_warm: seconds to leave the device open afterwards
    LIST = "list"        # prefer_builtin → DEVICES
    RELEASE = "release"  # close a device that is being kept warm → IDLE
    PING = "ping"        # → PONG


class Event(StrEnum):
    READY = "ready"                # device, warm
    AUDIO = "audio"                # pcm (base64), overflows
    DONE = "done"                  # every captured buffer has been sent
    IDLE = "idle"                  # no device open; waiting for a request
    WARM = "warm"                  # device kept open; waiting for a request
    CLOSING = "closing"            # helper is closing a device on its own initiative
    RECONNECTING = "reconnecting"  # the input failed; a replacement is being opened
    DEVICE = "device"              # device, reopened: the outcome of RECONNECTING
    DEVICES = "devices"            # devices, automatic
    ERROR = "error"                # message
    PONG = "pong"


class InputDevice(NamedTuple):
    device_index: int
    name: str
    is_default: bool


def request(operation: Operation, **fields) -> str:
    return json.dumps({"operation": operation, **fields})


def event(kind: Event, **fields) -> str:
    return json.dumps({"event": kind, **fields})


def parse(line: str) -> dict | None:
    """The message on a line, or None for anything that is not one."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None
