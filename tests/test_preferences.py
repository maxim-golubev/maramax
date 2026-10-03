"""Exercise native settings in a separate process with all windows hidden."""

import subprocess
import sys


def test_settings_edit_save_reload_and_remove_without_showing_ui(tmp_path):
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
from parakeet_dictation.preferences import PreferencesController
from parakeet_dictation.config import AppConfig
from parakeet_dictation.app import _SETTING_LABELS
from parakeet_dictation.hotkeys import DEFAULT_DICTATE

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
path = Path(sys.argv[1]) / "settings.json"
config = AppConfig()
delegate = SimpleNamespace(
    config=config, is_busy=False,
    transcriber=SimpleNamespace(load_error=None, status_message=lambda: "Speech model ready"),
    qwen=SimpleNamespace(status_message=lambda: "Loading the high-accuracy model…"),
    updates=SimpleNamespace(status_text=lambda: "This is the newest version.", can_check=lambda: True),
    current_shortcut=lambda: DEFAULT_DICTATE, choose_shortcut=lambda key, modifiers: None,
    pause_shortcut=lambda: None, resume_shortcut=lambda: None,
)
def replace_word_rules(rules):
    config.replacements = rules
    config.save(path)
    return True
delegate.replace_word_rules = replace_word_rules
panel = PreferencesController.alloc().initWithDelegate_labels_(delegate, _SETTING_LABELS)
editor = panel.replacements
heard_column, replacement_column = editor.table.tableColumns()
def saved():
    return AppConfig.load(path).replacements
def add(heard, replacement):
    editor.heard.setStringValue_(heard)
    editor.replacement.setStringValue_(replacement)
    editor.addRule_(None)
def shown(column):
    return [str(editor.tableView_objectValueForTableColumn_row_(editor.table, column, row))
            for row in range(editor.numberOfRowsInTableView_(editor.table))]
def note():
    return str(editor.note.stringValue())
assert "No replacements yet" in note()
add("mara max", "Maramax")
add("lira", "Lyra")
add("Kairos", "Cairos")
# Adding never replaces another rule, and every rule is on view, alphabetically.
assert sorted(rule["heard"] for rule in saved()) == ["Kairos", "lira", "mara max"], saved()
assert shown(heard_column) == ["Kairos", "lira", "mara max"] and shown(replacement_column) == ["Cairos", "Lyra", "Maramax"]
assert editor._selected_rows() == [0] and "Added" in note() and str(editor.heard.stringValue()) == ""
assert str(editor.count.stringValue()) == "3 replacements"
# Words another rule covers are refused, never merged: the rule is pointed out and the typing kept.
add("LIRA ", "Other")
assert len(saved()) == 3 and {"heard": "lira", "replacement": "Lyra"} in saved()
assert "“lira” is already in the list, replaced with “Lyra”." == note() and editor._selected_rows() == [1]
assert str(editor.heard.stringValue()) == "LIRA "
add("", "")
assert note() == "Enter the words the transcript says." and len(saved()) == 3
# Typing words that already have a rule says so at once.
editor.heard.setStringValue_("mara  MAX")
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.heard))
assert editor._selected_rows() == [2] and "already in the list" in note()
editor.heard.setStringValue_("")
# Changed in place: only that rule changes.
editor.tableView_setObjectValue_forTableColumn_row_(editor.table, "Lyra!", replacement_column, 1)
assert sorted(map(str, saved()), key=str) == sorted(map(str, [{"heard": "Kairos", "replacement": "Cairos"},
    {"heard": "lira", "replacement": "Lyra!"}, {"heard": "mara max", "replacement": "Maramax"}]))
editor.tableView_setObjectValue_forTableColumn_row_(editor.table, "kairos", heard_column, 1)   # Taken by another.
assert "already in the list" in note() and "not saved" in note()
assert {"heard": "lira", "replacement": "Lyra!"} in saved() and shown(heard_column)[1] == "lira"
editor.tableView_setObjectValue_forTableColumn_row_(editor.table, "  ", replacement_column, 1)  # Empty.
assert {"heard": "lira", "replacement": "Lyra!"} in saved()
# Remove takes the selected rules (Delete does the same), and Undo brings them back.
editor._select([0, 2])
editor.removeRules_(None)
assert saved() == [{"heard": "lira", "replacement": "Lyra!"}] and note() == "Removed 2 replacements."
assert not editor.undo.isHidden() and not editor.remove.isEnabled()
editor.undoRemoval_(None)
assert len(saved()) == 3 and editor.undo.isHidden()
# Through AppKit, as a double-click does: the cell's editor opens, and Return saves.
panel.show_tab(2)
editor.table.editColumn_row_withEvent_select_(1, 1, None, True)
assert str(editor.table.currentEditor().string()) == "Lyra!"
editor.table.currentEditor().setString_("Lyra")
editor.table.currentEditor().insertNewline_(None)
assert {"heard": "lira", "replacement": "Lyra"} in saved() and note() == "Change saved."
editor._select([1])
from AppKit import NSEvent, NSEventTypeKeyDown
delete = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
    NSEventTypeKeyDown, (0, 0), 0, 0, 0, None, "\x7f", "\x7f", False, 51)
editor.table.keyDown_(delete)
assert [rule["heard"] for rule in saved()] == ["Kairos", "mara max"] and note() == "Removed “lira”."
# A replacement of several lines is shown on one line and not edited in a one-line cell.
add("sig", "Kind regards,\nMaxim")
row = shown(heard_column).index("sig")
assert shown(replacement_column)[row] == "Kind regards, ⏎ Maxim"
assert not editor.tableView_shouldEditTableColumn_row_(editor.table, replacement_column, row)
assert editor.tableView_shouldEditTableColumn_row_(editor.table, heard_column, row)
# Try it shows a sentence with the replacements applied.
editor.trial.setStringValue_("mara max met kairos")
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.trial))
assert str(editor.trial_result.stringValue()) == "Maramax met Cairos" and not editor.trial_result.isHidden()
for rule in ("sig", "Kairos", "mara max"):
    editor._select([shown(heard_column).index(rule)])
    editor.removeRules_(None)
assert not saved() and note() == "Removed “mara max”."
# Return in either field adds; Tab to the next field does not.
editor.heard.setStringValue_("mara max")
editor.replacement.setStringValue_("Maramax")
panel.show_tab(2)
panel.panel.makeFirstResponder_(editor.heard)
panel.panel.fieldEditor_forObject_(True, editor.heard).insertTab_(None)
assert not saved()
panel.panel.fieldEditor_forObject_(True, editor.replacement).insertNewline_(None)
assert saved() == [{"heard": "mara max", "replacement": "Maramax"}]
editor._select([0])
editor.removeRules_(None)
assert not saved()
# A failed save says so instead of claiming success.
delegate.replace_word_rules = lambda rules: setattr(config, "replacements", rules) or False
add("lira", "Lyra")
assert "could not be saved" in note() and config.replacements == [{"heard": "lira", "replacement": "Lyra"}]
config.replacements = []
delegate.replace_word_rules = replace_word_rules
editor.refresh()
from parakeet_dictation.helper_protocol import InputDevice
calls = []
delegate.refresh_input_devices = lambda: calls.append("refresh")
delegate.select_input_device = lambda name: calls.append(name)
assert len(panel.pages) == 3
panel.show_tab(0)
assert not panel.pages[0].isHidden()
assert panel.pages[1].isHidden()
# The tabs are a Settings toolbar, each with its symbol; the window is named for the tab.
items = panel.panel.toolbar().items()
assert [str(item.label()) for item in items] == ["General", "Microphone", "Words"]
assert all(item.image() is not None for item in items)
panel.selectTab_(items[1])
assert calls == ["refresh"] and str(panel.panel.title()) == "Microphone"
assert str(panel.panel.toolbar().selectedItemIdentifier()) == "Microphone"
assert panel.pages[0].isHidden()
assert not panel.pages[1].isHidden()
panel.update_input_devices([InputDevice(1, "AirPods", False)], "AirPods")
panel.selectDevice_(None)
assert calls[-1] == "AirPods"
panel.update_input_devices([], "Disconnected mic")
assert panel.device_picker.titleOfSelectedItem() == "Disconnected mic"
panel.update_input_devices([InputDevice(0, "MacBook Pro Microphone", True)], None, "MacBook Pro Microphone")
assert panel.device_picker.titleOfSelectedItem() == "Automatic — MacBook Pro Microphone"
panel.selectDevice_(None)
assert calls[-1] is None
delegate.is_busy = True
panel.show_busy_state()                  # Told when a dictation starts, not only on a refresh.
assert not panel.device_picker.isEnabled()
delegate.is_busy = False
panel.show_busy_state()
assert panel.device_picker.isEnabled()
delegate.set_keep_microphone_ready = lambda seconds: calls.append(seconds) or setattr(config, "keep_mic_ready_seconds", seconds)
assert panel.keep_ready.titleOfSelectedItem() == "Off"
panel.keep_ready.selectItemWithTitle_("2 minutes")
panel.selectKeepReady_(None)
assert calls[-1] == 120
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "2 minutes"
config.keep_mic_ready_seconds = 45  # Hand-edited settings stay visible instead of snapping to a preset.
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "45 seconds"
config.keep_mic_ready_seconds = 60
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "1 minute"
assert panel.model_retry.isHidden()
assert set(panel.options) == set(_SETTING_LABELS)
# Every tab's window ends the same distance below its content.
from parakeet_dictation.preferences import MARGIN, duration_label
content = panel.panel.contentView()
for index, page in enumerate(panel.pages):
    panel.show_tab(index)
    content.layoutSubtreeIfNeeded()
    assert abs(page.frame().origin.y - MARGIN) < 1, (index, page.frame())
assert [duration_label(n) for n in (0, 1, 30, 60, 120, 90)] == ["Off", "1 second", "30 seconds", "1 minute", "2 minutes", "90 seconds"]
# Pasting copies whatever the copy box says, so while it is on the box shows that and cannot be changed.
copy = panel.options["auto_copy_to_clipboard"]
config.auto_copy_to_clipboard, config.paste_to_active_app = False, True
panel.refresh()
assert copy.state() == 1 and not copy.isEnabled()
config.paste_to_active_app = False
panel.refresh()
assert copy.state() == 0 and copy.isEnabled()
config.auto_copy_to_clipboard = True
config.high_accuracy = True
panel.refresh()
assert str(panel.model_status.stringValue()) == "Speech model ready. Loading the high-accuracy model."
# The version is at the top of General; Check Now asks the updater and shows what it says.
from parakeet_dictation import __version__
def labels(view):
    found = [str(view.stringValue())] if hasattr(view, "stringValue") and view.isKindOfClass_(__import__("AppKit").NSTextField) else []
    return found + [text for child in view.subviews() for text in labels(child)]
general = labels(panel.pages[0])
assert general[:2] == ["Maramax", f"Version {__version__}"], general[:3]
# Automatic prefers the Mac's own microphone by default, and macOS shows no
# Accessibility prompt of its own: the help says what really happens.
assert any(text.startswith("Automatic uses the Mac’s own microphone") for text in labels(panel.pages[1]))
assert any("turn Maramax on there" in text for text in general)
assert str(panel.update_status.stringValue()) == "This is the newest version." and panel.update_check.isEnabled()
checks = []
delegate.updates = SimpleNamespace(status_text=lambda: "Checking for updates…", can_check=lambda: False,
                                   check_requested=lambda: checks.append("check"))
panel.checkForUpdates_(None)
assert checks == ["check"]
assert str(panel.update_status.stringValue()) == "Checking for updates…" and not panel.update_check.isEnabled()
# Clicking another app must not make a window of a Dock-less app vanish.
assert not panel.panel.hidesOnDeactivate()
assert not panel.panel.isVisible() and not panel.shows_microphones()
# Other shortcut… records keys; another tab, or closing Settings, gives the global shortcut back.
from AppKit import NSMakeRect
shortcut_calls = []
delegate.pause_shortcut = lambda: shortcut_calls.append("pause")
delegate.resume_shortcut = lambda: shortcut_calls.append("resume")
delegate.problem_with_shortcut = lambda key, modifiers: (
    "That key cannot be a shortcut. Use a letter, digit, punctuation key, Space, or F1–F12.")
picker = panel.shortcut_picker
def choose_other():
    panel.show_tab(0)
    picker.popup.selectItemAtIndex_(picker.popup.numberOfItems() - 1)
    picker.chooseItem_(None)
def check_now_bottom():
    content.layoutSubtreeIfNeeded()
    return panel.update_check.convertRect_toView_(panel.update_check.bounds(), None).origin.y
panel.show_tab(0)
bottom = check_now_bottom()
choose_other()
assert shortcut_calls == ["pause"] and picker.is_recording()
picker.key_pressed(0x24, 0)                      # Return: refused with two lines of explanation.
content.layoutSubtreeIfNeeded()
note = picker.note
needed = note.cell().cellSizeForBounds_(NSMakeRect(0, 0, note.frame().size.width, 10000)).height
assert note.frame().size.height >= needed - 0.5, (note.frame(), needed)   # "or press Esc." is not cut off.
assert note.preferredMaxLayoutWidth() <= note.frame().size.width + 0.5, note.frame()   # It wraps where it ends.
assert abs(check_now_bottom() - bottom) < 1, (check_now_bottom(), bottom)   # The window grew to fit it.
panel.show_tab(2)                                # Typing a replacement is not choosing a shortcut.
assert shortcut_calls == ["pause", "resume"] and not picker.is_recording()
choose_other()
panel.windowWillClose_(None)
assert shortcut_calls == ["pause", "resume", "pause", "resume"] and not picker.is_recording()
# A cell still being edited is saved to its own rule before Remove, Add, or Undo change the list.
config.replacements = []
editor.refresh()
for heard in ("gamma", "alpha", "beta"):
    add(heard, heard.upper())
assert [rule["heard"] for rule in saved()] == ["alpha", "beta", "gamma"]   # Saved in the order shown.
panel.show_tab(2)
editor._select([0])
editor.table.editColumn_row_withEvent_select_(1, 0, None, True)
editor.table.currentEditor().setString_("X")
panel.refresh()                                   # A refresh for another reason leaves the edit open...
assert editor.table.currentEditor() is not None and str(editor.table.currentEditor().string()) == "X"
editor.removeRules_(None)                         # ...and Remove first saves it, to alpha.
assert saved() == [{"heard": "beta", "replacement": "BETA"}, {"heard": "gamma", "replacement": "GAMMA"}], saved()
editor.undoRemoval_(None)
assert saved()[0] == {"heard": "alpha", "replacement": "X"} and len(saved()) == 3
editor._select([2])
editor.table.editColumn_row_withEvent_select_(1, 2, None, True)
editor.table.currentEditor().setString_("G")
add("aardvark", "Aardvark")                       # Sorts above the row being edited.
assert {"heard": "gamma", "replacement": "G"} in saved() and {"heard": "beta", "replacement": "BETA"} in saved()
# The window grows with the Words page: a long Try it result is never cut off.
editor.trial.setStringValue_("alpha beta gamma " * 30)
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.trial))
content.layoutSubtreeIfNeeded()
assert abs(panel.pages[2].frame().origin.y - MARGIN) < 1, panel.pages[2].frame()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-1500:]
