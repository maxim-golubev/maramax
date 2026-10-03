"""The Settings window."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSFontWeightSemibold, NSImage,
    NSLayoutConstraint, NSMakeRect, NSPanel, NSPopUpButton, NSTextField, NSToolbar, NSToolbarItem,
    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled, NSWindowToolbarStylePreference,
)
from Foundation import NSObject

from . import __version__
from .config import Delivery
from .hotkeys import STOP
from .layout import small_text, stack
from .replacements_editor import ReplacementsEditor
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
    "auto_copy_to_clipboard": "Always on while Maramax pastes: pasting goes through the clipboard.",
    "paste_to_active_app": "Pastes the result where your cursor is. Needs Accessibility permission: Maramax opens "
                           "System Settings → Privacy & Security → Accessibility; turn Maramax on there.",
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
    ("When a transcript is ready", ("auto_copy_to_clipboard", "paste_to_active_app")),
    (_SPEECH_MODEL, ("high_accuracy",)),
    (_UPDATES, ("check_for_updates",)),
)
# Toolbar tabs, as in every Mac app's Settings: a name and an SF Symbol.
TABS = (("General", "gearshape"), ("Microphone", "mic"), ("Words", "character.book.closed"))
MICROPHONE_TAB = 1
_KEEP_READY_CHOICES = (0, 30, 120, 300)
PAGE_TOP = 20
ROW_GAP = 8  # Between controls that share a row.


def duration_label(seconds: int) -> str:
    if seconds == 0:
        return "Off"
    count, unit = (seconds // 60, "minute") if seconds % 60 == 0 else (seconds, "second")
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


class PreferencesController(NSObject):
    def initWithDelegate_labels_(self, delegate, labels):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self.labels = labels
        self.options = {}
        self.device_names = [None]
        self.selected_tab = 0
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, 480), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setReleasedWhenClosed_(False)
        # A menu-bar app has no Dock icon to bring a hidden panel back with.
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)
        toolbar = NSToolbar.alloc().initWithIdentifier_("MaramaxSettings")
        toolbar.setDelegate_(self)
        toolbar.setAllowsUserCustomization_(False)
        self.panel.setToolbar_(toolbar)
        self.panel.setToolbarStyle_(NSWindowToolbarStylePreference)
        root = self.panel.contentView()

        self.pages = [self._general_page(), self._microphone_page(), self._words_page()]
        constraints = []
        for page in self.pages:
            page.setTranslatesAutoresizingMaskIntoConstraints_(False)
            root.addSubview_(page)
            constraints += [
                page.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), PAGE_TOP),
                page.leadingAnchor().constraintEqualToAnchor_constant_(root.leadingAnchor(), MARGIN),
                page.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH),
            ]
        NSLayoutConstraint.activateConstraints_(constraints)
        self.update_input_devices([], delegate.config.input_device)
        self.show_tab(0)
        self.refresh()
        return self

    @objc.python_method
    def _fit_window_to_page(self):
        """Each tab gets a window exactly as tall as its content plus the
        same bottom margin, keeping the title bar where it is."""
        root = self.panel.contentView()
        root.layoutSubtreeIfNeeded()
        height = PAGE_TOP + self.pages[self.selected_tab].fittingSize().height + MARGIN
        current = self.panel.frame()
        # The title bar and toolbar: whatever the frame holds beyond the content.
        chrome = current.size.height - root.frame().size.height
        top = current.origin.y + current.size.height
        if abs(current.size.height - (height + chrome)) < 0.5:
            return  # Refreshed with nothing that changes the height.
        frame = NSMakeRect(current.origin.x, top - height - chrome, current.size.width, height + chrome)
        self.panel.setFrame_display_animate_(frame, True, bool(self.panel.isVisible()))

    # -- Toolbar --

    def toolbarAllowedItemIdentifiers_(self, toolbar):
        return [name for name, _symbol in TABS]

    def toolbarDefaultItemIdentifiers_(self, toolbar):
        return [name for name, _symbol in TABS]

    def toolbarSelectableItemIdentifiers_(self, toolbar):
        return [name for name, _symbol in TABS]

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, identifier, flag):
        symbol = dict(TABS)[str(identifier)]
        item = NSToolbarItem.alloc().initWithItemIdentifier_(identifier)
        item.setLabel_(identifier)
        item.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, identifier))
        item.setTarget_(self)
        item.setAction_("selectTab:")
        return item

    # -- Building blocks --

    @objc.python_method
    def _header(self, text):
        label = NSTextField.labelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        return label

    @objc.python_method
    def _help(self, text, width=CONTENT_WIDTH):
        label = small_text(text, width)
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
        indented = stack([self._help(text, CONTENT_WIDTH - CHECKBOX_INDENT)])
        indented.setEdgeInsets_((0, CHECKBOX_INDENT, 0, 0))
        return stack([button, indented], spacing=3)

    @objc.python_method
    def _page(self, groups):
        """Groups are lists of views; space between groups is wider than
        the space inside one."""
        views = [view for group in groups for view in group]
        page = stack(views, spacing=10)
        for group in groups[:-1]:
            page.setCustomSpacing_afterView_(22, group[-1])
        return page

    @objc.python_method
    def _general_page(self):
        sections = {title: [self._header(title)] + [self._option(name) for name in names] for title, names in _SECTIONS}
        shortcut_label = NSTextField.labelWithString_("Shortcut")
        # The picker's notes wrap within what the label leaves of the row.
        self.shortcut_picker = ShortcutPicker.alloc().initWithOwner_width_onResize_(
            self.delegate, CONTENT_WIDTH - shortcut_label.fittingSize().width - ROW_GAP, self._fit_window_to_page)
        shortcut_row = stack([shortcut_label, self.shortcut_picker.view], horizontal=True, spacing=ROW_GAP)
        sections[_DICTATION].insert(1, shortcut_row)
        self.model_status = self._help("")
        self.model_retry = self._button("Retry", "retryModel:")
        sections[_SPEECH_MODEL].append(stack([self.model_status, self.model_retry], horizontal=True,
                                                    spacing=ROW_GAP))
        self.update_check = self._button("Check Now", "checkForUpdates:")
        self.update_status = self._help("", CONTENT_WIDTH - 120)
        sections[_UPDATES].append(stack([self.update_check, self.update_status], horizontal=True,
                                               spacing=ROW_GAP))
        name = NSTextField.labelWithString_("Maramax")
        name.setFont_(NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold))
        version = NSTextField.labelWithString_(f"Version {__version__}")
        version.setTextColor_(NSColor.secondaryLabelColor())
        about = [stack([name, version], horizontal=True, spacing=ROW_GAP)]
        return self._page([about, *sections.values()])

    @objc.python_method
    def _microphone_page(self):
        self.device_picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.device_picker.setTarget_(self)
        self.device_picker.setAction_("selectDevice:")
        self.device_picker.widthAnchor().constraintEqualToConstant_(400).setActive_(True)
        picker_row = stack([self.device_picker, self._button("Refresh", "refreshDevices:")],
                                 horizontal=True, spacing=ROW_GAP)

        self.keep_ready = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.keep_ready.setTarget_(self)
        self.keep_ready.setAction_("selectKeepReady:")
        self.keep_ready.widthAnchor().constraintEqualToConstant_(140).setActive_(True)
        keep_row = stack([NSTextField.labelWithString_("Keep the microphone connected for"), self.keep_ready],
                               horizontal=True, spacing=ROW_GAP)
        return self._page([
            [self._header("Input"), picker_row,
             self._help("Automatic uses the Mac’s own microphone while the option below is on, otherwise the "
                        "input chosen in macOS. If a microphone disconnects while you "
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
        self.replacements = ReplacementsEditor.alloc().initWithOwner_width_onResize_(
            self.delegate, CONTENT_WIDTH, self._fit_window_to_page)
        return self._page([
            [self._option("use_corrections")],
            [self._header("Replacements"), self.replacements.view],
            [self._header("Try it"), self.replacements.trial_view],
        ])

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
        # Pasting copies whatever the copy setting says, so while it is on the
        # copy box shows what happens and cannot be turned off.
        copy = self.options["auto_copy_to_clipboard"]
        copy.setState_(int(config.delivery() is not Delivery.KEPT))
        copy.setEnabled_(config.delivery() is not Delivery.PASTED)
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
        self.show_busy_state()
        self.replacements.refresh()
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
    def show_busy_state(self):
        """A microphone cannot be chosen during a dictation or transcription."""
        self.device_picker.setEnabled_(not self.delegate.is_busy)

    @objc.python_method
    def show(self):
        self.refresh()
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        if self.selected_tab == MICROPHONE_TAB:
            self.refreshDevices_(None)

    def windowWillClose_(self, notification):
        del notification
        self.shortcut_picker.stop_recording()  # Never leave the global shortcut paused.

    def selectTab_(self, sender):
        names = [name for name, _symbol in TABS]
        self.show_tab(names.index(str(sender.itemIdentifier())))

    @objc.python_method
    def show_tab(self, index):
        self.selected_tab = index
        name = TABS[index][0]
        self.panel.toolbar().setSelectedItemIdentifier_(name)
        self.panel.setTitle_(name)
        if index != 0:
            self.shortcut_picker.stop_recording()  # Keys typed on another tab are typing, not a shortcut.
        for page_index, page in enumerate(self.pages):
            page.setHidden_(page_index != index)
        self._fit_window_to_page()
        if index == MICROPHONE_TAB:
            self.refreshDevices_(None)

    def refreshDevices_(self, sender):
        self.delegate.refresh_input_devices()

    @objc.python_method
    def shows_microphones(self):
        """Whether the device list is on screen and worth refreshing."""
        return self.panel.isVisible() and self.selected_tab == MICROPHONE_TAB

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
        self.show_busy_state()

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
