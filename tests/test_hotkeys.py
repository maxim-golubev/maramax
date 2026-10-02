import pytest

from parakeet_dictation.hotkeys import _four_char_code


def test_four_char_code_requires_exact_length():
    with pytest.raises(ValueError, match="exactly 4 characters"):
        _four_char_code("MM")


def test_four_char_code_encodes_ascii_signature():
    assert _four_char_code("MRMX") == int.from_bytes(b"MRMX", "big")


# -- The dictation shortcut --

from types import SimpleNamespace  # noqa: E402

from parakeet_dictation import hotkeys  # noqa: E402
from parakeet_dictation.hotkeys import (  # noqa: E402
    DEFAULT_DICTATE, DICTATE_PRESETS, HotKeyError, carbon_modifiers, cmdKey, controlKey, dictation_shortcut,
    optionKey, shiftKey, shortcut_label, shortcut_problem,
)

SPACE, D, F5, TAB = 0x31, 0x02, 0x60, 0x30


@pytest.mark.parametrize("key, modifiers, label", [
    (SPACE, optionKey, "Option+Space"),
    (SPACE, cmdKey | shiftKey, "Shift+Cmd+Space"),
    (D, controlKey | optionKey | shiftKey | cmdKey, "Control+Option+Shift+Cmd+D"),
    (F5, 0, "F5"),
])
def test_shortcuts_are_named_in_the_order_macos_writes_them(key, modifiers, label):
    assert shortcut_label(key, modifiers) == label


@pytest.mark.parametrize("key, modifiers, reason", [
    (SPACE, optionKey, None),
    (D, controlKey | optionKey, None),
    (F5, 0, None),                                   # A function key alone is fine.
    (SPACE, cmdKey | shiftKey, None),
    (SPACE, cmdKey, "Spotlight"),
    (SPACE, controlKey, "input sources"),
    (SPACE, controlKey | optionKey, "input sources"),
    (TAB, cmdKey, "Use a letter"),                   # Tab is not offered (and Cmd+Tab switches apps).
    (D, 0, "stop that key from typing"),
    (D, shiftKey, "stop that key from typing"),
    (D, cmdKey, "Apps use Cmd+D"),                  # Would steal the command from every app.
    (D, cmdKey | shiftKey, "Apps use"),
    (0x24, optionKey, "Use a letter"),               # Return is not offered.
])
def test_which_shortcuts_can_be_the_dictation_shortcut(key, modifiers, reason):
    problem = shortcut_problem(key, modifiers)
    assert (problem is None) if reason is None else (reason in problem)


def test_every_preset_is_allowed_and_option_space_is_recommended():
    assert DICTATE_PRESETS[0] == DEFAULT_DICTATE and DEFAULT_DICTATE.label == "Option+Space"
    assert all(shortcut_problem(p.key_code, p.modifiers) is None for p in DICTATE_PRESETS)


def test_appkit_modifier_flags_become_carbon_bits():
    appkit = (1 << 18) | (1 << 19) | (1 << 17) | (1 << 20) | (1 << 16)   # plus Caps Lock, ignored
    assert carbon_modifiers(appkit) == controlKey | optionKey | shiftKey | cmdKey


def fake_manager(refuse=()):
    """A manager on a fake Carbon that records registrations and refuses some key codes."""
    registered = []
    manager = object.__new__(hotkeys.GlobalHotKeyManager)
    manager._hotkey_refs = []
    manager._dictation = None
    manager._recording_ref = None
    manager._signature = 1
    manager._target = None

    def register(key, modifiers, hotkey_id, target, options, ref):
        if key in refuse:
            return -9878
        registered.append(key)
        return hotkeys.noErr

    def unregister(ref):
        registered.pop(0)
        return hotkeys.noErr

    manager._carbon = SimpleNamespace(RegisterEventHotKey=register, UnregisterEventHotKey=unregister)
    return manager, registered


def test_a_new_shortcut_replaces_the_old_one_and_a_refused_one_restores_it():
    manager, registered = fake_manager(refuse={D})
    manager.set_dictation_shortcut(DEFAULT_DICTATE)
    manager.set_dictation_shortcut(dictation_shortcut(F5, 0))
    assert registered == [F5]
    with pytest.raises(HotKeyError):
        manager.set_dictation_shortcut(dictation_shortcut(D, controlKey | optionKey))
    assert registered == [F5]                     # The previous shortcut is back.
    manager.set_dictation_shortcut(None)          # Paused while new keys are recorded.
    assert registered == []
