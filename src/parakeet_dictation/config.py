"""User settings, persisted to settings.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from .atomic_file import set_aside, write_text_atomically
from .corrections import normalize_rules
from .hotkeys import DEFAULT_DICTATE, shortcut_problem

# How long the microphone may stay connected after a dictation.
MAX_KEEP_MIC_READY_SECONDS = 600


@dataclass
class AppConfig:
    auto_start_recording: bool = True
    auto_copy_to_clipboard: bool = True
    paste_to_active_app: bool = False
    live_preview: bool = True
    high_accuracy: bool = False
    history_limit: int = 100
    compact_dictation: bool = True
    prefer_builtin_mic: bool = True
    input_device: str | None = None
    # 0 releases the microphone the moment a dictation ends.
    keep_mic_ready_seconds: int = 0
    use_corrections: bool = True
    replacements: list[dict[str, str]] = field(default_factory=list)
    check_for_updates: bool = True
    # [virtual key code, Carbon modifier bits]; see hotkeys.shortcut_problem().
    dictation_shortcut: list[int] = field(
        default_factory=lambda: [DEFAULT_DICTATE.key_code, DEFAULT_DICTATE.modifiers])
    # The welcome window has been through once (it can be reopened from the menu).
    onboarded: bool = False
    # A release the user chose "Skip This Version" for; automatic checks do not offer it again.
    skipped_update_version: str | None = None
    # Settings written by a newer version, kept so saving here (for example
    # after rolling back) does not erase them.
    unrecognized: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Load settings from JSON. A missing file gives defaults; a single
        missing or mistyped value falls back to its default; a file that is
        not valid settings at all is set aside rather than overwritten."""
        config = cls()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return config
        except (OSError, UnicodeDecodeError) as exc:
            set_aside(path, str(exc))
            return config
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("settings are not an object")
        except ValueError as exc:
            set_aside(path, str(exc))
            return config

        config.unrecognized = {key: value for key, value in payload.items() if key not in _PERSISTED_FIELDS}
        for name in _PERSISTED_FIELDS:
            if name not in payload:
                continue
            default = getattr(config, name)
            value = payload[name]
            if name == "replacements":
                config.replacements = normalize_rules(value)
                continue
            if name in ("input_device", "skipped_update_version"):
                if value is None or (isinstance(value, str) and value.strip()):
                    setattr(config, name, value)
                continue
            if name == "dictation_shortcut":
                if (isinstance(value, list) and len(value) == 2
                        and all(isinstance(part, int) and not isinstance(part, bool) for part in value)
                        and shortcut_problem(*value) is None):
                    config.dictation_shortcut = value
                continue
            if name == "keep_mic_ready_seconds":
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_KEEP_MIC_READY_SECONDS:
                    config.keep_mic_ready_seconds = value
                continue
            if isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(config, name, value)
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    setattr(config, name, value)
        return config

    def save(self, path: Path) -> None:
        payload = dict(self.unrecognized) | {name: getattr(self, name) for name in _PERSISTED_FIELDS}
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path, json.dumps(payload, indent=2))


# Every setting is persisted; the list is the dataclass itself.
_PERSISTED_FIELDS = tuple(f.name for f in fields(AppConfig) if f.name != "unrecognized")
