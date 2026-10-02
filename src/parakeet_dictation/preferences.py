"""The Settings window."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSFontWeightSemibold,
    NSGridCell, NSGridRowAlignmentFirstBaseline, NSGridView, NSLayoutAttributeFirstBaseline,
    NSLayoutAttributeLeading, NSLayoutConstraint, NSMakeRect, NSMenuItem, NSPanel, NSPopUpButton,
    NSSegmentedControl, NSStackView, NSTextField, NSTextFieldRoundedBezel,
    NSUserInterfaceLayoutOrientationHorizontal, NSUserInterfaceLayoutOrientationVertical,
    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import __version__
from .corrections import MAX_HEARD_CHARS, MAX_REPLACEMENT_CHARS, MAX_RULES, normalize_rules
from .hotkeys import STOP
from .shortcut_picker import ShortcutPicker

MARGIN = 24
CONTENT_WIDTH = 512
# Help text lines up with a checkbox's title rather than its box.
CHECKBOX_INDENT = 20

_HELP = {
    "compact_dictation": "A small bar that leaves the app you are typing in focused. "
                         "Turn off to dictate in the full Maramax window.",
    "auto_start_recording": f"Turn off to open the window first and start with {STOP.label}.",
    "live_preview": "Draft text while you speak. The final transcript always replaces it.",
    "auto_copy_to_clipboard": "",
    "paste_to_active_app": "Pastes the result where your cursor is. macOS asks for Accessibility permission once.",
    "high_accuracy": "Qwen3-ASR 1.7B, a larger model that reads your word replacements as vocabulary. "
                     "Several times slower on long dictations, about 6 GB of memory, "
                     "and a 4.1 GB download on first use.",
    "prefer_builtin_mic": "Records with the Mac so AirPods stay in high-quality playback. "
                          "Skipped while the lid is closed, when the Mac’s microphone is switched off.",
    "check_for_updates": "Once a day Maramax asks GitHub whether a newer version has been published, "
                         "and offers it. Nothing else is sent, and installing always asks first.",
    "use_corrections": "Fixes words the recognizer keeps getting wrong, such as names. "
                       "Whole words and phrases, ignoring capitalization. "
                       "The original text stays in History and Recordings.",
}
_DICTATION = "Dictation"
_SPEECH_MODEL = "Speech model"
_UPDATES = "Updates"
_SECTIONS = (
    (_DICTATION, ("compact_dictation", "auto_start_recording", "live_preview")),
    ("Result", ("auto_copy_to_clipboard", "paste_to_active_app")),
    (_SPEECH_MODEL, ("high_accuracy",)),
    (_UPDATES, ("check_for_updates",)),
)
_KEEP_READY_CHOICES = (0, 30, 120, 300)
_DEFAULT_NOTE = "Each replacement is applied once per match; replacements never chain."
_RULE_TITLE_CHARS = 60
TABS_TOP = 16
TABS_TO_PAGE = 20
ROW_GAP = 8  # Between controls that share a row.


def duration_label(seconds: int) -> str:
    if seconds == 0:
        return "Off"
    count, unit = (seconds // 60, "minute") if seconds % 60 == 0 else (seconds, "second")
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def rule_title(rule: dict[str, str]) -> str:
    """One menu line per rule; a long snippet must not make a 4,000 pt menu."""
    title = " ".join(f"{rule['heard']} → {rule['replacement']}".split())
    return title if len(title) <= _RULE_TITLE_CHARS else title[: _RULE_TITLE_CHARS - 1] + "…"


class PreferencesController(NSObject):
    def initWithDelegate_labels_(self, delegate, labels):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self.labels = labels
        self._editing_heard = None
        self.options = {}
        self.rules = []
        self.device_names = [None]
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, 480), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("Maramax Settings")
        self.panel.setReleasedWhenClosed_(False)
        # A menu-bar app has no Dock icon to bring a hidden panel back with.
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)
        self.shortcut_picker = ShortcutPicker.alloc().initWithOwner_width_(delegate, CONTENT_WIDTH)
        root = self.panel.contentView()

        self.tabs = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(0, 0, 330, 24))
        self.tabs.setSegmentCount_(3)
        for index, title in enumerate(("General", "Microphone", "Words")):
            self.tabs.setLabel_forSegment_(title, index)
            self.tabs.setWidth_forSegment_(110, index)
        self.tabs.setSelectedSegment_(0)
        self.tabs.setTarget_(self)
        self.tabs.setAction_("selectTab:")
        self.tabs.setTranslatesAutoresizingMaskIntoConstraints_(False)
        root.addSubview_(self.tabs)

        self.pages = [self._general_page(), self._microphone_page(), self._words_page()]
        constraints = [
            self.tabs.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), TABS_TOP),
            self.tabs.centerXAnchor().constraintEqualToAnchor_(root.centerXAnchor()),
        ]
        for index, page in enumerate(self.pages):
            page.setTranslatesAutoresizingMaskIntoConstraints_(False)
            root.addSubview_(page)
            page.setHidden_(index != 0)
            constraints += [
                page.topAnchor().constraintEqualToAnchor_constant_(self.tabs.bottomAnchor(), TABS_TO_PAGE),
                page.leadingAnchor().constraintEqualToAnchor_constant_(root.leadingAnchor(), MARGIN),
                page.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH),
            ]
        NSLayoutConstraint.activateConstraints_(constraints)
        self.update_input_devices([], delegate.config.input_device)
        self.refresh()
        return self

    @objc.python_method
    def _fit_window_to_page(self):
        """Each tab gets a window exactly as tall as its content plus the
        same bottom margin, keeping the title bar where it is."""
        root = self.panel.contentView()
        root.layoutSubtreeIfNeeded()
        page = self.pages[self.tabs.selectedSegment()]
        # Pages hang from the top, so the space above one is whatever the
        # layout made it; measuring it avoids restating the tab control's size.
        above = root.bounds().size.height - (page.frame().origin.y + page.frame().size.height)
        height = above + page.fittingSize().height + MARGIN
        frame = self.panel.frameRectForContentRect_(NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, height))
        current = self.panel.frame()
        top = current.origin.y + current.size.height
        self.panel.setFrame_display_(
            NSMakeRect(current.origin.x, top - frame.size.height, frame.size.width, frame.size.height), True)

    # -- Building blocks --

    @objc.python_method
    def _stack(self, views, horizontal=False, spacing=8):
        stack = NSStackView.stackViewWithViews_(views)
        stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal if horizontal
                              else NSUserInterfaceLayoutOrientationVertical)
        # Leading/baseline alignment works on alignment rectangles, so push
        # buttons, popups, and text line up on what the eye sees as edges.
        stack.setAlignment_(NSLayoutAttributeFirstBaseline if horizontal else NSLayoutAttributeLeading)
        stack.setSpacing_(spacing)
        return stack

    @objc.python_method
    def _header(self, text):
        label = NSTextField.labelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        return label

    @objc.python_method
    def _help(self, text, width=CONTENT_WIDTH):
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_(11))
        label.setTextColor_(NSColor.secondaryLabelColor())
        label.setSelectable_(False)
        label.setPreferredMaxLayoutWidth_(width)
        label.widthAnchor().constraintLessThanOrEqualToConstant_(width).setActive_(True)
        return label

    @objc.python_method
    def _button(self, title, action):
        return NSButton.buttonWithTitle_target_action_(title, self, action)

    @objc.python_method
    def _option(self, name):
        """A checkbox with its explanation underneath, as one unit."""
        button = NSButton.checkboxWithTitle_target_action_(self.labels.get(name, name), self, "toggleSetting:")
        self.options[name] = button
        text = _HELP.get(name, "")
        if not text:
            return button
        indented = self._stack([self._help(text, CONTENT_WIDTH - CHECKBOX_INDENT)])
        indented.setEdgeInsets_((0, CHECKBOX_INDENT, 0, 0))
        return self._stack([button, indented], spacing=3)

    @objc.python_method
    def _page(self, groups):
        """Groups are lists of views; space between groups is wider than
        the space inside one."""
        views = [view for group in groups for view in group]
        page = self._stack(views, spacing=10)
        for group in groups[:-1]:
            page.setCustomSpacing_afterView_(22, group[-1])
        return page

    @objc.python_method
    def _general_page(self):
        sections = {title: [self._header(title)] + [self._option(name) for name in names] for title, names in _SECTIONS}
        shortcut_row = self._stack([NSTextField.labelWithString_("Shortcut"), self.shortcut_picker.view],
                                   horizontal=True, spacing=ROW_GAP)
        sections[_DICTATION].insert(1, shortcut_row)
        self.model_status = self._help("")
        self.model_retry = self._button("Retry", "retryModel:")
        sections[_SPEECH_MODEL].append(self._stack([self.model_status, self.model_retry], horizontal=True,
                                                    spacing=ROW_GAP))
        self.update_check = self._button("Check Now", "checkForUpdates:")
        self.update_status = self._help("", CONTENT_WIDTH - 120)
        sections[_UPDATES].append(self._stack([self.update_check, self.update_status], horizontal=True,
                                               spacing=ROW_GAP))
        name = NSTextField.labelWithString_("Maramax")
        name.setFont_(NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold))
        version = NSTextField.labelWithString_(f"Version {__version__}")
        version.setTextColor_(NSColor.secondaryLabelColor())
        about = [self._stack([name, version], horizontal=True, spacing=ROW_GAP)]
        return self._page([about, *sections.values()])

    @objc.python_method
    def _microphone_page(self):
        self.device_picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.device_picker.setTarget_(self)
        self.device_picker.setAction_("selectDevice:")
        self.device_picker.widthAnchor().constraintEqualToConstant_(400).setActive_(True)
        picker_row = self._stack([self.device_picker, self._button("Refresh", "refreshDevices:")],
                                 horizontal=True, spacing=ROW_GAP)

        self.keep_ready = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.keep_ready.setTarget_(self)
        self.keep_ready.setAction_("selectKeepReady:")
        self.keep_ready.widthAnchor().constraintEqualToConstant_(140).setActive_(True)
        keep_row = self._stack([NSTextField.labelWithString_("Keep the microphone connected for"), self.keep_ready],
                               horizontal=True, spacing=ROW_GAP)
        return self._page([
            [self._header("Input"), picker_row,
             self._help("Automatic follows the input chosen in macOS. If a microphone disconnects while you "
                        "dictate, Automatic carries on with the next available one; a microphone you picked "
                        "here is never swapped silently."),
             self._option("prefer_builtin_mic")],
            [self._header("After a dictation"), keep_row,
             self._help("Bluetooth microphones such as AirPods need 2–3 seconds to connect each time. Keeping "
                        "the connection open makes the next dictation start instantly. While it is open, macOS "
                        "shows the microphone indicator and AirPods stay in call-quality playback. "
                        "Nothing heard while waiting is recorded.")],
            [self._header("Reliability"),
             self._help("A stalled connection is reset automatically, and audio that was received is "
                        "always saved to Recordings for a retry.")],
        ])

    @objc.python_method
    def _words_page(self):
        self.heard = self._field("e.g. mara max", 200)
        self.replacement = self._field("e.g. Maramax", 200)
        grid = NSGridView.gridViewWithViews_([
            [NSTextField.labelWithString_("When the transcript says"), NSTextField.labelWithString_("Replace with"),
             NSGridCell.emptyContentView()],
            [self.heard, self.replacement, self._button("Save", "saveRule:")],
        ])
        grid.setRowSpacing_(4)
        grid.setColumnSpacing_(ROW_GAP)
        grid.rowAtIndex_(1).setRowAlignment_(NSGridRowAlignmentFirstBaseline)

        self.picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.picker.setTarget_(self)
        self.picker.setAction_("selectRule:")
        self.picker.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH).setActive_(True)
        self.remove = self._button("Remove", "removeRule:")
        new_rule = self._button("New", "newRule:")
        rule_buttons = self._stack([self.remove, new_rule], horizontal=True, spacing=ROW_GAP)
        # Siblings of equal weight get equal width (they share a parent now).
        new_rule.widthAnchor().constraintEqualToAnchor_(self.remove.widthAnchor()).setActive_(True)
        self.note = self._help(_DEFAULT_NOTE)
        return self._page([
            [self._option("use_corrections")],
            [self._header("Add or change a replacement"), grid],
            [self._header("Saved replacements"), self.picker, rule_buttons, self.note],
        ])

    @objc.python_method
    def _field(self, placeholder, width):
        field = NSTextField.textFieldWithString_("")
        field.setPlaceholderString_(placeholder)
        field.setBezelStyle_(NSTextFieldRoundedBezel)
        field.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        return field

    # -- State --

    @objc.python_method
    def _model_message(self):
        message = self.delegate.transcriber.status_message() + "."
        if self.delegate.config.high_accuracy:
            message += " " + self.delegate.qwen.status_message().rstrip("…") + "."
        return message

    @objc.python_method
    def refresh(self):
        config = self.delegate.config
        for name, button in self.options.items():
            button.setState_(int(getattr(config, name)))
        self.model_status.setStringValue_(self._model_message())
        self.model_retry.setHidden_(self.delegate.transcriber.load_error is None)
        if not self.shortcut_picker.is_recording():
            self.shortcut_picker.refresh()
        self.show_update_status()

        # A hand-edited duration stays visible instead of snapping to a preset.
        self._keep_ready_values = sorted({*_KEEP_READY_CHOICES, config.keep_mic_ready_seconds})
        self.keep_ready.removeAllItems()
        self.keep_ready.addItemsWithTitles_([duration_label(value) for value in self._keep_ready_values])
        self.keep_ready.selectItemAtIndex_(self._keep_ready_values.index(config.keep_mic_ready_seconds))

        if config.input_device in self.device_names:
            self.device_picker.selectItemAtIndex_(self.device_names.index(config.input_device))
        self._sync_device_picker_enabled()

        selected = self.picker.indexOfSelectedItem()
        self.rules = list(config.replacements)
        self.picker.removeAllItems()
        for rule in self.rules:
            # addItemWithTitle would merge rules whose titles collide.
            self.picker.menu().addItem_(
                NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(rule_title(rule), None, ""))
        if not self.rules:
            self.picker.addItemWithTitle_("No replacements yet")
        elif selected >= 0:
            self.picker.selectItemAtIndex_(min(selected, len(self.rules) - 1))
        self.picker.setEnabled_(bool(self.rules))
        self.remove.setEnabled_(bool(self.rules))
        # The model status line can grow to two lines.
        self._fit_window_to_page()

    @objc.python_method
    def show_update_status(self):
        updates = self.delegate.updates
        self.update_status.setStringValue_(updates.status_text())
        self.update_check.setEnabled_(updates.can_check())

    def checkForUpdates_(self, sender):
        del sender
        self.delegate.updates.check_requested()
        self.show_update_status()

    @objc.python_method
    def _sync_device_picker_enabled(self):
        self.device_picker.setEnabled_(not self.delegate.is_busy)

    @objc.python_method
    def show(self):
        self.refresh()
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        if self.tabs.selectedSegment() == 1:
            self.refreshDevices_(None)

    def windowWillClose_(self, notification):
        del notification
        self.shortcut_picker.stop_recording()  # Never leave the global shortcut paused.

    def selectTab_(self, sender):
        index = self.tabs.selectedSegment()
        for page_index, page in enumerate(self.pages):
            page.setHidden_(page_index != index)
        self._fit_window_to_page()
        if index == 1:
            self.refreshDevices_(None)

    def refreshDevices_(self, sender):
        self.delegate.refresh_input_devices()

    @objc.python_method
    def shows_microphones(self):
        """Whether the device list is on screen and worth refreshing."""
        return self.panel.isVisible() and self.tabs.selectedSegment() == 1

    @objc.python_method
    def update_input_devices(self, devices, selected_name, automatic_name=None):
        names = list(dict.fromkeys(device.name for device in devices))
        if selected_name and selected_name not in names:
            names.insert(0, selected_name)
        self.device_names = [None] + names
        # Automatic names the microphone it would use right now, so there is
        # no guessing which one "automatic" means.
        automatic = f"Automatic — {automatic_name}" if automatic_name else "Automatic"
        self.device_picker.removeAllItems()
        self.device_picker.addItemsWithTitles_([automatic] + names)
        self.device_picker.selectItemAtIndex_(self.device_names.index(selected_name))
        self._sync_device_picker_enabled()

    def selectDevice_(self, sender):
        self.delegate.select_input_device(self.device_names[self.device_picker.indexOfSelectedItem()])

    def selectKeepReady_(self, sender):
        del sender
        index = self.keep_ready.indexOfSelectedItem()
        if 0 <= index < len(self._keep_ready_values):
            self.delegate.set_keep_microphone_ready(self._keep_ready_values[index])

    def toggleSetting_(self, sender):
        for name, button in self.options.items():
            if button is sender:
                self.delegate.toggle_setting(name)
                return

    def retryModel_(self, sender):
        del sender
        self.delegate.retry_speech_model()
        self.refresh()

    def selectRule_(self, sender):
        del sender
        index = self.picker.indexOfSelectedItem()
        if 0 <= index < len(self.rules):
            self._editing_heard = self.rules[index]["heard"]
            self.heard.setStringValue_(self.rules[index]["heard"])
            self.replacement.setStringValue_(self.rules[index]["replacement"])

    def newRule_(self, sender):
        del sender
        self._editing_heard = None
        self.heard.setStringValue_("")
        self.replacement.setStringValue_("")
        self.panel.makeFirstResponder_(self.heard)

    def saveRule_(self, sender):
        del sender
        candidate = normalize_rules([{
            "heard": str(self.heard.stringValue()), "replacement": str(self.replacement.stringValue()),
        }])
        if not candidate:
            self.note.setStringValue_(f"Enter both phrases (up to {MAX_HEARD_CHARS} characters heard and "
                                      f"{MAX_REPLACEMENT_CHARS:,} for the replacement).")
            return
        rule = candidate[0]
        replaced = {rule["heard"].casefold()}
        if self._editing_heard is not None:
            replaced.add(self._editing_heard.casefold())
        rules = [r for r in self.rules if r["heard"].casefold() not in replaced]
        if len(rules) >= MAX_RULES:
            self.note.setStringValue_(f"Up to {MAX_RULES} replacements are supported. Remove one to add another.")
            return
        self._editing_heard = rule["heard"]
        saved = self.delegate.replace_word_rules(rules + [rule])
        self.refresh()
        self.picker.selectItemAtIndex_(len(self.rules) - 1)
        self.note.setStringValue_("Replacement saved." if saved else
                                  "Replacement works for this session, but settings could not be saved.")

    def removeRule_(self, sender):
        del sender
        index = self.picker.indexOfSelectedItem()
        if 0 <= index < len(self.rules):
            saved = self.delegate.replace_word_rules([r for i, r in enumerate(self.rules) if i != index])
            self.refresh()
            self.newRule_(None)
            self.note.setStringValue_("Replacement removed." if saved else "Could not save this change to disk.")
