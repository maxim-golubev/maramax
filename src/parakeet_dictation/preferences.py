"""Native settings and explicit word replacements."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSButtonTypeSwitch, NSColor,
    NSFont, NSMakeRect, NSMenuItem, NSPanel, NSPopUpButton, NSTextField,
    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled, NSView, NSSegmentedControl,
)
from Foundation import NSObject

from .corrections import MAX_RULES, normalize_rules


class PreferencesController(NSObject):
    def initWithDelegate_labels_(self, delegate, labels):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self._editing_heard = None
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 640, 480), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("Maramax Settings")
        self.panel.setReleasedWhenClosed_(False)
        root = self.panel.contentView()
        self.tabs = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(140, 427, 360, 32))
        self.tabs.setSegmentCount_(3)
        for index, title in enumerate(("General", "Microphone", "Words")):
            self.tabs.setLabel_forSegment_(title, index)
            self.tabs.setWidth_forSegment_(120, index)
        self.tabs.setSelectedSegment_(0)
        self.tabs.setTarget_(self)
        self.tabs.setAction_("selectTab:")
        root.addSubview_(self.tabs)
        self.pages = []
        self.options = {}
        for index in range(3):
            page = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 410))
            root.addSubview_(page)
            page.setHidden_(index != 0)
            self.pages.append(page)
        self.content = self.pages[0]
        self._label("Everyday dictation", 24, 366, 590, 26, 20, True)
        general = [(name, title) for name, title in labels.items()
                   if name not in ("prefer_builtin_mic", "use_corrections")]
        for index, (name, title) in enumerate(general):
            button = self._button(title, 24, 320 - index * 34, 590, "toggleSetting:")
            button.setButtonType_(NSButtonTypeSwitch)
            self.options[name] = button
        self._label("Paste requires Accessibility access. High accuracy uses an optional 4.1 GB download.",
                    24, 98, 592, 38, 11).setTextColor_(NSColor.secondaryLabelColor())
        self.model_status = self._label("", 24, 53, 445, 28, 12)
        self.model_retry = self._button("Retry model", 490, 51, 126, "retryModel:")
        self.content = self.pages[1]
        self._label("Microphone", 24, 366, 590, 26, 20, True)
        self._label("Input for your next recording", 24, 319, 590, 22, 13)
        self.device_picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(24, 275, 430, 32), False)
        self.device_picker.setTarget_(self)
        self.device_picker.setAction_("selectDevice:")
        self.content.addSubview_(self.device_picker)
        self._button("Refresh", 473, 275, 143, "refreshDevices:")
        button = self._button(labels.get("prefer_builtin_mic", "Prefer Mac microphone in Automatic mode"),
                              24, 220, 590, "toggleSetting:")
        button.setButtonType_(NSButtonTypeSwitch)
        self.options["prefer_builtin_mic"] = button
        self._label("Automatic can use the Mac microphone while you listen through AirPods.\n"
                    "Selecting AirPods uses their microphone and may change Bluetooth playback quality.\n\n"
                    "A stalled connection resets automatically. Received audio is saved for retry.",
                    24, 75, 590, 128, 12).setTextColor_(NSColor.secondaryLabelColor())
        self.update_input_devices([], getattr(delegate.config, "input_device", None))
        self.content = self.pages[2]
        self._label("Your words", 24, 366, 590, 26, 20, True)
        button = self._button(labels.get("use_corrections", "Apply my word replacements"),
                              24, 318, 590, "toggleSetting:")
        button.setButtonType_(NSButtonTypeSwitch)
        self.options["use_corrections"] = button
        self._label("When the transcript says", 24, 269, 250, 18, 11)
        self._label("Replace with", 285, 269, 240, 18, 11)
        self.heard = self._field(24, 236, 248, "e.g. mara max")
        self.replacement = self._field(285, 236, 220, "e.g. Maramax")
        self._button("Save", 517, 233, 99, "saveRule:")
        self.picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(24, 187, 592, 30), False)
        self.picker.setTarget_(self)
        self.picker.setAction_("selectRule:")
        self.content.addSubview_(self.picker)
        self.remove = self._button("Remove selected", 24, 143, 156, "removeRule:")
        self._button("New replacement", 188, 143, 165, "newRule:")
        self.note = self._label(
            "Whole words and phrases, ignoring capitalization. Each replacement is applied once.\n"
            "Original text stays in history and saved recordings.", 24, 71, 592, 56, 11)
        self.note.setTextColor_(NSColor.secondaryLabelColor())
        self.refresh()
        return self

    @objc.python_method
    def _label(self, text, x, y, width, height, size, bold=False):
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFrame_(NSMakeRect(x, y, width, height))
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        self.content.addSubview_(label)
        return label

    @objc.python_method
    def _button(self, title, x, y, width, action):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 32))
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        self.content.addSubview_(button)
        return button

    @objc.python_method
    def _field(self, x, y, width, placeholder):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, 27))
        field.setPlaceholderString_(placeholder)
        field.setFont_(NSFont.systemFontOfSize_(13))
        self.content.addSubview_(field)
        return field

    @objc.python_method
    def refresh(self):
        for name, button in self.options.items():
            button.setState_(int(getattr(self.delegate.config, name)))
        transcriber = self.delegate.transcriber
        message = ("Speech model ready" if transcriber.is_ready() else
                   "Model unavailable — check your connection and retry" if transcriber.load_error else
                   "Preparing speech model; first launch downloads weights…")
        self.model_status.setStringValue_(message)
        self.model_retry.setEnabled_(transcriber.load_error is not None)
        selected = self.picker.indexOfSelectedItem()
        self.rules = list(self.delegate.config.replacements)
        self.picker.removeAllItems()
        for rule in self.rules:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{rule['heard']} → {rule['replacement']}", None, "",
            )
            self.picker.menu().addItem_(item)
        if not self.rules:
            self.picker.addItemWithTitle_("No replacements yet")
        elif selected >= 0:
            self.picker.selectItemAtIndex_(min(selected, len(self.rules) - 1))
        self.remove.setEnabled_(bool(self.rules))

    @objc.python_method
    def show(self):
        self.refresh()
        self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    def selectTab_(self, sender):
        index = self.tabs.selectedSegment()
        for page_index, page in enumerate(self.pages):
            page.setHidden_(page_index != index)
        if index == 1:
            self.refreshDevices_(None)

    def refreshDevices_(self, sender):
        self.delegate._refresh_input_devices()

    @objc.python_method
    def update_input_devices(self, devices, selected_name):
        names = list(dict.fromkeys(device.name for device in devices))
        if selected_name and selected_name not in names:
            names.insert(0, selected_name)
        self.device_names = [None] + names
        self.device_picker.removeAllItems()
        self.device_picker.addItemsWithTitles_(["Automatic"] + names)
        self.device_picker.selectItemAtIndex_(self.device_names.index(selected_name))
        self.device_picker.setEnabled_(not (getattr(self.delegate, "recording_active", False)
                                           or getattr(self.delegate, "is_transcribing", False)))

    def selectDevice_(self, sender):
        self.delegate.handle_device_selected(self.device_names[self.device_picker.indexOfSelectedItem()])

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
        self.note.setStringValue_("Replacement saved. Original transcripts are retained." if saved else
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
