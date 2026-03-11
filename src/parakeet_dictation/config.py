from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShortcutConfig:
    open_overlay: str = "Option+Space"
    toggle_recording: str = "Cmd+R"
    copy_result: str = "Cmd+C"
    close_overlay: str = "Esc"


@dataclass(frozen=True)
class AppConfig:
    auto_start_recording: bool = True
    auto_copy_to_clipboard: bool = True
    history_limit: int = 100
    shortcuts: ShortcutConfig = field(default_factory=ShortcutConfig)
