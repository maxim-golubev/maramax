"""Native settings and explicit word replacements."""

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

from .corrections import MAX_RULES, normalize_rules

MARGIN = 24
CONTENT_WIDTH = 512
# Help text lines up with a checkbox's title rather than its box.
CHECKBOX_INDENT = 20

_HELP = {
    "compact_dictation": "A small bar that leaves the app you are typing in focused. "
                         "Turn off to dictate in the full Maramax window.",
    "auto_start_recording": "Turn off to open the window first and start with Cmd+R.",
    "live_preview": "Draft text while you speak. The final transcript always replaces it.",
    "auto_copy_to_clipboard": "",
    "paste_to_active_app": "Types the result where your cursor is. macOS asks for Accessibility permission once.",
    "high_accuracy": "Qwen3-ASR 1.7B. Better with names and unusual words, and it reads your word replacements "
                     "as vocabulary. Several times slower on long dictations, about 6 GB of memory, "
                     "and a 4.1 GB download on first use.",
    "prefer_builtin_mic": "Records with the Mac so AirPods stay in high-quality playback. "
                          "Skipped while the lid is closed, when the Mac's microphone is switched off.",
    "use_corrections": "Fixes words the recognizer keeps getting wrong, such as names. "
                       "Whole words and phrases, ignoring capitalization. "
                       "The original text stays in History and Recordings.",
}
_SECTIONS = (
    ("Dictation", ("compact_dictation", "auto_start_recording", "live_preview")),
    ("Result", ("auto_copy_to_clipboard", "paste_to_active_app")),
    ("Speech model", ("high_accuracy",)),
)
_KEEP_READY_CHOICES = ((0, "Off"), (30, "30 seconds"), (120, "2 minutes"), (300, "5 minutes"))
_DEFAULT_NOTE = "Each replacement is applied once per match; replacements never chain."


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
            self.tabs.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), 16),
            self.tabs.centerXAnchor().constraintEqualToAnchor_(root.centerXAnchor()),
        ]
        for index, page in enumerate(self.pages):
            page.setTranslatesAutoresizingMaskIntoConstraints_(False)
            root.addSubview_(page)
            page.setHidden_(index != 0)
            constraints += [
                page.topAnchor().constraintEqualToAnchor_constant_(self.tabs.bottomAnchor(), 20),
                page.leadingAnchor().constraintEqualToAnchor_constant_(root.leadingAnchor(), MARGIN),
                page.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH),
            ]
        NSLayoutConstraint.activateConstraints_(constraints)
        root.layoutSubtreeIfNeeded()
        # One window size for every tab: the tallest page decides.
        tallest = max(page.fittingSize().height for page in self.pages)
        self.panel.setContentSize_((CONTENT_WIDTH + 2 * MARGIN, 16 + 24 + 20 + tallest + MARGIN))
        self.update_input_devices([], getattr(delegate.config, "input_device", None))
        self.refresh()
        return self

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
        groups = [[self._header(title)] + [self._option(name) for name in names] for title, names in _SECTIONS]
        self.model_status = self._help("")
        self.model_retry = self._button("Retry", "retryModel:")
        groups[-1].append(self._stack([self.model_status, self.model_retry], horizontal=True, spacing=12))
        return self._page(groups)

    @objc.python_method
    def _microphone_page(self):
        self.device_picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.device_picker.setTarget_(self)
        self.device_picker.setAction_("selectDevice:")
        self.device_picker.widthAnchor().constraintEqualToConstant_(400).setActive_(True)
        picker_row = self._stack([self.device_picker, self._button("Refresh", "refreshDevices:")],
                                 horizontal=True, spacing=12)

        self.keep_ready = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.keep_ready.setTarget_(self)
        self.keep_ready.setAction_("selectKeepReady:")
        self.keep_ready.widthAnchor().constraintEqualToConstant_(140).setActive_(True)
        keep_row = self._stack([NSTextField.labelWithString_("Keep the microphone connected for"), self.keep_ready],
                               horizontal=True)
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
        grid.setColumnSpacing_(12)
        grid.rowAtIndex_(1).setRowAlignment_(NSGridRowAlignmentFirstBaseline)

        self.picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.picker.setTarget_(self)
        self.picker.setAction_("selectRule:")
        self.picker.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH).setActive_(True)
        self.remove = self._button("Remove", "removeRule:")
        self.note = self._help(_DEFAULT_NOTE)
        return self._page([
            [self._option("use_corrections")],
            [self._header("Add or change a replacement"), grid],
            [self._header("Saved replacements"), self.picker,
             self._stack([self.remove, self._button("New", "newRule:")], horizontal=True),
             self.note],
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
        transcriber = self.delegate.transcriber
        message = ("Speech model ready." if transcriber.is_ready() else
                   "Speech model unavailable — check your connection, then retry." if transcriber.load_error else
                   "Preparing the speech model; the first launch downloads it…")
        qwen = getattr(self.delegate, "qwen", None)
        if qwen is not None and getattr(self.delegate.config, "high_accuracy", False):
            message += (" High-accuracy model ready." if qwen.is_ready() else
                        " High-accuracy model could not be loaded; using the standard model."
                        if qwen.load_error is not None and not qwen.is_loading() else
                        " Loading the high-accuracy model…")
        return message

    @objc.python_method
    def refresh(self):
        config = self.delegate.config
        for name, button in self.options.items():
            button.setState_(int(getattr(config, name)))
        self.model_status.setStringValue_(self._model_message())
        self.model_retry.setHidden_(self.delegate.transcriber.load_error is None)

        seconds = getattr(config, "keep_mic_ready_seconds", 0)
        choices = list(_KEEP_READY_CHOICES)
        if seconds not in dict(choices):
            choices.append((seconds, f"{seconds} seconds"))
        self._keep_ready_values = [value for value, _ in choices]
        self.keep_ready.removeAllItems()
        self.keep_ready.addItemsWithTitles_([title for _, title in choices])
        self.keep_ready.selectItemAtIndex_(self._keep_ready_values.index(seconds))

        selected_device = getattr(config, "input_device", None)
        if selected_device in self.device_names:
            self.device_picker.selectItemAtIndex_(self.device_names.index(selected_device))
        self._sync_device_picker_enabled()

        selected = self.picker.indexOfSelectedItem()
        self.rules = list(config.replacements)
        self.picker.removeAllItems()
        for rule in self.rules:
            # addItemWithTitle would merge rules whose titles collide.
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{rule['heard']} → {rule['replacement']}", None, "",
            )
            self.picker.menu().addItem_(item)
        if not self.rules:
            self.picker.addItemWithTitle_("No replacements yet")
        elif selected >= 0:
            self.picker.selectItemAtIndex_(min(selected, len(self.rules) - 1))
        self.picker.setEnabled_(bool(self.rules))
        self.remove.setEnabled_(bool(self.rules))

    @objc.python_method
    def _sync_device_picker_enabled(self):
        self.device_picker.setEnabled_(not (getattr(self.delegate, "recording_active", False)
                                           or getattr(self.delegate, "is_transcribing", False)))

    @objc.python_method
    def show(self):
        self.refresh()
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        if self.tabs.selectedSegment() == 1:
            self.refreshDevices_(None)

    def selectTab_(self, sender):
        index = self.tabs.selectedSegment()
        for page_index, page in enumerate(self.pages):
            page.setHidden_(page_index != index)
        if index == 1:
            self.refreshDevices_(None)

    def refreshDevices_(self, sender):
        self.delegate._refresh_input_devices()

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
        self.delegate.handle_device_selected(self.device_names[self.device_picker.indexOfSelectedItem()])

    def selectKeepReady_(self, sender):
        del sender
        index = self.keep_ready.indexOfSelectedItem()
        if 0 <= index < len(self._keep_ready_values):
            self.delegate.handle_keep_ready_selected(self._keep_ready_values[index])

    def toggleSetting_(self, sender):
        for name, button in self.options.items():
            if button is sender:
                self.delegate._on_setting_toggled(self.delegate._settings_items[name])
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
            self.note.setStringValue_("Enter both phrases (up to 200 characters heard and 2,000 for the replacement).")
            return
        rule = candidate[0]
        replaced = {rule["heard"].casefold()}
        if self._editing_heard is not None:
            replaced.add(self._editing_heard.casefold())
        rules = [r for r in self.rules if r["heard"].casefold() not in replaced]
        if len(rules) >= MAX_RULES:
            self.note.setStringValue_(f"Up to {MAX_RULES} replacements are supported. Remove one to add another.")
            return
        self.delegate.config.replacements = rules + [rule]
        self._editing_heard = rule["heard"]
        saved = self.delegate._save_settings()
        self.refresh()
        self.picker.selectItemAtIndex_(len(self.rules) - 1)
        self.note.setStringValue_("Replacement saved." if saved else
                                  "Replacement works for this session, but settings could not be saved.")

    def removeRule_(self, sender):
        del sender
        index = self.picker.indexOfSelectedItem()
        if 0 <= index < len(self.rules):
            self.delegate.config.replacements = [r for i, r in enumerate(self.rules) if i != index]
            saved = self.delegate._save_settings()
            self.refresh()
            self.newRule_(None)
            self.note.setStringValue_("Replacement removed." if saved else "Could not save this change to disk.")
