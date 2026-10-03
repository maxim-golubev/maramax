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
    DEFAULT_DICTATE, DICTATE_PRESETS, KEY_NAMES, STOP, HotKeyError, carbon_modifiers, cmdKey, controlKey,
    dictation_shortcut, enabled_combinations, key_names_for, key_typing, macos_problem, optionKey, shiftKey,
    shortcut_label, shortcut_problem,
)

SPACE, D, E, C, V, TWO, MINUS, F5, F11, TAB, RETURN = 0x31, 0x02, 0x0E, 0x08, 0x09, 0x13, 0x1B, 0x60, 0x67, 0x30, 0x24
FN = 1 << 17


@pytest.mark.parametrize("key, modifiers, label", [
    (SPACE, optionKey, "Option+Space"),
    (SPACE, cmdKey | shiftKey, "Shift+Cmd+Space"),
    (D, controlKey | optionKey | shiftKey | cmdKey, "Control+Option+Shift+Cmd+D"),
    (F5, 0, "F5"),
])
def test_shortcuts_are_named_in_the_order_macos_writes_them(key, modifiers, label):
    assert shortcut_label(key, modifiers, KEY_NAMES) == label


@pytest.mark.parametrize("key, modifiers, reason", [
    (SPACE, optionKey, None),
    (SPACE, cmdKey, None),                           # Spotlight's, but only macos_problem() knows that.
    (D, controlKey | optionKey, None),
    (D, optionKey | cmdKey, None),
    (D, controlKey | cmdKey, None),
    (D, controlKey | shiftKey, None),
    (D, cmdKey | shiftKey, None),
    (F5, 0, None),                                   # A function key alone is fine.
    (F5, cmdKey, None),
    (SPACE, cmdKey | shiftKey, None),
    (TAB, cmdKey, "cannot be a shortcut"),           # Tab is not offered (and Cmd+Tab switches apps).
    (RETURN, optionKey, "cannot be a shortcut"),
    (D, 1 << 16, "only Control, Option, Shift, and Cmd"),
    (SPACE, 0, "stop Space from typing"),
    (SPACE, shiftKey, "stop Space from typing"),
    (D, 0, "stop the key from typing"),
    (D, shiftKey, "stop the key from typing"),
    (E, optionKey, "types a character"),             # The é dead key on a US keyboard.
    (TWO, optionKey, "types a character"),           # ™
    (MINUS, optionKey | shiftKey, "types a character"),  # —
    (C, controlKey, "Terminal"),                     # Interrupt.
    (D, cmdKey, "Apps use Cmd"),                     # Would steal the command from every app.
])
def test_which_shortcuts_can_be_the_dictation_shortcut(key, modifiers, reason):
    problem = shortcut_problem(key, modifiers)
    assert (problem is None) if reason is None else (reason in problem), problem


@pytest.mark.parametrize("key, modifiers, added", [
    (E, optionKey, controlKey), (E, optionKey, cmdKey),
    (C, controlKey, shiftKey), (C, controlKey, optionKey), (C, controlKey, cmdKey),
    (D, cmdKey, shiftKey), (D, cmdKey, controlKey), (D, cmdKey, optionKey),
    (D, 0, controlKey | optionKey), (SPACE, 0, optionKey),
])
def test_each_refusal_says_what_to_add_and_adding_it_is_accepted(key, modifiers, added):
    name = {controlKey: "Control", optionKey: "Option", shiftKey: "Shift", cmdKey: "Cmd"}.get(added, "two of")
    assert name in shortcut_problem(key, modifiers)
    assert shortcut_problem(key, modifiers | added) is None


def test_every_preset_is_allowed_and_option_space_is_recommended():
    assert DICTATE_PRESETS[0] == DEFAULT_DICTATE and DEFAULT_DICTATE.label == "Option+Space"
    assert all(shortcut_problem(p.key_code, p.modifiers) is None for p in DICTATE_PRESETS)


def test_shortcuts_macos_has_turned_on_are_refused_with_what_they_do():
    taken = {(SPACE, cmdKey), (F11, 0)}
    assert macos_problem(SPACE, cmdKey, taken) == "macOS uses this shortcut for Spotlight."
    assert "Keyboard Shortcuts in System Settings" in macos_problem(F11, 0, taken)
    assert macos_problem(SPACE, optionKey, taken) is None
    assert macos_problem(SPACE, controlKey, taken) is None   # Input-source switching turned off on this Mac.


def test_macos_shortcut_list_keeps_enabled_entries_in_the_terms_used_here():
    assert enabled_combinations([
        (SPACE, cmdKey, True),
        (SPACE, controlKey, False),                 # Turned off in System Settings.
        (F11, FN, True),                            # Show Desktop: function keys carry the fn bit.
        (0x78, FN | controlKey, True),              # Control+F2
        (0x00, FN, True),                           # Globe+A: never collides with a shortcut here.
    ]) == {(SPACE, cmdKey), (F11, 0), (0x78, controlKey)}


def test_keys_are_named_as_the_keyboard_layout_types_them():
    french = {0x0C: "a", 0x00: "q", 0x0D: "z", 0x1B: ")", 0x27: "ù"}
    names = key_names_for(french)
    assert (names[0x0C], names[0x00], names[0x0D], names[0x1B], names[0x27]) == ("A", "Q", "Z", ")", "Ù")
    assert names[D] == "D" and names[SPACE] == "Space" and names[F5] == "F5"   # Not given: the US names.
    assert key_names_for({0x1B: "ß", 0x18: "", 0x21: "\x10", 0x27: " "}) == KEY_NAMES | {0x1B: "ß"}
    assert shortcut_label(0x0C, controlKey | optionKey, names) == "Control+Option+A"


def test_a_shortcut_is_the_same_whichever_layout_named_it():
    dvorak = dictation_shortcut(D, controlKey | optionKey, key_names_for({D: "e"}))
    assert dvorak.label == "Control+Option+E" and dvorak == dictation_shortcut(D, controlKey | optionKey, KEY_NAMES)


def test_the_key_that_types_a_character_is_found_in_the_layout():
    dvorak = {V: "k", 0x2F: "v", 0x0F: "p", 0x1F: "r"}
    assert key_typing("v", dvorak) == 0x2F and key_typing("r", dvorak) == 0x1F
    assert key_typing("v", {V: "v"}) == V
    assert key_typing("v", {V: "м"}) is None


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
    manager.set_dictation_shortcut(dictation_shortcut(F5, 0, KEY_NAMES))
    assert registered == [F5]
    with pytest.raises(HotKeyError):
        manager.set_dictation_shortcut(dictation_shortcut(D, controlKey | optionKey, KEY_NAMES))
    assert registered == [F5]                     # The previous shortcut is back.
    manager.set_dictation_shortcut(None)          # Paused while new keys are recorded.
    assert registered == []


def test_cmd_r_is_registered_on_the_key_that_gives_r_with_command(monkeypatch):
    manager, registered = fake_manager()
    monkeypatch.setattr(hotkeys, "command_key_code", {"r": 0x1F}.get)     # Dvorak's R key.
    manager.set_recording_shortcut(lambda: None)
    assert registered == [0x1F] and STOP.key_code == 0x0F
    manager.set_recording_shortcut(None)
    assert registered == []
    monkeypatch.setattr(hotkeys, "command_key_code", lambda character: None)
    with pytest.raises(HotKeyError, match=r"Cmd\+R"):
        manager.set_recording_shortcut(lambda: None)
