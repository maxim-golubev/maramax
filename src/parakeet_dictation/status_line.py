"""The status line at the top of the menu bar menu, which wraps at a fixed width so the menu never changes width."""

from __future__ import annotations

import rumps
from AppKit import NSColor, NSFont, NSMakeRect, NSTextField, NSView

# The menu's width. Every other item's title must fit inside it, or the menu
# would widen for that item (tests/test_status_line.py measures them); then
# no status, however long, changes the width: it wraps onto another line.
WIDTH = 300
# Where menu item titles start and end, so the status lines up with them.
LEADING = 14
TRAILING = 14
# Above and below the text, as much as a menu item has around its title.
VERTICAL = 3
# A text field draws its text this far inside its frame.
_TEXT_INSET = 2


class StatusLine(rumps.MenuItem):
    """A menu item that shows the app's status as secondary text. macOS
    sizes a menu to its widest title and, while it is open, widens it for a
    longer one but never narrows it again; a fixed-width line that wraps
    keeps the menu one width whatever the status says."""

    def __init__(self, text: str):
        super().__init__("Status")
        self._label = NSTextField.wrappingLabelWithString_("")
        self._label.setFont_(NSFont.menuFontOfSize_(0))
        self._label.setTextColor_(NSColor.secondaryLabelColor())
        self._label.setSelectable_(False)
        self._label.setPreferredMaxLayoutWidth_(self._text_width())
        self._view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 0))
        self._view.addSubview_(self._label)
        self._menuitem.setView_(self._view)
        self.text = ""
        self.show(text)

    @staticmethod
    def _text_width() -> float:
        return WIDTH - LEADING - TRAILING + 2 * _TEXT_INSET

    def show(self, text: str) -> None:
        """Say `text`. An open menu takes the new height at once."""
        if text == self.text:
            return
        self.text = text
        self._label.setStringValue_(text)
        height = self._label.fittingSize().height
        self._label.setFrame_(NSMakeRect(LEADING - _TEXT_INSET, VERTICAL, self._text_width(), height))
        self._view.setFrameSize_((WIDTH, height + 2 * VERTICAL))
