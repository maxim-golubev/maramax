"""The control for choosing the dictation shortcut: a recommended preset, or keys the user presses."""

from __future__ import annotations

import objc
from AppKit import (
    NSAccessibilityAnnouncementKey, NSAccessibilityAnnouncementRequestedNotification, NSAccessibilityPriorityHigh,
    NSAccessibilityPriorityKey, NSAccessibilityPostNotificationWithUserInfo, NSEvent, NSEventMaskKeyDown,
    NSMakeRect, NSMenuItem, NSPopUpButton, NSWindowDidResignKeyNotification,
)
from Foundation import NSNotificationCenter, NSObject

from .hotkeys import DICTATE_PRESETS, carbon_modifiers
from .layout import small_text, stack

OTHER = "Other shortcut…"
# Carbon cannot tell when another app already uses a shortcut, so the user is told how to notice.
HINT = "If pressing it opens something else, another app uses it: choose another."
_ESCAPE = 0x35


class ShortcutPicker(NSObject):
    """`owner` provides current_shortcut(), problem_with_shortcut(key_code,
    modifiers) -> problem or None, choose_shortcut(key_code, modifiers) ->
    problem or None, pause_shortcut(), and resume_shortcut(). While keys are
    being recorded the global shortcut is paused, so pressing the current one
    is seen here rather than starting a dictation. Recording ends when its
    window stops being the key window, so at most one picker records and the
    shortcut is never left paused. `on_resize` is called when the note under
    the popup appears, changes, or goes, so the window can fit it."""

    def initWithOwner_width_onResize_(self, owner, width, on_resize):
        self = objc.super(ShortcutPicker, self).init()
        if self is None:
            return None
        self.owner = owner
        self._on_resize = on_resize
        self._monitor = None
        self._resign_observer = None
        self._choices = []
        self.popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.popup.setTarget_(self)
        self.popup.setAction_("chooseItem:")
        self.popup.setAccessibilityLabel_("Dictation shortcut")
        self.popup.widthAnchor().constraintEqualToConstant_(260).setActive_(True)
        self.note = small_text("", width)
        self.note.setHidden_(True)
        self.view = stack([self.popup, self.note, small_text(HINT, width)], spacing=4)
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
        window = self.view.window()
        self.owner.pause_shortcut()
        self.popup.setEnabled_(False)
        self._say("Press the keys you want, or Esc to cancel.")

        def key_down(event):
            if event.window() != window:
                return event  # Typing in another window stays typing.
            self.key_pressed(int(event.keyCode()), int(event.modifierFlags()))
            return None  # Swallowed: the keys are a choice, not typing.

        self._monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, key_down)
        self._resign_observer = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSWindowDidResignKeyNotification, window, None, lambda _notification: self.stop_recording())

    @objc.python_method
    def key_pressed(self, key_code, event_flags):
        if key_code == _ESCAPE:
            self.stop_recording()
            return
        modifiers = carbon_modifiers(event_flags)
        problem = self.owner.problem_with_shortcut(key_code, modifiers)
        if problem is not None:
            self._say(f"{problem} Try another, or press Esc.")
            return
        self._end_monitor()
        self._apply(key_code, modifiers)

    @objc.python_method
    def stop_recording(self):
        """Leave recording without a choice (Esc, another tab or window, or the window closing)."""
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
        NSNotificationCenter.defaultCenter().removeObserver_(self._resign_observer)
        self._resign_observer = None
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
        if text:
            # The note is the only sign that keys are being captured or were refused.
            NSAccessibilityPostNotificationWithUserInfo(
                self.note, NSAccessibilityAnnouncementRequestedNotification,
                {NSAccessibilityAnnouncementKey: text, NSAccessibilityPriorityKey: NSAccessibilityPriorityHigh})
        self._on_resize()
