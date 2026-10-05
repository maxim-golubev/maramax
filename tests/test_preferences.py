"""Exercise native settings in a separate process with all windows hidden."""

import subprocess
import sys


def test_settings_edit_save_reload_and_remove_without_showing_ui(tmp_path):
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
from parakeet_dictation.preferences import SETTING_LABELS, PreferencesController, _DELIVERY_HELP
from parakeet_dictation.config import AppConfig, Delivery
_DELIVERY_HELP_KEPT = _DELIVERY_HELP[Delivery.KEPT]
from parakeet_dictation.hotkeys import DEFAULT_DICTATE

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
path = Path(sys.argv[1]) / "settings.json"
config = AppConfig()
delegate = SimpleNamespace(
    config=config, is_busy=False,
    transcriber=SimpleNamespace(status_message=lambda: "Speech model ready"), models_failed=lambda: False,
    qwen=SimpleNamespace(status_message=lambda: "Loading the high-accuracy model…"),
    updates=SimpleNamespace(status_text=lambda: "This is the newest version.", can_check=lambda: True),
    current_shortcut=lambda: DEFAULT_DICTATE, choose_shortcut=lambda key, modifiers: None,
    pause_shortcut=lambda: None, resume_shortcut=lambda: None, paste_permitted=lambda: False,
)
def replace_word_rules(rules):
    config.replacements = rules
    config.save(path)
    return True
delegate.replace_word_rules = replace_word_rules
panel = PreferencesController.alloc().initWithDelegate_(delegate)
editor = panel.replacements
heard_column, replacement_column = editor.table.tableColumns()
def saved():
    return AppConfig.load(path).replacements
def add(heard, replacement):
    editor.heard.setStringValue_(heard)
    editor.replacement.setStringValue_(replacement)
    editor.addRule_(None)
def cell(column, row):
    return editor.table.viewAtColumn_row_makeIfNecessary_(
        editor.table.columnWithIdentifier_(column.identifier()), row, True).textField()
def shown(column):
    return [str(cell(column, row).stringValue()) for row in range(editor.numberOfRowsInTableView_(editor.table))]
def type_into(column, row, text):
    """What finishing an edit in a cell does: its text field sends its action."""
    cell(column, row).setStringValue_(text)
    editor.ruleEdited_(cell(column, row))
def field_editor():
    responder = panel.panel.firstResponder()
    return responder if responder.isKindOfClass_(__import__("AppKit").NSTextView) else None
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
type_into(replacement_column, 1, "Lyra!")
assert sorted(map(str, saved()), key=str) == sorted(map(str, [{"heard": "Kairos", "replacement": "Cairos"},
    {"heard": "lira", "replacement": "Lyra!"}, {"heard": "mara max", "replacement": "Maramax"}]))
type_into(heard_column, 1, "kairos")   # Taken by another.
assert "already in the list" in note() and "not saved" in note()
assert {"heard": "lira", "replacement": "Lyra!"} in saved() and shown(heard_column)[1] == "lira"
type_into(replacement_column, 1, "  ")  # Empty.
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
assert str(field_editor().string()) == "Lyra!"
field_editor().setString_("Lyra")
field_editor().insertNewline_(None)
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
assert not cell(replacement_column, row).isEditable() and cell(heard_column, row).isEditable()
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
calls = []
delegate.refresh_input_devices = lambda: calls.append("refresh")
delegate.select_input_device = lambda name: calls.append(name)
assert len(panel.pages) == 4
panel.show_tab(0)
assert not panel.pages[0].isHidden()
assert panel.pages[1].isHidden()
# The tabs are a Settings toolbar, each with its symbol; the window is named for the tab.
items = panel.panel.toolbar().items()
assert [str(item.label()) for item in items] == ["General", "Microphone", "Words", "Advanced"]
assert all(item.image() is not None for item in items)
panel.selectTab_(items[1])
assert calls == ["refresh"] and str(panel.panel.title()) == "Microphone"
assert str(panel.panel.toolbar().selectedItemIdentifier()) == "Microphone"
assert panel.pages[0].isHidden()
assert not panel.pages[1].isHidden()
assert str(panel.device_picker.titleOfSelectedItem()) == "Automatic"            # Nothing listed yet.
panel.update_input_devices(None, "AirPods")                                       # Chosen, not listed yet:
assert str(panel.device_picker.titleOfSelectedItem()) == "AirPods"               # never "(not connected)".
panel.update_input_devices(["AirPods"], "AirPods")
panel.selectDevice_(None)
assert calls[-1] == "AirPods"
panel.update_input_devices([], "Disconnected mic")
assert panel.device_picker.titleOfSelectedItem() == "Disconnected mic (not connected)"   # Not one that will record.
panel.selectDevice_(None)
assert calls[-1] == "Disconnected mic"                                                 # Still the name saved.
panel.update_input_devices(["MacBook Pro Microphone"], None, "MacBook Pro Microphone")
assert panel.device_picker.titleOfSelectedItem() == "Automatic — MacBook Pro Microphone"
panel.selectDevice_(None)
assert calls[-1] is None
delegate.is_busy = True
panel.show_busy_state()                  # Told when a dictation starts, not only on a refresh.
assert not panel.device_picker.isEnabled() and not panel.clear_history.isEnabled()
delegate.is_busy = False
panel.show_busy_state()
assert panel.device_picker.isEnabled() and panel.clear_history.isEnabled()
delegate.set_keep_microphone_ready = lambda seconds: calls.append(seconds) or setattr(config, "keep_mic_ready_seconds", seconds)
assert panel.keep_ready.titleOfSelectedItem() == "Off"
panel.keep_ready.selectItemWithTitle_("For 2 minutes")
panel.selectKeepReady_(None)
assert calls[-1] == 120
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "For 2 minutes"
config.keep_mic_ready_seconds = 45  # Hand-edited settings stay visible instead of snapping to a preset.
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "For 45 seconds"
config.keep_mic_ready_seconds = 60
panel.refresh()
assert panel.keep_ready.titleOfSelectedItem() == "For 1 minute"
assert panel.model_retry.isHidden()
delegate.models_failed = lambda: True                      # The standard model, or the chosen high-accuracy one.
panel.refresh()
assert not panel.model_retry.isHidden()
delegate.models_failed = lambda: False
panel.refresh()
assert set(panel.options) == set(SETTING_LABELS)
# Every tab's window ends the same distance below its content.
from parakeet_dictation.preferences import CONTENT_WIDTH, MARGIN, duration_label
content = panel.panel.contentView()
for index, page in enumerate(panel.pages):
    panel.show_tab(index)
    content.layoutSubtreeIfNeeded()
    assert abs(page.frame().origin.y - MARGIN) < 1, (index, page.frame())
assert [duration_label(n) for n in (0, 1, 30, 60, 120, 90)] == [
    "Off", "For 1 second", "For 30 seconds", "For 1 minute", "For 2 minutes", "For 90 seconds"]
# Where a transcript goes is one choice of three; keeping it in Maramax is said plainly.
chosen = []
delegate.choose_delivery = lambda delivery: chosen.append(delivery) or config.set_delivery(delivery)
def shown_delivery():
    return [delivery for delivery, button in panel.delivery_buttons.items() if button.state() == 1]
assert shown_delivery() == [Delivery.COPIED] and panel.permission_row.isHidden()
kept_help = panel.delivery_help[Delivery.KEPT]
assert str(kept_help.stringValue()) == _DELIVERY_HELP_KEPT                            # No symbol: plain help.
panel.chooseDelivery_(panel.delivery_buttons[Delivery.KEPT])
panel.refresh()
assert chosen == [Delivery.KEPT] and shown_delivery() == [Delivery.KEPT]
assert str(kept_help.stringValue()) == "\ufffc " + _DELIVERY_HELP_KEPT                 # The warning symbol leads it.
assert str(kept_help.stringValue()).endswith("Transcripts wait in Open Transcript and History.")
panel.chooseDelivery_(panel.delivery_buttons[Delivery.PASTED])
panel.refresh()
assert str(kept_help.stringValue()) == _DELIVERY_HELP_KEPT                            # Plain again.
assert shown_delivery() == [Delivery.PASTED] and not panel.permission_row.isHidden()
assert "Not allowed yet" in str(panel.permission_note.stringValue()) and not panel.permission_button.isHidden()
asked = []
delegate.request_paste_permission = lambda: asked.append("ask")
panel.requestPastePermission_(None)
assert asked == ["ask"]
delegate.paste_permitted = lambda: True
panel.refresh()
assert "Allowed" in str(panel.permission_note.stringValue()) and panel.permission_button.isHidden()
config.set_delivery(Delivery.COPIED)
panel.refresh()
config.high_accuracy = True
panel.refresh()
assert str(panel.model_status.stringValue()) == "Speech model ready. Loading the high-accuracy model."
config.high_accuracy = False
# The guide and clearing history are a click away, in General and Advanced.
opened = []
delegate.show_welcome = lambda: opened.append("welcome")
delegate.clear_history_requested = lambda: opened.append("clear")
panel.showWelcome_(None)
panel.clearHistory_(None)
assert opened == ["welcome", "clear"]
# How many transcripts History keeps: 100 by default, a hand-edited number kept on view.
delegate.set_history_limit = lambda count: calls.append(count) or setattr(config, "history_limit", count)
assert panel.history_limit.titleOfSelectedItem() == "100 transcripts"
panel.history_limit.selectItemWithTitle_("1,000 transcripts")
panel.selectHistoryLimit_(None)
assert calls[-1] == 1000
panel.refresh()
assert panel.history_limit.titleOfSelectedItem() == "1,000 transcripts"
config.history_limit = 37
panel.refresh()
assert panel.history_limit.titleOfSelectedItem() == "37 transcripts"
config.history_limit = 1
panel.refresh()
assert panel.history_limit.titleOfSelectedItem() == "1 transcript"
config.history_limit = 100
# The version is at the top of General; Check Now asks the updater and shows what it says.
from parakeet_dictation import __version__
def labels(view):
    found = [str(view.stringValue())] if hasattr(view, "stringValue") and view.isKindOfClass_(__import__("AppKit").NSTextField) else []
    return found + [text for child in view.subviews() for text in labels(child)]
general = labels(panel.pages[0])
# And how many recordings: 20 by default, under the archive's size cap either way.
assert panel.recordings_limit.titleOfSelectedItem() == "20 recordings"
delegate.set_recordings_limit = lambda count: calls.append(count) or setattr(config, "recordings_limit", count)
panel.recordings_limit.selectItemWithTitle_("100 recordings")
panel.selectRecordingsLimit_(None)
assert calls[-1] == 100
assert any("under 512 MB" in text for text in labels(panel.pages[3]))
assert general[:2] == ["Maramax", f"Version {__version__}"], general[:3]
# Automatic prefers the Mac's own microphone by default: the help says what really happens.
assert any(text.startswith("Automatic uses the Mac’s own microphone") for text in labels(panel.pages[1]))
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
def last_option_bottom():
    content.layoutSubtreeIfNeeded()
    last = panel.options["live_preview"]
    return last.convertRect_toView_(last.bounds(), None).origin.y
panel.show_tab(0)
bottom = last_option_bottom()
choose_other()
assert shortcut_calls == ["pause"] and picker.is_recording()
picker.key_pressed(0x24, 0)                      # Return: refused with two lines of explanation.
content.layoutSubtreeIfNeeded()
note = picker.note
needed = note.cell().cellSizeForBounds_(NSMakeRect(0, 0, note.frame().size.width, 10000)).height
assert note.frame().size.height >= needed - 0.5, (note.frame(), needed)   # "or press Esc." is not cut off.
assert note.preferredMaxLayoutWidth() <= note.frame().size.width + 0.5, note.frame()   # It wraps where it ends.
assert abs(last_option_bottom() - bottom) < 1, (last_option_bottom(), bottom)   # The window grew to fit it.
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
field_editor().setString_("X")
panel.refresh()                                   # A refresh for another reason leaves the edit open...
assert field_editor() is not None and str(field_editor().string()) == "X"
editor.removeRules_(None)                         # ...and Remove first saves it, to alpha.
assert saved() == [{"heard": "beta", "replacement": "BETA"}, {"heard": "gamma", "replacement": "GAMMA"}], saved()
editor.undoRemoval_(None)
assert saved()[0] == {"heard": "alpha", "replacement": "X"} and len(saved()) == 3
editor._select([2])
editor.table.editColumn_row_withEvent_select_(1, 2, None, True)
field_editor().setString_("G")
add("aardvark", "Aardvark")                       # Sorts above the row being edited.
assert {"heard": "gamma", "replacement": "G"} in saved() and {"heard": "beta", "replacement": "BETA"} in saved()
from AppKit import NSAppearance
panel.panel.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
def ink_left(rect):
    """Where the first dark pixel inside `rect` (window points) is drawn."""
    rep = content.bitmapImageRepForCachingDisplayInRect_(content.bounds())
    content.cacheDisplayInRect_toBitmapImageRep_(content.bounds(), rep)
    scale, height = rep.pixelsWide() / content.bounds().size.width, content.bounds().size.height
    rows = range(int((height - rect.origin.y - rect.size.height) * scale), int((height - rect.origin.y) * scale))
    for x in range(int(rect.origin.x * scale), int((rect.origin.x + rect.size.width) * scale)):
        for y in rows:
            colour = rep.colorAtX_y_(x, y)
            if colour.redComponent() + colour.greenComponent() + colour.blueComponent() < 1.2:
                return x / scale
    raise AssertionError(f"nothing drawn in {rect}")
# Each column's text is centred in its row and starts where the text of the
# field that adds to it does, so the fields read as the list's next row.
content.layoutSubtreeIfNeeded()
def text_rect(field):
    return field.convertRect_toView_(field.cell().drawingRectForBounds_(field.bounds()), None)
row_rect = editor.table.convertRect_toView_(editor.table.rectOfRow_(0), None)
for column, field in ((heard_column, editor.heard), (replacement_column, editor.replacement)):
    text = text_rect(cell(column, 0))
    assert abs(text.origin.x - text_rect(field).origin.x) < 0.5, (text, text_rect(field))
    assert abs((text.origin.y + text.size.height / 2) - (row_rect.origin.y + row_rect.size.height / 2)) < 0.5
    # Its title starts where its text does, as drawn: the header pads its title inside its own rectangle.
    header = editor.table.headerView()
    column_index = editor.table.columnWithIdentifier_(column.identifier())
    title_ink = ink_left(header.convertRect_toView_(header.headerRectOfColumn_(column_index), None))
    cell_ink = ink_left(editor.table.convertRect_toView_(editor.table.frameOfCellAtColumn_row_(column_index, 0), None))
    assert abs(title_ink - cell_ink) <= 1, (title_ink, cell_ink)   # Within a glyph's own side bearing.
def visible(view):
    return view.convertRect_toView_(view.alignmentRectForFrame_(view.frame()), None) if view.superview() is None else \
        view.superview().convertRect_toView_(view.alignmentRectForFrame_(view.frame()), None)
list_rect = editor.table.enclosingScrollView().convertRect_toView_(editor.table.enclosingScrollView().bounds(), None)
add_button = editor.heard.superview().views()[-1]
assert abs(visible(add_button).origin.x + visible(add_button).size.width - (list_rect.origin.x + list_rect.size.width)) < 0.5
assert editor.heard.frame().size.width == int(editor.heard.frame().size.width)   # Edges on whole points.
editor.trial.setStringValue_("Kairos")
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.trial))
content.layoutSubtreeIfNeeded()
result = editor.trial_result
result_text = result.convertRect_toView_(result.cell().drawingRectForBounds_(result.bounds()), None)
assert abs(result_text.origin.x - text_rect(editor.trial).origin.x) < 0.5     # Under the typed sentence.
editor.trial.setStringValue_("")
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.trial))
# The bar's Reset Position is there only while it has been moved, and ends where the page does.
panel.show_tab(0)
content.layoutSubtreeIfNeeded()
assert not panel.bar_reset.isEnabled()
assert abs(visible(panel.bar_reset).origin.x + visible(panel.bar_reset).size.width - (MARGIN + CONTENT_WIDTH)) < 0.5
config.bar_position = [0.2, 0.8]
panel.refresh()
assert panel.bar_reset.isEnabled()
delegate.reset_bar_position = lambda: calls.append("bar reset")
panel.resetBarPosition_(None)
assert calls[-1] == "bar reset"
config.bar_position = None
# On the Microphone tab the picker and Refresh end where the page's text does.
panel.show_tab(1)
content.layoutSubtreeIfNeeded()
refresh = panel.device_picker.superview().views()[-1]
assert abs(visible(refresh).origin.x + visible(refresh).size.width - (MARGIN + CONTENT_WIDTH)) < 0.5
panel.show_tab(2)
# The window grows with the Words page: a long Try it result is never cut off.
editor.trial.setStringValue_("alpha beta gamma " * 30)
editor.controlTextDidChange_(SimpleNamespace(object=lambda: editor.trial))
content.layoutSubtreeIfNeeded()
assert abs(panel.pages[2].frame().origin.y - MARGIN) < 1, panel.pages[2].frame()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-1500:]
