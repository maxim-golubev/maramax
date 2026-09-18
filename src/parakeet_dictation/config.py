"""User settings persisted atomically to settings.json with type-validated loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .corrections import normalize_rules

# Settings the user can change at runtime; everything else stays code-defined.
_PERSISTED_FIELDS = (
    "auto_start_recording",
    "auto_copy_to_clipboard",
    "paste_to_active_app",
    "live_preview",
    "high_accuracy",
    "history_limit",
    "compact_dictation",
    "prefer_builtin_mic",
    "input_device",
    "use_corrections",
    "replacements",
)


@dataclass(frozen=True)
class ShortcutConfig:
    open_overlay: str = "Option+Space"
    toggle_recording: str = "Cmd+R"
    copy_result: str = "Cmd+C"
    close_overlay: str = "Esc"


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
    use_corrections: bool = True
    replacements: list[dict[str, str]] = field(default_factory=list)
    shortcuts: ShortcutConfig = field(default_factory=ShortcutConfig)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Load settings from JSON, falling back to defaults for anything
        missing, malformed, or of the wrong type."""
        config = cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return config

        if not isinstance(payload, dict):
            return config

        for name in _PERSISTED_FIELDS:
            if name not in payload:
                continue
            default = getattr(config, name)
            value = payload[name]
            if name == "replacements":
                config.replacements = normalize_rules(value)
                continue
            if name == "input_device":
                if value is None or (isinstance(value, str) and value.strip()):
                    config.input_device = value
                continue
            if isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(config, name, value)
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    setattr(config, name, value)
        return config

    def save(self, path: Path) -> None:
        payload = {name: getattr(self, name) for name in _PERSISTED_FIELDS}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(path)
