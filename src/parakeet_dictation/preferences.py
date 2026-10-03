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
from .layout import Notice, aligned_width, show_notice, small_text, spacer, stack
from .recordings import MAX_ARCHIVE_BYTES
from .replacements_editor import ReplacementsEditor
from .shortcut_picker import ShortcutPicker

MARGIN = 24
CONTENT_WIDTH = 512
# Help text lines up with a checkbox's or radio button's title rather than its box.
CHECKBOX_INDENT = 20

SETTING_LABELS = {
    "compact_dictation": "Use the compact dictation bar",
    "auto_start_recording": "Start dictating as soon as the shortcut is pressed",
    "live_preview": "Show a live preview in the full window",
    "high_accuracy": "Use the high-accuracy model",
    "prefer_builtin_mic": "Prefer the Mac’s own microphone in Automatic",
    "use_corrections": "Apply my word replacements",
    "check_for_updates": "Check for updates automatically",
}
_HELP = {
    "compact_dictation": "A small bar at the bottom of the screen, so the app you are typing in keeps focus. "
                         "Turn it off to dictate in the Maramax window.",
    "auto_start_recording": f"When off, the shortcut opens the Maramax window and {STOP.label} starts.",
    "live_preview": "Draft text while you speak. The final transcript replaces it.",
    "high_accuracy": "Qwen3-ASR 1.7B, which also reads your word replacements as vocabulary while they are on. "
                     "Several times slower, uses about 6 GB of memory, and downloads 4.1 GB the first time.",
    "prefer_builtin_mic": "Records with the Mac, so AirPods keep their high-quality playback. Skipped while "
                          "the lid is closed, which switches the Mac’s microphone off.",
    "check_for_updates": "Once a day, Maramax asks GitHub whether there is a newer version. Nothing else is "
                         "sent, and installing always asks first.",
    "use_corrections": "Fixes words the recognizer keeps getting wrong, such as names. Matches whole words and "
                       "phrases, ignoring capitals. The original text stays in History and Recordings.",
}
# Where a finished transcript goes: the choices, in the order shown.
DELIVERY_LABELS = {
    Delivery.PASTED: "Paste into the app you’re using",
    Delivery.COPIED: "Copy to the clipboard",
    Delivery.KEPT: "Keep in Maramax only",
}
_DELIVERY_HELP = {
    Delivery.PASTED: "Maramax presses Cmd+V for you. The transcript is copied too.",
    Delivery.COPIED: "Paste it yourself with Cmd+V.",
    Delivery.KEPT: "Nothing is copied or pasted. Transcripts wait in Open Transcript and History.",
}
# Toolbar tabs, as in every Mac app's Settings: a name and an SF Symbol.
TABS = (("General", "gearshape"), ("Microphone", "mic"), ("Words", "character.book.closed"),
        ("Advanced", "gearshape.2"))
MICROPHONE_TAB = 1
_KEEP_READY_CHOICES = (0, 30, 120, 300)
_HISTORY_CHOICES = (50, 100, 250, 500, 1000)
_RECORDINGS_CHOICES = (10, 20, 50, 100)
_HISTORY_NOTE = (f"Nothing leaves this Mac. Recordings also stay under {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB "
                 "in all. With a lower number, the oldest are deleted when the next one is saved.")
PAGE_TOP = 20
ROW_GAP = 8  # Between controls that share a row.
SECTION_GAP = 22  # Between groups on a page; inside one, PAGE_SPACING.
PAGE_SPACING = 10


def duration_label(seconds: int) -> str:
    if seconds == 0:
        return "Off"
    count, unit = (seconds // 60, "minute") if seconds % 60 == 0 else (seconds, "second")
    return f"For {count} {unit}" if count == 1 else f"For {count} {unit}s"


def count_label(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


class PreferencesController(NSObject):
    def initWithDelegate_(self, delegate):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.delegate = delegate
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

        self.pages = [self._general_page(), self._microphone_page(), self._words_page(), self._advanced_page()]
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
        self.update_input_devices(None, delegate.config.input_device)  # Listed once the tab is shown.
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
    def _indented(self, views):
        """Views under a checkbox or radio button, lined up with its title."""
        indented = stack(views, spacing=6)
        indented.setEdgeInsets_((0, CHECKBOX_INDENT, 0, 0))
        return indented

    @objc.python_method
    def _with_help(self, button, *views):
        """A checkbox or radio button with what explains it underneath, as one unit."""
        return stack([button, self._indented(list(views))], spacing=3)

    @objc.python_method
    def _option(self, name):
        button = NSButton.checkboxWithTitle_target_action_(SETTING_LABELS[name], self, "toggleSetting:")
        self.options[name] = button
        return self._with_help(button, self._help(_HELP[name], CONTENT_WIDTH - CHECKBOX_INDENT))

    @objc.python_method
    def _choice_menu(self, action, width):
        menu = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        menu.setTarget_(self)
        menu.setAction_(action)
        menu.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        return menu

    @staticmethod
    def _offer(menu, presets, current, label):
        """Fill `menu` with `presets` and select `current`, which stays on
        view when it was set by hand rather than snapping to a preset.
        Returns the values in the order shown."""
        values = sorted({*presets, current})
        menu.removeAllItems()
        menu.addItemsWithTitles_([label(value) for value in values])
        menu.selectItemAtIndex_(values.index(current))
        return values

    @staticmethod
    def _chosen(menu, values):
        """The value picked in `menu`; None while nothing is selected (-1)."""
        index = menu.indexOfSelectedItem()
        return values[index] if 0 <= index < len(values) else None

    @objc.python_method
    def _row_to_edge(self, views):
        """A row as wide as the page, so a spacer in it pushes what follows to the page's edge."""
        row = stack(views, horizontal=True, spacing=ROW_GAP)
        row.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH).setActive_(True)
        return row

    @objc.python_method
    def _page(self, groups):
        """Groups are lists of views; space between groups is wider than
        the space inside one."""
        views = [view for group in groups for view in group]
        page = stack(views, spacing=PAGE_SPACING)
        for group in groups[:-1]:
            page.setCustomSpacing_afterView_(SECTION_GAP, group[-1])
        return page

    # -- Pages --

    @objc.python_method
    def _general_page(self):
        name = NSTextField.labelWithString_("Maramax")
        name.setFont_(NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold))
        version = NSTextField.labelWithString_(f"Version {__version__}")
        version.setTextColor_(NSColor.secondaryLabelColor())
        about = self._row_to_edge([name, version, spacer(), self._button("Welcome Guide…", "showWelcome:")])

        shortcut_label = NSTextField.labelWithString_("Shortcut")
        # The picker's notes wrap within what the label leaves of the row.
        self.shortcut_picker = ShortcutPicker.alloc().initWithOwner_width_onResize_(
            self.delegate, CONTENT_WIDTH - shortcut_label.fittingSize().width - ROW_GAP, self._fit_window_to_page)
        shortcut_row = stack([shortcut_label, self.shortcut_picker.view], horizontal=True, spacing=ROW_GAP)

        return self._page([
            [about],
            [shortcut_row],
            [self._header("When you finish dictating"), *self._delivery_choices()],
            [self._header("Dictation"), self._option("compact_dictation"), self._option("auto_start_recording"),
             self._option("live_preview")],
        ])

    @objc.python_method
    def _delivery_choices(self):
        """One choice of three, so copying and pasting cannot both end up
        off by accident; choosing that on purpose shows a warning."""
        self.delivery_buttons = {}
        self.delivery_help = {}
        units = []
        for delivery, title in DELIVERY_LABELS.items():
            button = NSButton.radioButtonWithTitle_target_action_(title, self, "chooseDelivery:")
            self.delivery_buttons[delivery] = button
            help_label = self._help(_DELIVERY_HELP[delivery], CONTENT_WIDTH - CHECKBOX_INDENT)
            self.delivery_help[delivery] = help_label
            views = [help_label]
            if delivery is Delivery.PASTED:
                self.permission_note = self._help("", CONTENT_WIDTH - CHECKBOX_INDENT - 100)
                self.permission_button = self._button("Allow…", "requestPastePermission:")
                self.permission_row = stack([self.permission_note, self.permission_button], horizontal=True,
                                            spacing=ROW_GAP)
                views.append(self.permission_row)
            units.append(self._with_help(button, *views))
        return units

    @objc.python_method
    def _microphone_page(self):
        self.device_picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 25), False)
        self.device_picker.setTarget_(self)
        self.device_picker.setAction_("selectDevice:")
        refresh = self._button("Refresh", "refreshDevices:")
        # The row ends where the page's text does.
        self.device_picker.widthAnchor().constraintEqualToConstant_(
            CONTENT_WIDTH - aligned_width(refresh) - ROW_GAP).setActive_(True)
        picker_row = stack([self.device_picker, refresh], horizontal=True, spacing=ROW_GAP)

        self.keep_ready = self._choice_menu("selectKeepReady:", 170)
        keep_row = stack([NSTextField.labelWithString_("Keep the microphone connected"), self.keep_ready],
                         horizontal=True, spacing=ROW_GAP)
        return self._page([
            [self._header("Input"), picker_row,
             self._help("Automatic uses the Mac’s own microphone while the option below is on, otherwise the "
                        "input chosen in macOS. If a microphone disconnects while you dictate, Automatic carries "
                        "on with the next one. A microphone you choose here is never swapped: the dictation ends "
                        "with what was recorded, and the audio is saved."),
             self._option("prefer_builtin_mic")],
            [self._header("After a dictation"), keep_row,
             self._help("Bluetooth microphones such as AirPods take 2–3 seconds to connect each time. Keeping "
                        "the connection open makes the next dictation start at once. While it is open, macOS "
                        "shows the microphone indicator and AirPods play in call quality. Nothing heard while "
                        "waiting is recorded.")],
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

    @objc.python_method
    def _advanced_page(self):
        self.model_status = self._help("", CONTENT_WIDTH - 80)
        self.model_retry = self._button("Retry", "retryModel:")
        self.update_check = self._button("Check Now", "checkForUpdates:")
        self.update_status = self._help("", CONTENT_WIDTH - 120)
        self.history_limit = self._choice_menu("selectHistoryLimit:", 160)
        self.recordings_limit = self._choice_menu("selectRecordingsLimit:", 160)
        history_row = stack([NSTextField.labelWithString_("Keep the last"), self.history_limit,
                             NSTextField.labelWithString_("and"), self.recordings_limit],
                            horizontal=True, spacing=ROW_GAP)
        self.clear_history = self._button("Clear History & Recordings…", "clearHistory:")
        return self._page([
            [self._header("Speech model"), self._option("high_accuracy"),
             stack([self.model_status, self.model_retry], horizontal=True, spacing=ROW_GAP)],
            [self._header("Updates"), self._option("check_for_updates"),
             stack([self.update_check, self.update_status], horizontal=True, spacing=ROW_GAP)],
            [self._header("History"), history_row, self._help(_HISTORY_NOTE), self.clear_history],
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
        self._show_delivery()
        self.model_status.setStringValue_(self._model_message())
        self.model_retry.setHidden_(not self.delegate.models_failed())
        if not self.shortcut_picker.is_recording():
            self.shortcut_picker.refresh()
        self.show_update_status()

        self._keep_ready_values = self._offer(self.keep_ready, _KEEP_READY_CHOICES, config.keep_mic_ready_seconds,
                                             duration_label)
        self._history_limits = self._offer(self.history_limit, _HISTORY_CHOICES, config.history_limit,
                                           lambda count: count_label(count, "transcript"))
        self._recordings_limits = self._offer(self.recordings_limit, _RECORDINGS_CHOICES, config.recordings_limit,
                                              lambda count: count_label(count, "recording"))

        if config.input_device in self.device_names:
            self.device_picker.selectItemAtIndex_(self.device_names.index(config.input_device))
        self.show_busy_state()
        self.replacements.refresh()
        # Notes appear and go, and the model status can grow to two lines.
        self._fit_window_to_page()

    @objc.python_method
    def _show_delivery(self):
        delivery = self.delegate.config.delivery()
        for choice, button in self.delivery_buttons.items():
            button.setState_(int(choice is delivery))
        self.permission_row.setHidden_(delivery is not Delivery.PASTED)
        permitted = self.delegate.paste_permitted()
        self.permission_button.setHidden_(permitted)
        if permitted:
            show_notice(self.permission_note, Notice.ALLOWED, "Allowed in Privacy & Security → Accessibility.")
        else:
            show_notice(self.permission_note, Notice.WARNING, "Not allowed yet, so transcripts are only copied.")
        kept = self.delivery_help[Delivery.KEPT]
        if delivery is Delivery.KEPT:
            show_notice(kept, Notice.WARNING, _DELIVERY_HELP[Delivery.KEPT])
        else:
            kept.setStringValue_(_DELIVERY_HELP[Delivery.KEPT])

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
        """A microphone cannot be chosen, nor history cleared, during a dictation or transcription."""
        self.device_picker.setEnabled_(not self.delegate.is_busy)
        self.clear_history.setEnabled_(not self.delegate.is_busy)

    @objc.python_method
    def show(self):
        self.refresh()
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        if self.selected_tab == MICROPHONE_TAB:
            self.refreshDevices_(None)

    def windowDidBecomeKey_(self, notification):
        del notification
        # Accessibility may have been switched on or off in System Settings
        # meanwhile; Allow… comes or goes with it.
        self._show_delivery()
        self._fit_window_to_page()

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
    def update_input_devices(self, listed, selected_name, automatic_name=None):
        """`listed` is the inputs macOS has now, or None before a listing has arrived."""
        names = list(dict.fromkeys(listed or []))
        # Only a listing can say a chosen microphone is not connected.
        missing = listed is not None and selected_name is not None and selected_name not in names
        if selected_name is not None and selected_name not in names:
            names.insert(0, selected_name)
        self.device_names = [None] + names
        # Automatic names the microphone it would use right now, so there is
        # no guessing which one "automatic" means.
        automatic = f"Automatic — {automatic_name}" if automatic_name else "Automatic"
        # A chosen microphone that is not connected says so, rather than
        # looking like one that will record.
        titles = [f"{name} (not connected)" if missing and name == selected_name else name for name in names]
        self.device_picker.removeAllItems()
        self.device_picker.addItemsWithTitles_([automatic] + titles)
        self.device_picker.selectItemAtIndex_(self.device_names.index(selected_name))
        self.show_busy_state()

    def selectDevice_(self, sender):
        self.delegate.select_input_device(self.device_names[self.device_picker.indexOfSelectedItem()])

    def selectKeepReady_(self, sender):
        del sender
        seconds = self._chosen(self.keep_ready, self._keep_ready_values)
        if seconds is not None:
            self.delegate.set_keep_microphone_ready(seconds)

    def selectHistoryLimit_(self, sender):
        del sender
        count = self._chosen(self.history_limit, self._history_limits)
        if count is not None:
            self.delegate.set_history_limit(count)

    def selectRecordingsLimit_(self, sender):
        del sender
        count = self._chosen(self.recordings_limit, self._recordings_limits)
        if count is not None:
            self.delegate.set_recordings_limit(count)

    def toggleSetting_(self, sender):
        for name, button in self.options.items():
            if button is sender:
                self.delegate.toggle_setting(name)
                return

    def chooseDelivery_(self, sender):
        for delivery, button in self.delivery_buttons.items():
            if button is sender:
                self.delegate.choose_delivery(delivery)
                return

    def requestPastePermission_(self, sender):
        del sender
        self.delegate.request_paste_permission()

    def showWelcome_(self, sender):
        del sender
        self.delegate.show_welcome()

    def clearHistory_(self, sender):
        del sender
        self.delegate.clear_history_requested()

    def retryModel_(self, sender):
        del sender
        self.delegate.retry_speech_model()
        self.refresh()
