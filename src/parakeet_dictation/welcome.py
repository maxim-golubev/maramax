"""The welcome window: the first launch's four steps, from what Maramax is to a first dictation."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSFont, NSFontWeightSemibold, NSLayoutConstraint,
    NSMakeRect, NSPanel, NSTextField, NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from .config import AppConfig, Delivery
from .hotkeys import STOP
from .layout import Notice, aligned_width, show_notice, small_text, spacer, stack
from .main_thread import call_later
from .preferences import CHECKBOX_INDENT, DELIVERY_LABELS
from .shortcut_picker import ShortcutPicker

MARGIN = 28
CONTENT_WIDTH = 480
STEPS = 4
# How often the first step looks at the speech model while its window is open.
STATUS_SECONDS = 1.0


_OUTCOMES = {
    Delivery.PASTED: "pasted where you are typing",
    Delivery.COPIED: "copied, ready to paste with Cmd+V",
    Delivery.KEPT: "under Open Transcript in the menu bar icon",
}
# The two choices offered here; Settings also offers keeping transcripts in Maramax.
_CHOICES = (Delivery.PASTED, Delivery.COPIED)
_CHOICE_HELP = {
    Delivery.PASTED: "Maramax presses Cmd+V for you in the app you were typing in. macOS asks you once to allow it.",
    Delivery.COPIED: "Paste it yourself with Cmd+V. Nothing needs extra permission.",
}


def try_it_text(shortcut: str, config: AppConfig) -> str:
    outcome = _OUTCOMES[config.delivery()]
    if config.auto_start_recording:
        return (f"Click into any text field, press {shortcut}, and say a sentence. Press {shortcut} again "
                f"(or {STOP.label}): in a moment the text is {outcome}.")
    return (f"Click into any text field and press {shortcut} to open Maramax, then {STOP.label} to start. Say a "
            f"sentence and press {STOP.label} again: in a moment the text is {outcome}.")


def shortcut_page_text(config: AppConfig) -> str:
    if config.auto_start_recording:
        return (f"Press it to start dictating, and again to finish. While you dictate, {STOP.label} finishes too. "
                "You can change it later in Settings.")
    return (f"Press it to open Maramax, then {STOP.label} to start dictating and again to finish. You can change "
            "it later in Settings.")


def recording_note(config: AppConfig) -> str:
    """What shows while the first dictation records."""
    # The shortcut opens the full window instead when it does not start recording.
    shown = ("A small bar at the bottom of the screen shows the microphone and the time."
             if config.compact_dictation and config.auto_start_recording else
             "The Maramax window shows the microphone while you speak.")
    return (f"{shown} While it says “Don’t speak yet” in orange, wait: Bluetooth headphones take two or three "
            "seconds to connect. macOS asks for the microphone the first time.")


class WelcomeController(NSObject):
    """`delegate` provides what the picker needs (see shortcut_picker.py) and
    config, transcriber, choose_delivery(delivery), paste_permitted(),
    request_paste_permission(), and finish_welcome()."""

    def initWithDelegate_(self, delegate):
        self = objc.super(WelcomeController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self.step = 0
        # Each show() starts a new watch; an older one stops at its next look.
        self._watch_generation = 0
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, 300), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("Welcome to Maramax")
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)
        self.picker = ShortcutPicker.alloc().initWithOwner_width_onResize_(delegate, CONTENT_WIDTH, self._fit_window)
        root = self.panel.contentView()
        self.pages = [self._welcome_page(), self._shortcut_page(), self._result_page(), self._try_page()]
        self.counter = small_text("", CONTENT_WIDTH)
        self.back = NSButton.buttonWithTitle_target_action_("Back", self, "goBack:")
        self.forward = NSButton.buttonWithTitle_target_action_("Continue", self, "goForward:")
        self.forward.setKeyEquivalent_("\r")
        row = stack([self.counter, spacer(), self.back, self.forward], horizontal=True, spacing=10)
        # One width for both (once they share a parent), whatever they say, so
        # neither moves from step to step when "Continue" becomes "Done".
        self.forward.widthAnchor().constraintEqualToConstant_(aligned_width(self.forward)).setActive_(True)
        self.back.widthAnchor().constraintEqualToAnchor_(self.forward.widthAnchor()).setActive_(True)
        constraints = []
        for page in [*self.pages, row]:
            page.setTranslatesAutoresizingMaskIntoConstraints_(False)
            root.addSubview_(page)
            constraints += [page.leadingAnchor().constraintEqualToAnchor_constant_(root.leadingAnchor(), MARGIN),
                            page.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH)]
        for page in self.pages:
            constraints.append(page.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), MARGIN))
        constraints.append(row.bottomAnchor().constraintEqualToAnchor_constant_(root.bottomAnchor(), -20))
        NSLayoutConstraint.activateConstraints_(constraints)
        self.show_step(0)
        return self

    # -- Building blocks --

    @objc.python_method
    def _title(self, text, size=17):
        label = NSTextField.labelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold))
        return label

    @objc.python_method
    def _body(self, text):
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_(13))
        label.setSelectable_(False)
        label.setPreferredMaxLayoutWidth_(CONTENT_WIDTH)
        return label

    # -- The four steps --

    @objc.python_method
    def _welcome_page(self):
        self.model_status = small_text("", CONTENT_WIDTH)
        return stack([
            self._title("Welcome to Maramax", 20),
            self._body("Dictation that runs entirely on this Mac. Press a shortcut, speak, press it again, and the "
                       "text is ready to paste. Nothing you say leaves your computer."),
            self.model_status,
        ], spacing=12)

    @objc.python_method
    def _shortcut_page(self):
        self.shortcut_text = self._body("")
        return stack([
            self._title("Choose your shortcut"),
            self.shortcut_text,
            self.picker.view,
        ], spacing=12)

    @objc.python_method
    def _result_page(self):
        self.choices = {delivery: NSButton.radioButtonWithTitle_target_action_(
            DELIVERY_LABELS[delivery], self, "chooseDelivery:") for delivery in _CHOICES}
        self.permission = NSButton.buttonWithTitle_target_action_("Allow…", self, "requestPermission:")
        self.permission_note = small_text("", CONTENT_WIDTH - 120)
        self.permission_row = stack([self.permission_note, self.permission], horizontal=True)
        title = self._title("When you finish dictating")
        views = [title]
        for delivery in _CHOICES:
            explanation = [small_text(_CHOICE_HELP[delivery], CONTENT_WIDTH - CHECKBOX_INDENT)]
            if delivery is Delivery.PASTED:
                explanation.append(self.permission_row)
            views += [self.choices[delivery], self._indented(explanation)]
        page = stack(views, spacing=10)
        # As in Settings: the title stands apart as on every step, and each
        # choice's explanation sits close under it.
        page.setCustomSpacing_afterView_(12, title)
        for choice in self.choices.values():
            page.setCustomSpacing_afterView_(3, choice)
        return page

    @objc.python_method
    def _indented(self, views):
        indented = stack(views, spacing=6)
        indented.setEdgeInsets_((0, CHECKBOX_INDENT, 0, 0))
        return indented

    @objc.python_method
    def _try_page(self):
        self.try_text = self._body("")
        self.recording_note = small_text("", CONTENT_WIDTH)
        return stack([
            self._title("Try it"),
            self.try_text,
            self.recording_note,
            small_text("Settings and Recordings are in the menu bar icon. This guide is in Settings → General.",
                       CONTENT_WIDTH),
        ], spacing=12)

    # -- Showing --

    @objc.python_method
    def show(self):
        self.show_step(0)
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self._watch_generation += 1
        self._watch_model(self._watch_generation)

    @objc.python_method
    def show_step(self, step):
        self.step = step
        if step != 1:
            self.picker.stop_recording()
        for index, page in enumerate(self.pages):
            page.setHidden_(index != step)
        self.refresh()
        self.counter.setStringValue_(f"{step + 1} of {STEPS}")
        self.back.setHidden_(step == 0)
        self.forward.setTitle_("Done" if step == STEPS - 1 else "Continue")

    @objc.python_method
    def refresh(self):
        config = self.delegate.config
        self.model_status.setStringValue_(self.delegate.transcriber.status_message() + ".")
        self.picker.refresh()
        self.shortcut_text.setStringValue_(shortcut_page_text(config))
        delivery = config.delivery()
        # With "Keep in Maramax only" chosen in Settings, neither choice here is what happens now.
        for choice, button in self.choices.items():
            button.setState_(int(choice is delivery))
        permitted = self.delegate.paste_permitted()
        self.permission_row.setHidden_(delivery is not Delivery.PASTED)
        self.permission.setHidden_(permitted)
        if permitted:
            show_notice(self.permission_note, Notice.ALLOWED, "Allowed.")
        else:
            show_notice(self.permission_note, Notice.WARNING, "Not allowed yet.")
        self.try_text.setStringValue_(try_it_text(self.delegate.current_shortcut().label, config))
        self.recording_note.setStringValue_(recording_note(config))
        self._fit_window()  # Allow… comes and goes with the permission, wherever the refresh came from.

    @objc.python_method
    def _fit_window(self):
        root = self.panel.contentView()
        root.layoutSubtreeIfNeeded()
        page = self.pages[self.step]
        height = MARGIN + page.fittingSize().height + 28 + 52
        frame = self.panel.frameRectForContentRect_(NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, height))
        current = self.panel.frame()
        if abs(current.size.height - frame.size.height) < 0.5:
            return  # Refreshed with nothing that changes the height (the model watch looks every second).
        top = current.origin.y + current.size.height
        self.panel.setFrame_display_(
            NSMakeRect(current.origin.x, top - frame.size.height, frame.size.width, frame.size.height), True)

    @objc.python_method
    def _watch_model(self, generation):
        # The first step says whether the speech model is still downloading.
        if generation == self._watch_generation and self.panel.isVisible():
            self.model_status.setStringValue_(self.delegate.transcriber.status_message() + ".")
            if self.step == 2:
                self.refresh()  # Accessibility may have been granted meanwhile.
            call_later(STATUS_SECONDS, self._watch_model, generation)

    # -- Actions --

    def goBack_(self, sender):
        del sender
        self.show_step(max(0, self.step - 1))

    def goForward_(self, sender):
        del sender
        if self.step == STEPS - 1:
            self.panel.close()  # windowWillClose_ records that the welcome was seen.
        else:
            self.show_step(self.step + 1)

    def chooseDelivery_(self, sender):
        for delivery, button in self.choices.items():
            if button is sender:
                self.delegate.choose_delivery(delivery)
        self.refresh()

    def requestPermission_(self, sender):
        del sender
        self.delegate.request_paste_permission()

    def windowWillClose_(self, notification):
        del notification
        self.picker.stop_recording()  # Never leave the global shortcut paused.
        self.delegate.finish_welcome()
