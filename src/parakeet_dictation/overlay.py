"""The full transcript window: result, history, and file-queue tabs with drag-and-drop."""

from __future__ import annotations

import warnings
from enum import StrEnum
from pathlib import Path

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationDidResignActiveNotification,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSDragOperationCopy,
    NSEventModifierFlagCommand,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSMakeRect,
    NSOpenPanel,
    NSPanel,
    NSPopUpButton,
    NSSavePanel,
    NSScrollView,
    NSSegmentedControl,
    NSTextField,
    NSTextAlignmentCenter,
    NSTextView,
    NSView,
    NSWindowBelow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowDidBecomeKeyNotification,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRange, NSNotificationCenter, NSObject

from .capture import CaptureHealth
from .export import Destination, OutputMode, ToFile, ToFolder
from .file_queue import QueueStatus
from .hotkeys import STOP
from .main_thread import call_later

MEDIA_EXTENSIONS = [
    "aac", "aiff", "flac", "m4a", "mov", "mp3", "mp4", "ogg", "opus", "wav", "webm",
]

_COMMAND_ONLY_MASK = (
    NSEventModifierFlagCommand
    | (1 << 17)   # NSEventModifierFlagShift
    | (1 << 18)   # NSEventModifierFlagControl
    | (1 << 19)   # NSEventModifierFlagOption
)

_QUEUE_STATUS_TEXT = {
    QueueStatus.PENDING: "",
    QueueStatus.PROCESSING: "transcribing…",
    QueueStatus.DONE: "done",
    QueueStatus.FAILED: "failed",
    QueueStatus.CANCELLED: "cancelled",
}
DROP_HINT = "Drop a file to transcribe it, or several to add them to the queue."

# One set of measurements for every state of the window. Positions are of
# what the eye sees (alignment rectangles), not of control frames.
WIDTH = 688
MARGIN = 24
CONTENT = WIDTH - 2 * MARGIN
BOTTOM = 16          # below the lowest element
ROW = 26             # visible height of a row of controls (the tallest is the tab control)
CONTROL_FRAME_HEIGHT = 34  # push-button frames are taller than what they draw
GAP = 10             # between a row of controls and a text area
BUTTON_GAP = 12
STATUS_BELOW_TOP = 40
STATUS_HEIGHT = 20
STATUS_TO_TABS = 8   # between the status line and the tab row below it
CLOSE_WIDTH = 76
CANCEL_WIDTH = 108   # the same button, alone and centred, while something is running


def _utf16_length(text: str) -> int:
    """AppKit measures text in UTF-16 units; an emoji is two of them."""
    return len(text.encode("utf-16-le")) // 2


class Mode(StrEnum):
    RESULT = "result"
    HISTORY = "history"
    QUEUE = "queue"


class DropTarget(StrEnum):
    TRANSCRIBE = "transcribe"   # one file, at once
    QUEUE = "queue"


def drop_target(mode: Mode, count: int, transcribing: bool) -> DropTarget:
    """What dropping `count` media files does. One file dropped outside the
    Queue tab is transcribed at once, unless something is being transcribed:
    then it waits in the queue like several would."""
    return DropTarget.TRANSCRIBE if mode != Mode.QUEUE and count == 1 and not transcribing else DropTarget.QUEUE


_DROP_FEEDBACK = {DropTarget.TRANSCRIBE: "Drop to transcribe", DropTarget.QUEUE: "Drop to add to the queue"}


_SEGMENTS = (Mode.RESULT, Mode.HISTORY, Mode.QUEUE)


def _is_media(path: str) -> bool:
    return "." in path and path.rsplit(".", 1)[-1].lower() in MEDIA_EXTENSIONS


class OverlayPanel(NSPanel):
    controller = objc.ivar()

    def initWithContentRect_styleMask_backing_defer_controller_(
        self,
        content_rect,
        style_mask,
        backing,
        defer,
        controller,
    ):
        self = objc.super(OverlayPanel, self).initWithContentRect_styleMask_backing_defer_(
            content_rect,
            style_mask,
            backing,
            defer,
        )
        if self is None:
            return None

        self.controller = controller
        return self

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def performKeyEquivalent_(self, event):
        flags = int(event.modifierFlags()) & _COMMAND_ONLY_MASK
        delegate = self.controller.delegate

        if event.charactersIgnoringModifiers() == "\x1b":
            delegate.dismiss_requested()
            return True

        # Matched on what the layout types with Cmd held: Russian and other
        # non-Latin layouts give Latin letters there, as menu shortcuts
        # expect. Matching key codes would move these shortcuts on AZERTY.
        chars = (event.characters() or "").lower()

        if flags == NSEventModifierFlagCommand and chars == "r":
            delegate.toggle_recording_requested()
            return True

        if flags == NSEventModifierFlagCommand and chars == "w":
            # A borderless panel cannot answer the menu's Close Window, which
            # would beep; closing here does what the Close button does.
            delegate.dismiss_requested()
            return True

        if flags == NSEventModifierFlagCommand and chars == "c":
            # If the user selected text in one of the text views, copy just
            # the selection instead of the whole transcript.
            responder = self.firstResponder()
            if isinstance(responder, NSTextView) and responder.selectedRange().length > 0:
                responder.copy_(None)
                return True
            delegate.copy_current_transcript()
            return True

        return objc.super(OverlayPanel, self).performKeyEquivalent_(event)

    def cancelOperation_(self, sender):
        del sender
        self.controller.delegate.dismiss_requested()


class OverlayDropView(NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(OverlayDropView, self).initWithFrame_(frame)
        if self is None:
            return None

        self.controller = controller
        self.registerForDraggedTypes_(["public.file-url"])
        self.setWantsLayer_(True)
        return self

    def viewDidChangeEffectiveAppearance(self):
        self.controller.apply_appearance()

    @objc.python_method
    def _dragged_media(self, sender):
        urls = sender.draggingPasteboard().readObjectsForClasses_options_([objc.lookUpClass("NSURL")], None) or []
        return [url.path() for url in urls if url.path() and _is_media(url.path())]

    def draggingEntered_(self, sender):
        # A drop starts or queues work and opens the Queue tab, over Stop
        # and the live draft: not while recording.
        media = self._dragged_media(sender)
        if not self.controller.is_recording and media:
            self.controller.set_drop_state(self.controller.drop_target(len(media)))
            return NSDragOperationCopy
        return 0

    def draggingExited_(self, sender):
        del sender
        self.controller.set_drop_state(None)

    def prepareForDragOperation_(self, sender):
        del sender
        return True

    def performDragOperation_(self, sender):
        paths = self._dragged_media(sender)
        self.controller.set_drop_state(None)
        if not paths:
            return False
        self.controller.files_dropped(paths)
        return True


class OverlayController(NSObject):
    TRANSCRIBING_HEIGHT = 88
    RECORDING_HEIGHT = 128
    IDLE_HEIGHT = 148
    EXPANDED_HEIGHT = 224
    QUEUE_HEIGHT = 310

    def initWithDelegate_(self, delegate):
        self = objc.super(OverlayController, self).init()
        if self is None:
            return None

        self.delegate = delegate
        self.mode = Mode.RESULT
        self.current_text = ""
        self.history_text = ""
        # Shown in the Result tab until there is a transcript. It is
        # guidance, not a transcript: Copy stays disabled for it.
        self.intro_text = ""
        self.is_recording = False
        self.is_transcribing = False
        self._status = ""
        self._copy_feedback_token = 0
        self._copy_feedback_visible = False
        self._queue_files = []
        self._queue_processing = False
        self._selected_queue_file_id = None
        self._rendering_queue = False
        self._positioned = False
        self._build_window()
        self._refresh_text_view()
        self._apply_recording_state()
        self._update_layout()
        return self

    # -- Construction --

    @objc.python_method
    def _build_window(self):
        frame = NSMakeRect(0, 0, WIDTH, self.IDLE_HEIGHT)
        self.panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_controller_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
            self,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setMovableByWindowBackground_(True)
        # Above other apps' windows, below alerts and file panels, which
        # must never open behind it (see windowBecameKey_).
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setWorksWhenModal_(True)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        center = NSNotificationCenter.defaultCenter()
        center.addObserver_selector_name_object_(self, "windowBecameKey:", NSWindowDidBecomeKeyNotification, None)
        center.addObserver_selector_name_object_(self, "appResignedActive:",
                                                 NSApplicationDidResignActiveNotification, None)

        self.content_view = OverlayDropView.alloc().initWithFrame_controller_(frame, self)
        self.content_view.layer().setCornerRadius_(18.0)
        self.content_view.layer().setMasksToBounds_(True)
        self.content_view.layer().setBorderWidth_(1.0)
        self.apply_appearance()
        self.panel.setContentView_(self.content_view)

        self.status_label = self._make_label("", 13, True)
        # One line: a long status is shortened with an ellipsis and shown
        # in full as a tooltip, never wrapped out of its row.
        self.status_label.setLineBreakMode_(NSLineBreakByTruncatingTail)
        self.status_label.cell().setUsesSingleLineMode_(True)
        # Microphone and elapsed time while recording without a live draft.
        self.detail_label = self._make_label("", 11, False)
        self.detail_label.setTextColor_(NSColor.secondaryLabelColor())
        self.drop_label = self._make_label(DROP_HINT, 11, False)
        self.drop_label.setTextColor_(NSColor.secondaryLabelColor())

        self.mode_control = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 28))
        self.mode_control.setSegmentCount_(len(_SEGMENTS))
        for index, mode in enumerate(_SEGMENTS):
            self.mode_control.setLabel_forSegment_(mode.title(), index)
        self.mode_control.setSelectedSegment_(0)
        self.mode_control.setTarget_(self)
        self.mode_control.setAction_("toggleMode:")

        self.record_button = self._make_button("", "toggleRecording:")
        self.copy_button = self._make_button("Copy", "copyTranscript:")
        self.files_button = self._make_button("Files…", "openFiles:")
        self.close_button = self._make_button("Close", "closeOverlay:")

        self.scroll_view, self.text_view = self._make_text_area(NSFont.systemFontOfSize_(13))
        self.queue_scroll_view, self.queue_text_view = self._make_text_area(
            NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        self.queue_text_view.setDelegate_(self)

        self.queue_add_button = self._make_button("Add…", "queueAddFiles:")
        self.queue_up_button = self._make_button("▲", "queueMoveUp:")
        self.queue_down_button = self._make_button("▼", "queueMoveDown:")
        self.queue_remove_button = self._make_button("Remove", "queueRemove:")
        self.queue_clear_button = self._make_button("Clear", "queueClear:")
        self.queue_start_button = self._make_button("Start", "queueStart:")
        self.queue_start_button.setBezelColor_(NSColor.systemGreenColor())

        self._queue_buttons = (
            self.queue_add_button, self.queue_up_button, self.queue_down_button,
            self.queue_remove_button, self.queue_clear_button, self.queue_start_button,
        )
        self._transcript_buttons = (self.record_button, self.copy_button, self.files_button)
        self._all_views = (
            self.status_label, self.detail_label, self.drop_label, self.mode_control,
            *self._transcript_buttons, self.close_button, self.scroll_view,
            self.queue_scroll_view, *self._queue_buttons,
        )
        for view in self._all_views:
            self.content_view.addSubview_(view)

    @objc.python_method
    def _make_text_area(self, font):
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT, 100))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        # Text areas share the panel's rounded language instead of meeting
        # it with square white corners.
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(8.0)
        scroll.layer().setMasksToBounds_(True)
        text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT, 100))
        text.setEditable_(False)
        text.setSelectable_(True)
        text.setRichText_(False)
        text.setFont_(font)
        text.setTextContainerInset_((6, 8))
        text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text)
        return scroll, text

    @objc.python_method
    def apply_appearance(self):
        def apply_colors():
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
                self.content_view.layer().setBackgroundColor_(
                    NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.985).CGColor()
                )
                self.content_view.layer().setBorderColor_(
                    NSColor.separatorColor().colorWithAlphaComponent_(0.28).CGColor()
                )

        # A CGColor is fixed when made; while AppKit reports an appearance
        # change, the current drawing appearance is still the old one.
        self.content_view.effectiveAppearance().performAsCurrentDrawingAppearance_(apply_colors)

    @objc.python_method
    def _make_label(self, text, font_size: float, bold: bool):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT, 20))
        label.setStringValue_(text)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setAlignment_(NSTextAlignmentCenter)
        label.setFont_(NSFont.boldSystemFontOfSize_(font_size) if bold else NSFont.systemFontOfSize_(font_size))
        return label

    @objc.python_method
    def _make_button(self, title, action):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 80, CONTROL_FRAME_HEIGHT))
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    # -- Layout --

    @staticmethod
    def _place(control, x, row_bottom, width):
        """Put a control's visible edges at x and x+width, centred in a row.
        AppKit draws buttons, popups, and segmented controls inset from
        their frames by different amounts, so frames that line up do not
        look lined up; alignment rectangles do."""
        natural = control.alignmentRectForFrame_(NSMakeRect(0, 0, width, control.frame().size.height))
        aligned = NSMakeRect(x, row_bottom + (ROW - natural.size.height) / 2, width, natural.size.height)
        control.setFrame_(control.frameForAlignmentRect_(aligned))

    @objc.python_method
    def _place_text(self, view, bottom, height):
        view.setFrame_(NSMakeRect(MARGIN, bottom, CONTENT, height))

    @objc.python_method
    def _show_only(self, *views):
        for view in self._all_views:
            view.setHidden_(view not in views)

    @objc.python_method
    def _resize_panel(self, height: int):
        frame = self.panel.frame()
        if frame.size.height == height and frame.size.width == WIDTH:
            return
        # Grow and shrink downwards from the top edge, wherever the user put
        # the window: the status line and the tabs stay where the eye is.
        x = frame.origin.x + (frame.size.width - WIDTH) / 2
        y = frame.origin.y + frame.size.height - height
        screen = self.panel.screen()
        if screen:
            visible = screen.visibleFrame()
            x = max(visible.origin.x, min(x, visible.origin.x + visible.size.width - WIDTH))
            y = max(visible.origin.y, min(y, visible.origin.y + visible.size.height - height))
        self.panel.setFrame_display_animate_(NSMakeRect(x, y, WIDTH, height), True, False)
        self.content_view.setFrame_(NSMakeRect(0, 0, WIDTH, height))

    @objc.python_method
    def _place_tabs_row(self, row_bottom):
        """Tabs at the left, Close at the right; returns where the space
        between them starts."""
        tabs_width = self.mode_control.cell().cellSize().width
        self._place(self.mode_control, MARGIN, row_bottom, tabs_width)
        self._place(self.close_button, WIDTH - MARGIN - CLOSE_WIDTH, row_bottom, CLOSE_WIDTH)
        return MARGIN + tabs_width + BUTTON_GAP

    @objc.python_method
    def _update_layout(self):
        if self.is_transcribing and self._queue_processing:
            self._layout_queue_run()
        elif self.is_transcribing:
            self._layout_transcribing()
        elif self.mode == Mode.QUEUE:
            self._layout_queue()
        else:
            self._layout_transcript()
        self._sync_copy_button()

    @objc.python_method
    def _layout_transcribing(self):
        height = self.TRANSCRIBING_HEIGHT
        self._resize_panel(height)
        self._place_text(self.status_label, height - STATUS_BELOW_TOP, STATUS_HEIGHT)
        self._place(self.close_button, (WIDTH - CANCEL_WIDTH) / 2, BOTTOM, CANCEL_WIDTH)
        self._show_only(self.status_label, self.close_button)

    @objc.python_method
    def _layout_transcript(self):
        show_text = self._shows_text_area()
        show_drop_hint = not self.is_recording and not show_text
        height = (self.EXPANDED_HEIGHT if show_text else
                  self.IDLE_HEIGHT if show_drop_hint else self.RECORDING_HEIGHT)
        self._resize_panel(height)
        status_bottom = height - STATUS_BELOW_TOP
        row_bottom = (status_bottom - STATUS_TO_TABS - ROW if show_text else
                      BOTTOM + 16 + 6 if show_drop_hint else BOTTOM)

        self._place_text(self.status_label, status_bottom, STATUS_HEIGHT)
        self._place_text(self.detail_label, status_bottom - 22, 16)
        left = self._place_tabs_row(row_bottom)
        close_left = WIDTH - MARGIN - CLOSE_WIDTH
        files_left = close_left - BUTTON_GAP - 70
        copy_left = files_left - BUTTON_GAP - 90
        self._place(self.files_button, files_left, row_bottom, 70)
        self._place(self.copy_button, copy_left, row_bottom, 90)
        self._place(self.record_button, left, row_bottom, copy_left - BUTTON_GAP - left)
        self._place_text(self.scroll_view, BOTTOM, row_bottom - GAP - BOTTOM)
        self._place_text(self.drop_label, BOTTOM, 16)
        self.files_button.setEnabled_(not self.is_recording)  # Files start work, which waits for the recording.

        visible = [self.status_label, self.mode_control, *self._transcript_buttons, self.close_button]
        if show_text:
            visible.append(self.scroll_view)
        elif show_drop_hint:
            visible.append(self.drop_label)
        else:
            visible.append(self.detail_label)
        self._show_only(*visible)

    @objc.python_method
    def _layout_queue_run(self):
        """While the queue is being transcribed: the list and a Cancel
        button, because nothing else can be done."""
        height = self.QUEUE_HEIGHT
        self._resize_panel(height)
        status_bottom = height - STATUS_BELOW_TOP
        list_bottom = BOTTOM + ROW + GAP
        self._place_text(self.status_label, status_bottom, STATUS_HEIGHT)
        self._place_text(self.queue_scroll_view, list_bottom, status_bottom - GAP - list_bottom)
        self._place(self.close_button, (WIDTH - CANCEL_WIDTH) / 2, BOTTOM, CANCEL_WIDTH)
        self._show_only(self.status_label, self.queue_scroll_view, self.close_button)

    @objc.python_method
    def _layout_queue(self):
        height = self.QUEUE_HEIGHT
        self._resize_panel(height)
        status_bottom = height - STATUS_BELOW_TOP
        row_bottom = status_bottom - STATUS_TO_TABS - ROW
        list_bottom = BOTTOM + ROW + GAP
        self._place_text(self.status_label, status_bottom, STATUS_HEIGHT)
        self._place_tabs_row(row_bottom)
        self._place_text(self.queue_scroll_view, list_bottom, row_bottom - GAP - list_bottom)
        x = MARGIN
        for button, width in ((self.queue_add_button, 80), (self.queue_up_button, 36),
                              (self.queue_down_button, 36), (self.queue_remove_button, 80),
                              (self.queue_clear_button, 70)):
            self._place(button, x, BOTTOM, width)
            x += width + BUTTON_GAP
        self._place(self.queue_start_button, WIDTH - MARGIN - 106, BOTTOM, 106)
        self._show_only(self.status_label, self.mode_control, self.close_button,
                        self.queue_scroll_view, *self._queue_buttons)
        self._sync_queue_buttons()

    # -- State shown --

    @objc.python_method
    def _has_transcript(self) -> bool:
        return bool(self.current_text.strip())

    @objc.python_method
    def _shows_text_area(self) -> bool:
        if self.mode == Mode.HISTORY:
            return True
        # While recording this shows the live draft as soon as it has text.
        return self._has_transcript() or (bool(self.intro_text) and not self.is_recording)

    @objc.python_method
    def _refresh_text_view(self):
        if self.mode == Mode.QUEUE:
            return
        self.text_view.setString_(self.history_text if self.mode == Mode.HISTORY
                                  else self.current_text or self.intro_text)

    @objc.python_method
    def _sync_copy_button(self):
        enabled = self._has_transcript() and not self.is_recording
        self.copy_button.setEnabled_(enabled)
        self.copy_button.setTitle_("Copied ✓" if self._copy_feedback_visible and enabled else "Copy")

    @objc.python_method
    def _apply_recording_state(self):
        if self.is_recording:
            self.record_button.setTitle_(f"Stop ({STOP.label})")
            self.record_button.setBezelColor_(NSColor.systemRedColor())
        else:
            self.record_button.setTitle_(f"Dictate ({STOP.label})")
            self.record_button.setBezelColor_(NSColor.controlAccentColor())

    @objc.python_method
    def _sync_queue_buttons(self):
        has_items = bool(self._queue_files)
        has_pending = any(i.status in (QueueStatus.PENDING, QueueStatus.CANCELLED) for i in self._queue_files)
        selected = self._selected_queue_index() is not None
        idle = not self._queue_processing
        # Files can be added during a recording; the run waits for it to end.
        self.queue_start_button.setEnabled_(has_pending and idle and not self.is_recording)
        self.queue_clear_button.setEnabled_(has_items and idle)
        for button in (self.queue_remove_button, self.queue_up_button, self.queue_down_button):
            button.setEnabled_(selected and idle)
        self.queue_add_button.setEnabled_(idle)
        self.queue_start_button.setTitle_("Start" if idle else "Running…")
        self.queue_start_button.setBezelColor_(NSColor.systemGreenColor() if idle else None)

    # -- Queue list --

    @objc.python_method
    def _queue_lines(self):
        digits = len(str(len(self._queue_files)))
        lines = []
        for number, queued in enumerate(self._queue_files, start=1):
            status = _QUEUE_STATUS_TEXT[queued.status]
            if queued.status == QueueStatus.FAILED and queued.error:
                status = f"{status}: {queued.error}"  # Why, in full: often what to do about it.
            marker = f"  [{status}]" if status else ""
            prefix = "▶ " if queued.status == QueueStatus.PROCESSING else "  "
            lines.append(f"{prefix}{number:>{digits}}. {queued.filename}{marker}")
        return lines

    @objc.python_method
    def _render_queue_list(self):
        self._rendering_queue = True
        try:
            if not self._queue_files:
                self.queue_text_view.setString_("No files in queue.\n\nDrop files here or click Add… to get started.")
                self._selected_queue_file_id = None
                return
            lines = self._queue_lines()
            self.queue_text_view.setString_("\n".join(lines))
            # The chosen file is shown as a selected line, so Remove and the
            # arrows act on something the user can see.
            index = self._selected_queue_index()
            if index is None:
                self._selected_queue_file_id = None
                self.queue_text_view.setSelectedRange_(NSMakeRange(0, 0))
            else:
                start = sum(_utf16_length(line) + 1 for line in lines[:index])
                self.queue_text_view.setSelectedRange_(NSMakeRange(start, _utf16_length(lines[index])))
        finally:
            self._rendering_queue = False

    @objc.python_method
    def _selected_queue_index(self) -> int | None:
        for index, item in enumerate(self._queue_files):
            if item.id == self._selected_queue_file_id:
                return index
        return None

    def textViewDidChangeSelection_(self, notification):
        """A click in the queue list chooses the file on that line. Dragging
        out a selection (to copy names) or Select All chooses nothing."""
        if self._rendering_queue or notification.object() is not self.queue_text_view or not self._queue_files:
            return
        selection = self.queue_text_view.selectedRange()
        if selection.length:
            return
        # The location is in UTF-16 units, so the text is cut by AppKit, not
        # by Python (where an emoji counts as one character).
        line = str(self.queue_text_view.string().substringToIndex_(selection.location)).count("\n")
        if line < len(self._queue_files):
            self._selected_queue_file_id = self._queue_files[line].id
            self._render_queue_list()
            self._sync_queue_buttons()

    @objc.python_method
    def set_queue_files(self, files):
        self._queue_files = files
        self._render_queue_list()
        count = len(files)
        self.mode_control.setLabel_forSegment_(f"Queue ({count})" if count else "Queue", _SEGMENTS.index(Mode.QUEUE))
        self._update_layout()

    @objc.python_method
    def set_queue_processing(self, processing: bool):
        self._queue_processing = processing
        self._update_layout()

    @objc.python_method
    def show_output_mode_dialog(self) -> Destination | None:
        """Where the queue's transcripts should go, or None if the user cancels."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Where should the transcripts go?")
        alert.setInformativeText_("Each file in the queue is transcribed, then its transcript goes where you choose.")
        alert.addButtonWithTitle_("Start")
        alert.addButtonWithTitle_("Cancel")

        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 300, 26), False)
        popup.addItemWithTitle_("Copy to Clipboard")
        popup.addItemWithTitle_("Save Each Next to Its Original")
        popup.addItemWithTitle_("Save Each in a Folder…")
        popup.addItemWithTitle_("Save All in One File…")
        alert.setAccessoryView_(popup)

        response = alert.runModal()
        if response != 1000:  # NSAlertFirstButtonReturn
            return None

        selected = popup.indexOfSelectedItem()

        if selected == 0:
            return OutputMode.CLIPBOARD

        elif selected == 1:
            return OutputMode.NEXT_TO_ORIGINALS

        elif selected == 2:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("Choose Folder")
            if not panel.runModal():
                return None
            return ToFolder(Path(str(panel.URL().path())))

        else:
            panel = NSSavePanel.savePanel()
            panel.setAllowedFileTypes_(["txt"])
            panel.setNameFieldStringValue_("transcript.txt")
            if not panel.runModal():
                return None
            return ToFile(Path(str(panel.URL().path())))

    @objc.python_method
    def _choose_media_files(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setAllowedFileTypes_(MEDIA_EXTENSIONS)
        return [url.path() for url in panel.URLs()] if panel.runModal() else []

    # -- Showing and hiding --

    @objc.python_method
    def focus(self):
        # Activation must not depend on the app that is in front agreeing to
        # give way: the hotkey can arrive while any app is active.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeMainWindow()
        self.panel.orderFrontRegardless()

    @objc.python_method
    def _cancel_copy_feedback(self):
        self._copy_feedback_token += 1
        self._copy_feedback_visible = False
        self._sync_copy_button()

    @objc.python_method
    def _reset_copy_feedback(self, token: int):
        if token != self._copy_feedback_token:
            return

        self._copy_feedback_visible = False
        self._sync_copy_button()

    @objc.python_method
    def show_mode(self, mode: Mode):
        self._set_mode(mode)
        self.mode_control.setSelectedSegment_(_SEGMENTS.index(mode))
        self._place_on_a_screen()
        self.focus()

    @objc.python_method
    def _place_on_a_screen(self):
        """Centred the first time; afterwards where the user left it, unless
        that is on no screen now (a display was unplugged). AppKit does not
        bring a borderless window back by itself."""
        if not self._positioned or self.panel.screen() is None:
            self.panel.center()
            self._positioned = True

    @objc.python_method
    def _set_mode(self, mode: Mode):
        self.mode = mode
        self._refresh_text_view()
        if mode == Mode.QUEUE:
            self._render_queue_list()
        self._update_layout()

    @objc.python_method
    def prepare_for_recording(self):
        self._show_waiting(True)  # The microphone is not open yet.
        self.detail_label.setStringValue_("")
        self.intro_text = ""
        self.mode = Mode.RESULT
        self.mode_control.setSelectedSegment_(0)
        self.current_text = ""
        self._cancel_copy_feedback()
        self._refresh_text_view()
        self._update_layout()

    def windowBecameKey_(self, notification):
        """Settings, Recordings, the welcome, or a dialog the user turns to
        comes in front of this window; using this window again lifts it."""
        key = notification.object()
        self.panel.setFloatingPanel_(key is self.panel)
        if key is not self.panel and self.panel.isVisible():
            self.panel.orderWindow_relativeTo_(NSWindowBelow, key.windowNumber())

    def appResignedActive_(self, notification):
        """In another app, this window floats above its windows again."""
        del notification
        self.panel.setFloatingPanel_(True)

    @objc.python_method
    def hide(self):
        self.panel.orderOut_(None)
        self.current_text = ""
        self._cancel_copy_feedback()
        self._refresh_text_view()
        self._update_layout()

    @objc.python_method
    def set_status(self, text: str):
        self._status = text
        self.status_label.setStringValue_(text)
        self.status_label.setToolTip_(text)

    @objc.python_method
    def set_transcribing(self, is_transcribing: bool):
        self._show_waiting(False)
        self.is_transcribing = is_transcribing
        self.close_button.setTitle_("Cancel" if is_transcribing else "Close")
        self._update_layout()

    @objc.python_method
    def show_active_microphone(self, name: str):
        self.detail_label.setStringValue_(name)

    @objc.python_method
    def set_capture(self, snapshot):
        self.detail_label.setStringValue_(snapshot.summary())
        self._show_waiting(snapshot.health is not CaptureHealth.RECEIVING)

    @objc.python_method
    def _show_waiting(self, waiting: bool):
        """Orange while what is said would not be recorded, as on the compact bar."""
        self.status_label.setTextColor_(NSColor.systemOrangeColor() if waiting else NSColor.labelColor())

    @objc.python_method
    def set_recording(self, is_recording: bool):
        if is_recording == self.is_recording:
            return
        self.is_recording = is_recording
        if is_recording:
            self._cancel_copy_feedback()
        else:
            self._show_waiting(False)
        self._apply_recording_state()
        self._update_layout()

    @objc.python_method
    def set_current_text(self, text: str):
        self.current_text = text
        if not text.strip():
            self._cancel_copy_feedback()
        self._refresh_text_view()
        self._update_layout()
        if self.is_recording and text and self.mode == Mode.RESULT:
            # Keep the tail of the live draft visible during long dictations.
            self.text_view.scrollRangeToVisible_(NSMakeRange(len(self.text_view.string()), 0))

    @objc.python_method
    def set_intro_text(self, text: str):
        self.intro_text = text
        self._refresh_text_view()
        self._update_layout()

    @objc.python_method
    def set_history_text(self, text: str):
        """The History tab always shows its text area, so only its text changes."""
        self.history_text = text
        if self.mode == Mode.HISTORY:
            self._refresh_text_view()

    @objc.python_method
    def drop_target(self, count: int) -> DropTarget:
        return drop_target(self.mode, count, self.is_transcribing)

    @objc.python_method
    def set_drop_state(self, target: DropTarget | None):
        """What a drop in progress would do, or None when the drag has left."""
        if self.drop_label.isHidden():
            # The hint label is only part of the empty Result layout; elsewhere
            # the status line carries the feedback and gets its text back.
            self.status_label.setStringValue_(self._status if target is None else _DROP_FEEDBACK[target])
            return
        self.drop_label.setStringValue_(DROP_HINT if target is None else f"{_DROP_FEEDBACK[target]}.")
        self.drop_label.setTextColor_(NSColor.secondaryLabelColor() if target is None else NSColor.systemBlueColor())

    @objc.python_method
    def flash_copy_feedback(self):
        if not self._has_transcript():
            return

        self._copy_feedback_token += 1
        self._copy_feedback_visible = True
        self._sync_copy_button()
        call_later(2.0, self._reset_copy_feedback, self._copy_feedback_token)

    @objc.python_method
    def files_dropped(self, paths):
        if self.drop_target(len(paths)) is DropTarget.TRANSCRIBE:
            self.delegate.transcribe_file_directly(paths[0])
        else:
            self.delegate.queue_add_files(paths)

    # -- Actions --

    def toggleMode_(self, sender):
        self._set_mode(_SEGMENTS[sender.selectedSegment()])
        self.focus()

    def toggleRecording_(self, sender):
        del sender
        self.delegate.toggle_recording_requested()

    def copyTranscript_(self, sender):
        del sender
        self.delegate.copy_current_transcript()

    def openFiles_(self, sender):
        del sender
        paths = self._choose_media_files()
        if paths:
            self.files_dropped(paths)

    def closeOverlay_(self, sender):
        del sender
        self.delegate.dismiss_requested()

    def queueAddFiles_(self, sender):
        del sender
        paths = self._choose_media_files()
        if paths:
            self.delegate.queue_add_files(paths)

    def queueMoveUp_(self, sender):
        del sender
        index = self._selected_queue_index()
        if index is not None and index > 0:
            self.delegate.queue_move_file(self._queue_files[index].id, index - 1)

    def queueMoveDown_(self, sender):
        del sender
        index = self._selected_queue_index()
        if index is not None and index < len(self._queue_files) - 1:
            self.delegate.queue_move_file(self._queue_files[index].id, index + 1)

    def queueRemove_(self, sender):
        del sender
        index = self._selected_queue_index()
        if index is not None:
            self.delegate.queue_remove_file(self._queue_files[index].id)

    def queueClear_(self, sender):
        del sender
        self.delegate.queue_clear_requested()

    def queueStart_(self, sender):
        del sender
        self.delegate.queue_start_requested()
