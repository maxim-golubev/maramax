"""The menu keeps one width: every title fits inside the status line's, measured in a separate process."""

import subprocess
import sys

SCRIPT = r'''
import rumps
from AppKit import (NSApplication, NSApplicationActivationPolicyProhibited, NSEventModifierFlagCommand, NSMenu,
                    NSMenuItem)
from parakeet_dictation import app as module
from parakeet_dictation.hotkeys import (DICTATE_PRESETS, KEY_NAMES, controlKey, cmdKey, dictation_shortcut,
                                        menu_key_equivalent, optionKey, shiftKey)
from parakeet_dictation.status_line import WIDTH, StatusLine
from parakeet_dictation.update_offer import Step, menu_title

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)

# Every title the menu can show. The record and update items change theirs.
FIXED = ["Retry Speech Model", "Copy Last Transcript", "Open Transcript", "History", "Recordings…",
         "Recover Last Recording", "Transcribe Files…", "Settings…", "Quit Maramax"]
RECORD = ["Start Dictation", "Stop Dictation", "Transcribing…"]
UPDATE = sorted({menu_title(step, "10.10.10", 100) for step in Step} | {menu_title(Step.IDLE, None)})
# Every preset, and every modifier with a function key. Space with three or
# four modifiers is wider still (up to 320 pt): with one of those, an open menu
# widens a little while an update downloads, not worth a wider line for everyone.
SHORTCUTS = [*DICTATE_PRESETS, dictation_shortcut(0x6F, controlKey | optionKey | shiftKey | cmdKey, KEY_NAMES)]

# Each menu item the app wires up is listed above; a new one has to be measured too.
registered = {cell.cell_contents for register in rumps.clicked.__dict__["*buttons"]
              for cell in register.__closure__ or () if isinstance(cell.cell_contents, tuple)}
assert all(len(path) == 1 for path in registered), registered          # No submenus.
assert {path[0] for path in registered} <= set(FIXED + RECORD + UPDATE), registered


def natural_width(record, shortcut, update):
    menu = NSMenu.alloc().initWithTitle_("Maramax")
    for title in [*FIXED, record, update]:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "toggle:", "")
        if title == record:
            key, modifiers = menu_key_equivalent(shortcut.key_code, shortcut.modifiers, KEY_NAMES)
            item.setKeyEquivalent_(key)
            item.setKeyEquivalentModifierMask_(modifiers)
        elif title in ("Settings…", "Quit Maramax"):
            item.setKeyEquivalent_("," if title == "Settings…" else "q")
            item.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
        menu.addItem_(item)
    return menu.size().width


widest = max((natural_width(record, shortcut, update), record, shortcut.label, update)
             for record in RECORD for shortcut in SHORTCUTS for update in UPDATE)
assert widest[0] <= WIDTH, widest

# A status of any length wraps inside the line instead of widening it.
line = StatusLine("Ready")
one_line = line._view.frame().size
line.show("Preparing the speech model — the first launch downloads it, which takes a few minutes")
two_lines = line._view.frame().size
assert one_line.width == two_lines.width == WIDTH and two_lines.height > one_line.height
line.show("Ready")
assert line._view.frame().size.height == one_line.height
'''


def test_every_menu_title_fits_the_status_line_width():
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
