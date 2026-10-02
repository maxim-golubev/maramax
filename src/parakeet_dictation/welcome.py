"""The welcome window: the first launch's four steps, from what Maramax is to a first dictation."""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSFontWeightSemibold, NSLayoutAttributeLeading,
    NSLayoutConstraint, NSLayoutPriorityDefaultLow, NSMakeRect, NSPanel, NSStackView, NSTextField,
    NSUserInterfaceLayoutOrientationHorizontal, NSView,
    NSUserInterfaceLayoutOrientationVertical, NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from .hotkeys import STOP
from .main_thread import call_later
from .shortcut_picker import ShortcutPicker

MARGIN = 28
CONTENT_WIDTH = 480
STEPS = 4
# How often the first step looks at the speech model while its window is open.
STATUS_SECONDS = 1.0


def try_it_text(shortcut: str, pastes: bool) -> str:
    result = "copied and pasted where you are typing" if pastes else "copied, ready to paste with Cmd+V"
    return (f"Click into any text field, press {shortcut}, and say a sentence. Press {shortcut} again "
            f"(or {STOP.label}): in a moment the text is {result}.")


class WelcomeController(NSObject):
    """`delegate` provides what the picker needs (see shortcut_picker.py) and
    config, transcriber, set_paste_into_apps(bool), paste_permitted(),
    open_accessibility_settings(), and finish_welcome()."""

    def initWithDelegate_(self, delegate):
        self = objc.super(WelcomeController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self.step = 0
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, 300), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("Welcome to Maramax")
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)
        self.picker = ShortcutPicker.alloc().initWithOwner_width_(delegate, CONTENT_WIDTH)
        root = self.panel.contentView()
        self.pages = [self._welcome_page(), self._shortcut_page(), self._result_page(), self._try_page()]
        self.counter = self._small("")
        self.back = NSButton.buttonWithTitle_target_action_("Back", self, "goBack:")
        self.forward = NSButton.buttonWithTitle_target_action_("Continue", self, "goForward:")
        self.forward.setKeyEquivalent_("\r")
        row = self._stack([self.counter, _spacer(), self.back, self.forward], horizontal=True)
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
    def _stack(self, views, horizontal=False, spacing=10):
        stack = NSStackView.stackViewWithViews_(views)
        stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal if horizontal
                              else NSUserInterfaceLayoutOrientationVertical)
        if not horizontal:
            stack.setAlignment_(NSLayoutAttributeLeading)
        stack.setSpacing_(spacing)
        return stack

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

    @objc.python_method
    def _small(self, text):
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFont_(NSFont.systemFontOfSize_(11))
        label.setTextColor_(NSColor.secondaryLabelColor())
        label.setSelectable_(False)
        label.setPreferredMaxLayoutWidth_(CONTENT_WIDTH)
        return label

    # -- The four steps --

    @objc.python_method
    def _welcome_page(self):
        self.model_status = self._small("")
        return self._stack([
            self._title("Welcome to Maramax", 20),
            self._body("Dictation that runs entirely on this Mac. Press a shortcut, speak, press it again, and the "
                       "text is ready to paste. Nothing you say leaves your computer."),
            self.model_status,
        ], spacing=12)

    @objc.python_method
    def _shortcut_page(self):
        return self._stack([
            self._title("Choose your shortcut"),
            self._body(f"Press it to start dictating, and again to finish. While you dictate, {STOP.label} "
                       "finishes too. You can change it later in Settings."),
            self.picker.view,
        ], spacing=12)

    @objc.python_method
    def _result_page(self):
        self.copy_choice = NSButton.radioButtonWithTitle_target_action_("Copy the transcript", self, "choosePaste:")
        self.paste_choice = NSButton.radioButtonWithTitle_target_action_(
            "Copy it and paste it into the app you are using", self, "choosePaste:")
        self.permission = NSButton.buttonWithTitle_target_action_("Open Accessibility Settings", self,
                                                                 "openAccessibility:")
        self.permission_note = self._small("")
        self.permission_row = self._stack([self.permission, self.permission_note], horizontal=True, spacing=8)
        return self._stack([
            self._title("When you finish speaking"),
            self.copy_choice,
            self._indented(self._small("Paste it yourself with Cmd+V. Nothing needs extra permission.")),
            self.paste_choice,
            self._indented(self._small("Maramax presses Cmd+V for you in the app you were typing in. macOS asks "
                                       "once for Accessibility permission.")),
            self._indented(self.permission_row),
        ], spacing=6)

    @objc.python_method
    def _indented(self, view):
        stack = self._stack([view])
        stack.setEdgeInsets_((0, 20, 0, 0))
        return stack

    @objc.python_method
    def _try_page(self):
        self.try_text = self._body("")
        return self._stack([
            self._title("Try it"),
            self.try_text,
            self._small("A small bar at the bottom of the screen shows the microphone and the time. Bluetooth "
                        "headphones take two or three seconds to connect: start speaking when the bar says "
                        "Recording. macOS asks for the microphone the first time."),
            self._small("Settings and Recordings are in the menu bar icon; this window is under More → Welcome."),
        ], spacing=12)

    # -- Showing --

    @objc.python_method
    def show(self):
        self.show_step(0)
        if not self.panel.isVisible():
            self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self._watch_model()

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
        self._fit_window()

    @objc.python_method
    def refresh(self):
        config = self.delegate.config
        self.model_status.setStringValue_(self.delegate.transcriber.status_message() + ".")
        self.picker.refresh()
        pastes = config.paste_to_active_app
        self.copy_choice.setState_(int(not pastes))
        self.paste_choice.setState_(int(pastes))
        permitted = self.delegate.paste_permitted()
        self.permission_row.setHidden_(not pastes)
        self.permission.setHidden_(permitted)
        self.permission_note.setStringValue_("Permission granted." if permitted else "Not granted yet.")
        self.try_text.setStringValue_(try_it_text(self.delegate.current_shortcut().label, pastes))

    @objc.python_method
    def _fit_window(self):
        root = self.panel.contentView()
        root.layoutSubtreeIfNeeded()
        page = self.pages[self.step]
        height = MARGIN + page.fittingSize().height + 28 + 52
        frame = self.panel.frameRectForContentRect_(NSMakeRect(0, 0, CONTENT_WIDTH + 2 * MARGIN, height))
        current = self.panel.frame()
        top = current.origin.y + current.size.height
        self.panel.setFrame_display_(
            NSMakeRect(current.origin.x, top - frame.size.height, frame.size.width, frame.size.height), True)

    @objc.python_method
    def _watch_model(self):
        # The first step says whether the speech model is still downloading.
        if self.panel.isVisible():
            self.model_status.setStringValue_(self.delegate.transcriber.status_message() + ".")
            if self.step == 2:
                self.refresh()  # Accessibility may have been granted meanwhile.
            call_later(STATUS_SECONDS, self._watch_model)

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

    def choosePaste_(self, sender):
        self.delegate.set_paste_into_apps(sender is self.paste_choice)
        self.refresh()
        self._fit_window()

    def openAccessibility_(self, sender):
        del sender
        self.delegate.open_accessibility_settings()

    def windowWillClose_(self, notification):
        del notification
        self.picker.stop_recording()  # Never leave the global shortcut paused.
        self.delegate.finish_welcome()


def _spacer():
    """A view that takes up the free width in a horizontal row."""
    spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
    spacer.setContentHuggingPriority_forOrientation_(NSLayoutPriorityDefaultLow - 1, 0)
    return spacer
