"""The control for choosing the dictation shortcut: a recommended preset, or keys the user presses."""

from __future__ import annotations

import objc
from AppKit import (
    NSColor, NSEvent, NSEventMaskKeyDown, NSFont, NSMakeRect, NSMenuItem, NSPopUpButton, NSStackView,
    NSTextField, NSUserInterfaceLayoutOrientationVertical, NSLayoutAttributeLeading,
)
from Foundation import NSObject

from .hotkeys import DICTATE_PRESETS, carbon_modifiers, shortcut_problem

OTHER = "Other shortcut…"
_ESCAPE = 0x35


class ShortcutPicker(NSObject):
    """`owner` provides current_shortcut(), choose_shortcut(key_code,
    modifiers) -> problem or None, pause_shortcut(), and resume_shortcut().
    While keys are being recorded the global shortcut is paused, so pressing
    the current one is seen here rather than starting a dictation."""

    def initWithOwner_width_(self, owner, width):
        self = objc.super(ShortcutPicker, self).init()
        if self is None:
            return None
        self.owner = owner
        self._monitor = None
        self._choices = []
        self.popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.popup.setTarget_(self)
        self.popup.setAction_("chooseItem:")
        self.popup.widthAnchor().constraintEqualToConstant_(260).setActive_(True)
        self.note = NSTextField.wrappingLabelWithString_("")
        self.note.setFont_(NSFont.systemFontOfSize_(11))
        self.note.setTextColor_(NSColor.secondaryLabelColor())
        self.note.setPreferredMaxLayoutWidth_(width)
        self.note.setHidden_(True)
        self.view = NSStackView.stackViewWithViews_([self.popup, self.note])
        self.view.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        self.view.setAlignment_(NSLayoutAttributeLeading)
        self.view.setSpacing_(4)
        self.refresh()
        return self

    @objc.python_method
    def refresh(self):
        """Show the current shortcut, the presets, and Other."""
        current = self.owner.current_shortcut()
        self._choices = list(DICTATE_PRESETS)
        if current not in self._choices:
            self._choices.insert(0, current)
        self.popup.removeAllItems()
        for choice in self._choices:
            title = f"{choice.label} (recommended)" if choice == DICTATE_PRESETS[0] else choice.label
            self.popup.menu().addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, ""))
        self.popup.menu().addItem_(NSMenuItem.separatorItem())
        self.popup.addItemWithTitle_(OTHER)
        self.popup.selectItemAtIndex_(self._choices.index(current))

    @objc.python_method
    def is_recording(self):
        return self._monitor is not None

    def chooseItem_(self, sender):
        del sender
        index = self.popup.indexOfSelectedItem()
        if 0 <= index < len(self._choices):
            choice = self._choices[index]
            self._apply(choice.key_code, choice.modifiers)
        else:
            self.start_recording()

    @objc.python_method
    def start_recording(self):
        if self._monitor is not None:
            return
        self.owner.pause_shortcut()
        self.popup.setEnabled_(False)
        self._say("Press the keys you want, or Esc to cancel.")

        def key_down(event):
            self.key_pressed(int(event.keyCode()), int(event.modifierFlags()))
            return None  # Swallowed: the keys are a choice, not typing.

        self._monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, key_down)

    @objc.python_method
    def key_pressed(self, key_code, event_flags):
        if key_code == _ESCAPE:
            self.stop_recording()
            return
        modifiers = carbon_modifiers(event_flags)
        problem = shortcut_problem(key_code, modifiers)
        if problem is not None:
            self._say(f"{problem} Try another, or press Esc.")
            return
        self._end_monitor()
        self._apply(key_code, modifiers)

    @objc.python_method
    def stop_recording(self):
        """Leave recording without a choice (Esc, or the window closing)."""
        if self._monitor is None:
            return
        self._end_monitor()
        self.owner.resume_shortcut()
        self._say(None)
        self.refresh()

    @objc.python_method
    def _end_monitor(self):
        NSEvent.removeMonitor_(self._monitor)
        self._monitor = None
        self.popup.setEnabled_(True)

    @objc.python_method
    def _apply(self, key_code, modifiers):
        problem = self.owner.choose_shortcut(key_code, modifiers)
        self._say(problem)
        self.refresh()

    @objc.python_method
    def _say(self, text):
        self.note.setStringValue_(text or "")
        self.note.setHidden_(not text)
