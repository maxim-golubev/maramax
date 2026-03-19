from __future__ import annotations

import threading
import warnings

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSDragOperationCopy,
    NSEventModifierFlagCommand,
    NSFont,
    NSMakeRect,
    NSOpenPanel,
    NSPanel,
    NSPopUpButton,
    NSSavePanel,
    NSScrollView,
    NSSegmentedControl,
    NSStatusWindowLevel,
    NSTextField,
    NSTextAlignmentCenter,
    NSTextView,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from .queue import OutputConfig, OutputMode

MEDIA_EXTENSIONS = [
    "aac", "aiff", "flac", "m4a", "mov", "mp3", "mp4", "ogg", "opus", "wav", "webm",
]

try:
    from UniformTypeIdentifiers import UTType

    _ALLOWED_CONTENT_TYPES = [UTType.typeWithFilenameExtension_(ext) for ext in MEDIA_EXTENSIONS]
    _ALLOWED_CONTENT_TYPES = [t for t in _ALLOWED_CONTENT_TYPES if t is not None]
    _HAS_UTTYPE = bool(_ALLOWED_CONTENT_TYPES)
except ImportError:
    _HAS_UTTYPE = False
    _ALLOWED_CONTENT_TYPES = []

_COMMAND_ONLY_MASK = (
    NSEventModifierFlagCommand
    | (1 << 17)   # NSEventModifierFlagShift
    | (1 << 18)   # NSEventModifierFlagControl
    | (1 << 19)   # NSEventModifierFlagOption
)

_STATUS_LABELS = {
    "pending": "",
    "processing": "transcribing...",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}


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
        chars = (event.charactersIgnoringModifiers() or "").lower()
        flags = int(event.modifierFlags()) & _COMMAND_ONLY_MASK

        if chars == "\x1b":
            self.controller.handle_escape_key()
            return True

        if flags == NSEventModifierFlagCommand and chars == "r":
            self.controller.handle_toggle_recording_shortcut()
            return True

        if flags == NSEventModifierFlagCommand and chars == "c":
            self.controller.handle_copy_shortcut()
            return True

        return objc.super(OverlayPanel, self).performKeyEquivalent_(event)

    def cancelOperation_(self, sender):
        del sender
        self.controller.handle_escape_key()


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
        self.controller.refresh_appearance()

    def draggingEntered_(self, sender):
        pasteboard = sender.draggingPasteboard()
        urls = pasteboard.readObjectsForClasses_options_(
            [objc.lookUpClass("NSURL")], None,
        ) or []
        for url in urls:
            path = url.path()
            if path and "." in path:
                ext = path.rsplit(".", 1)[-1].lower()
                if ext in MEDIA_EXTENSIONS:
                    self.controller.set_drop_state(True)
                    return NSDragOperationCopy
        return 0

    def draggingExited_(self, sender):
        del sender
        self.controller.set_drop_state(False)

    def prepareForDragOperation_(self, sender):
        del sender
        return True

    def performDragOperation_(self, sender):
        pasteboard = sender.draggingPasteboard()
        urls = pasteboard.readObjectsForClasses_options_([objc.lookUpClass("NSURL")], None) or []
        self.controller.set_drop_state(False)

        paths = []
        for url in urls:
            path = url.path()
            if not path:
                continue
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext in MEDIA_EXTENSIONS:
                paths.append(path)

        if not paths:
            return False

        self.controller.handle_dropped_paths(paths)
        return True


class OverlayController(NSObject):
    WINDOW_WIDTH = 688
    TRANSCRIBING_HEIGHT = 80
    RECORDING_HEIGHT = 128
    IDLE_HEIGHT = 148
    EXPANDED_HEIGHT = 224
    QUEUE_HEIGHT = 310

    def initWithDelegate_config_(self, delegate, config):
        self = objc.super(OverlayController, self).init()
        if self is None:
            return None

        self.delegate = delegate
        self.config = config
        self.mode = "result"
        self.current_text = ""
        self.history_text = ""
        self.is_recording = False
        self.is_transcribing = False
        self._copy_feedback_token = 0
        self._copy_feedback_visible = False
        self._queue_items = []
        self._queue_processing = False
        self._build_window()
        self._refresh_text_view()
        self._sync_copy_button()
        self._update_layout()
        self.set_recording(False)
        self.set_status("Loading speech model\u2026")
        return self

    @objc.python_method
    def _build_window(self):
        style_mask = NSWindowStyleMaskBorderless
        frame = NSMakeRect(0, 0, self.WINDOW_WIDTH, self.IDLE_HEIGHT)
        self.panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_controller_(
            frame,
            style_mask,
            NSBackingStoreBuffered,
            False,
            self,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setWorksWhenModal_(True)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.panel.center()

        self.content_view = OverlayDropView.alloc().initWithFrame_controller_(frame, self)
        self.content_view.layer().setCornerRadius_(18.0)
        self.content_view.layer().setMasksToBounds_(True)
        self.content_view.layer().setBorderWidth_(1.0)
        self._apply_appearance()
        self.panel.setContentView_(self.content_view)

        self.status_label = self._make_label(NSMakeRect(124, 58, 440, 20), "", 13, True)
        self.status_label.setAlignment_(NSTextAlignmentCenter)

        self.mode_control = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(24, 18, 186, 30))
        self.mode_control.setSegmentCount_(3)
        self.mode_control.setLabel_forSegment_("Result", 0)
        self.mode_control.setLabel_forSegment_("History", 1)
        self.mode_control.setLabel_forSegment_("Queue", 2)
        self.mode_control.setSelectedSegment_(0)
        self.mode_control.setTarget_(self)
        self.mode_control.setAction_("toggleMode:")

        self.device_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(24, 52, 640, 26), False
        )
        self.device_popup.setFont_(NSFont.systemFontOfSize_(11))
        self.device_popup.setTarget_(self)
        self.device_popup.setAction_("deviceSelected:")

        self.record_button = self._make_button(NSMakeRect(224, 16, 168, 34), "", "toggleRecording:")
        self.copy_button = self._make_button(NSMakeRect(404, 16, 90, 34), "Copy", "copyTranscript:")
        self.files_button = self._make_button(NSMakeRect(506, 16, 70, 34), "Files\u2026", "openFiles:")
        self.close_button = self._make_button(NSMakeRect(588, 16, 76, 34), "Close", "closeOverlay:")

        self.scroll_view = NSScrollView.alloc().initWithFrame_(NSMakeRect(24, 16, 640, 124))
        self.scroll_view.setHasVerticalScroller_(True)
        self.scroll_view.setBorderType_(0)

        self.text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 124))
        self.text_view.setEditable_(False)
        self.text_view.setSelectable_(True)
        self.text_view.setRichText_(False)
        self.text_view.setFont_(NSFont.systemFontOfSize_(13))
        self.text_view.textContainer().setWidthTracksTextView_(True)
        self.scroll_view.setDocumentView_(self.text_view)

        self.drop_label = self._make_label(
            NSMakeRect(48, 12, 592, 16),
            "Drop audio or video files here, or switch to Queue to batch process.",
            11,
            False,
        )
        self.drop_label.setAlignment_(NSTextAlignmentCenter)
        self.drop_label.setTextColor_(NSColor.secondaryLabelColor())

        # -- Queue UI --
        self.queue_scroll_view = NSScrollView.alloc().initWithFrame_(NSMakeRect(24, 52, 640, 160))
        self.queue_scroll_view.setHasVerticalScroller_(True)
        self.queue_scroll_view.setBorderType_(0)

        self.queue_text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 160))
        self.queue_text_view.setEditable_(False)
        self.queue_text_view.setSelectable_(True)
        self.queue_text_view.setRichText_(False)
        self.queue_text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        self.queue_text_view.textContainer().setWidthTracksTextView_(True)
        self.queue_scroll_view.setDocumentView_(self.queue_text_view)

        self.queue_add_button = self._make_button(NSMakeRect(24, 16, 80, 34), "Add\u2026", "queueAddFiles:")
        self.queue_up_button = self._make_button(NSMakeRect(116, 16, 34, 34), "\u25B2", "queueMoveUp:")
        self.queue_down_button = self._make_button(NSMakeRect(158, 16, 34, 34), "\u25BC", "queueMoveDown:")
        self.queue_remove_button = self._make_button(NSMakeRect(204, 16, 80, 34), "Remove", "queueRemove:")
        self.queue_clear_button = self._make_button(NSMakeRect(296, 16, 70, 34), "Clear", "queueClear:")
        self.queue_start_button = self._make_button(NSMakeRect(558, 16, 106, 34), "Start", "queueStart:")
        self._set_button_tint(self.queue_start_button, NSColor.systemGreenColor())

        # Initially hide queue views
        self.queue_scroll_view.setHidden_(True)
        self.queue_add_button.setHidden_(True)
        self.queue_up_button.setHidden_(True)
        self.queue_down_button.setHidden_(True)
        self.queue_remove_button.setHidden_(True)
        self.queue_clear_button.setHidden_(True)
        self.queue_start_button.setHidden_(True)

        for view in [
            self.status_label,
            self.device_popup,
            self.mode_control,
            self.record_button,
            self.copy_button,
            self.files_button,
            self.close_button,
            self.scroll_view,
            self.drop_label,
            self.queue_scroll_view,
            self.queue_add_button,
            self.queue_up_button,
            self.queue_down_button,
            self.queue_remove_button,
            self.queue_clear_button,
            self.queue_start_button,
        ]:
            self.content_view.addSubview_(view)

    @objc.python_method
    def _apply_appearance(self):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
            self.content_view.layer().setBackgroundColor_(
                NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.985).CGColor()
            )
            self.content_view.layer().setBorderColor_(
                NSColor.separatorColor().colorWithAlphaComponent_(0.28).CGColor()
            )

    @objc.python_method
    def refresh_appearance(self):
        self._apply_appearance()

    @objc.python_method
    def _make_label(self, frame, text, font_size: float, bold: bool):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setStringValue_(text)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(font_size) if bold else NSFont.systemFontOfSize_(font_size))
        return label

    @objc.python_method
    def _make_button(self, frame, title, action):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.python_method
    def _has_transcript(self) -> bool:
        return bool(self.current_text.strip())

    @objc.python_method
    def _should_show_text_area(self) -> bool:
        if self.is_recording:
            return False
        if self.mode == "history":
            return True
        if self.mode == "queue":
            return False
        return self._has_transcript()

    @objc.python_method
    def _should_show_drop_hint(self) -> bool:
        if self.mode == "queue":
            return False
        return not self.is_recording and not self._should_show_text_area()

    @objc.python_method
    def _refresh_text_view(self):
        if self.mode == "queue":
            return
        value = self.current_text if self.mode == "result" else self.history_text
        self.text_view.setString_(value)

    @objc.python_method
    def _set_button_tint(self, button, color):
        if hasattr(button, "setContentTintColor_"):
            button.setContentTintColor_(color)

    @objc.python_method
    def _sync_copy_button(self):
        enabled = self._has_transcript() and not self.is_recording
        self.copy_button.setEnabled_(enabled)
        if self._copy_feedback_visible and enabled:
            self.copy_button.setTitle_("Copied \u2713")
        else:
            self.copy_button.setTitle_("Copy")

    @objc.python_method
    def _resize_panel(self, height: int):
        frame = self.panel.frame()
        center_x = frame.origin.x + (frame.size.width / 2)
        center_y = frame.origin.y + (frame.size.height / 2)
        x = center_x - (self.WINDOW_WIDTH / 2)
        y = center_y - (height / 2)

        screen = self.panel.screen()
        if screen:
            visible = screen.visibleFrame()
            x = max(visible.origin.x, min(x, visible.origin.x + visible.size.width - self.WINDOW_WIDTH))
            y = max(visible.origin.y, min(y, visible.origin.y + visible.size.height - height))

        new_frame = NSMakeRect(x, y, self.WINDOW_WIDTH, height)
        self.panel.setFrame_display_animate_(new_frame, True, False)
        self.content_view.setFrame_(NSMakeRect(0, 0, self.WINDOW_WIDTH, height))

    @objc.python_method
    def _hide_all_controls(self):
        for v in [
            self.device_popup, self.mode_control, self.record_button,
            self.copy_button, self.files_button, self.close_button,
            self.scroll_view, self.drop_label,
            self.queue_scroll_view, self.queue_add_button, self.queue_up_button,
            self.queue_down_button, self.queue_remove_button, self.queue_clear_button,
            self.queue_start_button,
        ]:
            v.setHidden_(True)

    @objc.python_method
    def _update_layout(self):
        if self.is_transcribing:
            if self._queue_processing:
                self._layout_queue_processing()
            else:
                self._resize_panel(self.TRANSCRIBING_HEIGHT)
                self.status_label.setFrame_(NSMakeRect(24, 42, 640, 20))
                self._hide_all_controls()
                self.close_button.setFrame_(NSMakeRect(290, 8, 108, 34))
                self.status_label.setHidden_(False)
                self.close_button.setHidden_(False)
            return

        if self.mode == "queue":
            self._layout_queue()
            return

        show_text_area = self._should_show_text_area()
        show_drop_hint = self._should_show_drop_hint()
        show_device = not show_text_area

        if show_text_area:
            height = self.EXPANDED_HEIGHT
        elif show_drop_hint:
            height = self.IDLE_HEIGHT
        else:
            height = self.RECORDING_HEIGHT
        self._resize_panel(height)

        if show_text_area:
            controls_y = height - 74
            status_y = height - 40
        elif show_drop_hint:
            controls_y = 38
            status_y = height - 40
        else:
            controls_y = 16
            status_y = height - 40

        device_y = controls_y + 36

        self.status_label.setFrame_(NSMakeRect(124, status_y, 440, 20))
        self.device_popup.setFrame_(NSMakeRect(24, device_y, 640, 26))
        self.device_popup.setHidden_(not show_device)
        self.mode_control.setFrame_(NSMakeRect(24, controls_y + 2, 186, 30))
        self.mode_control.setHidden_(False)
        self.record_button.setFrame_(NSMakeRect(224, controls_y, 168, 34))
        self.record_button.setHidden_(False)
        self.copy_button.setFrame_(NSMakeRect(404, controls_y, 90, 34))
        self.copy_button.setHidden_(False)
        self.files_button.setFrame_(NSMakeRect(506, controls_y, 70, 34))
        self.files_button.setHidden_(False)
        self.close_button.setFrame_(NSMakeRect(588, controls_y, 76, 34))
        self.close_button.setHidden_(False)
        self.status_label.setHidden_(False)

        # Hide queue controls
        self.queue_scroll_view.setHidden_(True)
        self.queue_add_button.setHidden_(True)
        self.queue_up_button.setHidden_(True)
        self.queue_down_button.setHidden_(True)
        self.queue_remove_button.setHidden_(True)
        self.queue_clear_button.setHidden_(True)
        self.queue_start_button.setHidden_(True)

        if show_text_area:
            self.scroll_view.setFrame_(NSMakeRect(24, 16, 640, controls_y - 26))
            self.scroll_view.setHidden_(False)
            self.drop_label.setHidden_(True)
        else:
            self.scroll_view.setHidden_(True)
            self.drop_label.setHidden_(not show_drop_hint)
            self.drop_label.setFrame_(NSMakeRect(48, 14, 592, 16))

        self._sync_copy_button()

    @objc.python_method
    def _layout_queue_processing(self):
        height = self.QUEUE_HEIGHT
        self._resize_panel(height)

        status_y = height - 40
        list_bottom = 56
        list_height = height - 40 - 34 - list_bottom

        self.status_label.setFrame_(NSMakeRect(24, status_y, 640, 20))
        self.status_label.setHidden_(False)

        self._hide_all_controls()

        # Show queue list and cancel button
        self.queue_scroll_view.setFrame_(NSMakeRect(24, list_bottom, 640, list_height))
        self.queue_scroll_view.setHidden_(False)
        self.close_button.setFrame_(NSMakeRect(290, 14, 108, 34))
        self.close_button.setHidden_(False)

    @objc.python_method
    def _layout_queue(self):
        height = self.QUEUE_HEIGHT
        self._resize_panel(height)

        status_y = height - 40
        controls_y = height - 74
        list_bottom = 56
        list_height = controls_y - 26 - list_bottom

        self.status_label.setFrame_(NSMakeRect(124, status_y, 440, 20))
        self.status_label.setHidden_(False)
        self.mode_control.setFrame_(NSMakeRect(24, controls_y + 2, 186, 30))
        self.mode_control.setHidden_(False)
        self.close_button.setFrame_(NSMakeRect(588, controls_y, 76, 34))
        self.close_button.setHidden_(False)

        # Hide non-queue controls
        self.device_popup.setHidden_(True)
        self.record_button.setHidden_(True)
        self.copy_button.setHidden_(True)
        self.files_button.setHidden_(True)
        self.scroll_view.setHidden_(True)
        self.drop_label.setHidden_(True)

        # Queue list
        self.queue_scroll_view.setFrame_(NSMakeRect(24, list_bottom, 640, list_height))
        self.queue_scroll_view.setHidden_(False)

        # Bottom button row
        btn_y = 14
        self.queue_add_button.setFrame_(NSMakeRect(24, btn_y, 80, 34))
        self.queue_up_button.setFrame_(NSMakeRect(116, btn_y, 34, 34))
        self.queue_down_button.setFrame_(NSMakeRect(158, btn_y, 34, 34))
        self.queue_remove_button.setFrame_(NSMakeRect(204, btn_y, 80, 34))
        self.queue_clear_button.setFrame_(NSMakeRect(296, btn_y, 70, 34))
        self.queue_start_button.setFrame_(NSMakeRect(558, btn_y, 106, 34))

        self.queue_add_button.setHidden_(False)
        self.queue_up_button.setHidden_(False)
        self.queue_down_button.setHidden_(False)
        self.queue_remove_button.setHidden_(False)
        self.queue_clear_button.setHidden_(False)
        self.queue_start_button.setHidden_(False)

        self._sync_queue_buttons()

    @objc.python_method
    def _sync_queue_buttons(self):
        has_items = bool(self._queue_items)
        has_pending = any(i.status == "pending" for i in self._queue_items)
        processing = self._queue_processing

        self.queue_start_button.setEnabled_(has_pending and not processing)
        self.queue_clear_button.setEnabled_(has_items and not processing)
        self.queue_remove_button.setEnabled_(has_items and not processing)
        self.queue_up_button.setEnabled_(has_items and not processing)
        self.queue_down_button.setEnabled_(has_items and not processing)
        self.queue_add_button.setEnabled_(not processing)

        if processing:
            self.queue_start_button.setTitle_("Running\u2026")
            self._set_button_tint(self.queue_start_button, NSColor.secondaryLabelColor())
        else:
            self.queue_start_button.setTitle_("Start")
            self._set_button_tint(self.queue_start_button, NSColor.systemGreenColor())

    @objc.python_method
    def _render_queue_list(self):
        if not self._queue_items:
            self.queue_text_view.setString_("No files in queue.\n\nDrop files here or click Add\u2026 to get started.")
            return

        lines = []
        for i, item in enumerate(self._queue_items):
            status = _STATUS_LABELS.get(item.status, item.status)
            marker = f"  [{status}]" if status else ""
            prefix = "\u25B6 " if item.status == "processing" else "  "
            lines.append(f"{prefix}{i + 1}. {item.filename}{marker}")

        self.queue_text_view.setString_("\n".join(lines))

    @objc.python_method
    def _get_selected_queue_index(self) -> int | None:
        if not self._queue_items:
            return None

        sel = self.queue_text_view.selectedRange()
        if sel.length == 0 and sel.location == 0:
            return None

        text = self.queue_text_view.string()
        if not text:
            return None

        # Find which line the cursor is on
        pos = min(sel.location, len(text) - 1) if text else 0
        line_num = text[:pos + 1].count("\n")
        if line_num < len(self._queue_items):
            return line_num
        return None

    @objc.python_method
    def _update_queue_tab_label(self):
        count = len(self._queue_items)
        label = f"Queue ({count})" if count > 0 else "Queue"
        self.mode_control.setLabel_forSegment_(label, 2)

    @objc.python_method
    def set_queue_items(self, items):
        self._queue_items = items
        self._render_queue_list()
        self._update_queue_tab_label()
        if self.mode == "queue":
            self._sync_queue_buttons()

    @objc.python_method
    def set_queue_processing(self, processing: bool):
        self._queue_processing = processing
        if self.mode == "queue":
            self._sync_queue_buttons()

    @objc.python_method
    def show_output_mode_dialog(self):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Choose Output")
        alert.setInformativeText_("Where should the transcription results be saved?")
        alert.addButtonWithTitle_("Start")
        alert.addButtonWithTitle_("Cancel")

        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 300, 26), False)
        popup.addItemWithTitle_("Copy to Clipboard")
        popup.addItemWithTitle_("Save as Individual Files (same directory)")
        popup.addItemWithTitle_("Save as Individual Files (choose directory)")
        popup.addItemWithTitle_("Save as Single File")
        alert.setAccessoryView_(popup)

        response = alert.runModal()
        if response != 1000:  # NSAlertFirstButtonReturn
            return None

        selected = popup.indexOfSelectedItem()

        if selected == 0:
            return OutputConfig(mode=OutputMode.CLIPBOARD)

        elif selected == 1:
            return OutputConfig(mode=OutputMode.INDIVIDUAL_SAME_DIR)

        elif selected == 2:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("Choose Folder")
            if not panel.runModal():
                return None
            return OutputConfig(
                mode=OutputMode.INDIVIDUAL_CHOSEN_DIR,
                output_path=str(panel.URL().path()),
            )

        else:
            panel = NSSavePanel.savePanel()
            if _HAS_UTTYPE:
                txt_type = UTType.typeWithFilenameExtension_("txt")
                if txt_type:
                    panel.setAllowedContentTypes_([txt_type])
            else:
                panel.setAllowedFileTypes_(["txt"])
            panel.setNameFieldStringValue_("transcript.txt")
            if not panel.runModal():
                return None
            return OutputConfig(
                mode=OutputMode.SINGLE_FILE,
                output_path=str(panel.URL().path()),
            )

    @objc.python_method
    def _focus_panel(self):
        app = NSApplication.sharedApplication()
        if hasattr(app, "activate"):
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)
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
    def show_mode(self, mode: str):
        self.mode = mode
        segment = {"result": 0, "history": 1, "queue": 2}.get(mode, 0)
        self.mode_control.setSelectedSegment_(segment)
        self._refresh_text_view()
        if mode == "queue":
            self._render_queue_list()
        self._update_layout()
        self.panel.center()
        self._focus_panel()

    @objc.python_method
    def focus(self):
        self._focus_panel()

    @objc.python_method
    def prepare_for_recording(self):
        self.mode = "result"
        self.mode_control.setSelectedSegment_(0)
        self.current_text = ""
        self._cancel_copy_feedback()
        self._refresh_text_view()
        self._update_layout()

    @objc.python_method
    def hide(self):
        self.panel.orderOut_(None)
        self.current_text = ""
        self.text_view.setString_("")
        self._cancel_copy_feedback()
        self.status_label.setStringValue_("")
        self._update_layout()

    @objc.python_method
    def set_status(self, text: str):
        self.status_label.setStringValue_(text)

    @objc.python_method
    def set_transcribing(self, is_transcribing: bool):
        self.is_transcribing = is_transcribing
        if is_transcribing:
            self.close_button.setTitle_("Cancel")
        else:
            self.close_button.setTitle_("Close")
        self._update_layout()

    @objc.python_method
    def update_input_devices(self, devices, selected_name: str | None):
        self.device_popup.removeAllItems()

        if not devices:
            self.device_popup.addItemWithTitle_("No input devices found")
            self.device_popup.setEnabled_(False)
            return

        self.device_popup.setEnabled_(not self.is_recording)

        for d in devices:
            self.device_popup.addItemWithTitle_(d.name)

        if selected_name is not None:
            idx = self.device_popup.indexOfItemWithTitle_(selected_name)
            if idx >= 0:
                self.device_popup.selectItemAtIndex_(idx)
                return

        # No explicit selection -- pick the system default
        for i, d in enumerate(devices):
            if d.is_default:
                self.device_popup.selectItemAtIndex_(i)
                return

        self.device_popup.selectItemAtIndex_(0)

    @objc.python_method
    def set_recording(self, is_recording: bool):
        self.is_recording = is_recording
        self.device_popup.setEnabled_(not is_recording)
        if is_recording:
            self._cancel_copy_feedback()
            self.record_button.setTitle_(f"Stop ({self.config.shortcuts.toggle_recording})")
            self._set_button_tint(self.record_button, NSColor.systemRedColor())
        else:
            self.record_button.setTitle_(f"Record ({self.config.shortcuts.toggle_recording})")
            self._set_button_tint(self.record_button, NSColor.systemBlueColor())
        self._update_layout()

    @objc.python_method
    def set_current_text(self, text: str):
        self.current_text = text
        if not text.strip():
            self._cancel_copy_feedback()
        self._refresh_text_view()
        self._update_layout()

    @objc.python_method
    def set_history_text(self, text: str):
        self.history_text = text
        self._refresh_text_view()
        self._update_layout()

    @objc.python_method
    def set_drop_state(self, active: bool):
        if active:
            self.drop_label.setStringValue_("Drop to add to queue.")
            self.drop_label.setTextColor_(NSColor.systemBlueColor())
        else:
            self.drop_label.setStringValue_(
                "Drop audio or video files here, or switch to Queue to batch process."
            )
            self.drop_label.setTextColor_(NSColor.secondaryLabelColor())

    @objc.python_method
    def flash_copy_feedback(self):
        if not self._has_transcript():
            return

        self._copy_feedback_token += 1
        token = self._copy_feedback_token
        self._copy_feedback_visible = True
        self._sync_copy_button()
        timer = threading.Timer(2.0, lambda: AppHelper.callAfter(self._reset_copy_feedback, token))
        timer.daemon = True
        timer.start()

    @objc.python_method
    def handle_dropped_paths(self, paths):
        self.delegate.queue_add_files(paths)

    @objc.python_method
    def handle_escape_key(self):
        self.delegate.hide_overlay()

    @objc.python_method
    def handle_toggle_recording_shortcut(self):
        self.delegate.toggle_recording_requested()

    @objc.python_method
    def handle_copy_shortcut(self):
        self.delegate.copy_current_transcript()

    def toggleMode_(self, sender):
        segment = sender.selectedSegment()
        self.mode = {0: "result", 1: "history", 2: "queue"}.get(segment, "result")
        self._refresh_text_view()
        if self.mode == "queue":
            self._render_queue_list()
        self._update_layout()
        self._focus_panel()

    def deviceSelected_(self, sender):
        self.delegate.handle_device_selected(sender.titleOfSelectedItem())

    def toggleRecording_(self, sender):
        del sender
        self.delegate.toggle_recording_requested()

    def copyTranscript_(self, sender):
        del sender
        self.delegate.copy_current_transcript()

    def openFiles_(self, sender):
        del sender
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)
        panel.setAllowsMultipleSelection_(True)
        if _HAS_UTTYPE:
            panel.setAllowedContentTypes_(_ALLOWED_CONTENT_TYPES)
        else:
            panel.setAllowedFileTypes_(MEDIA_EXTENSIONS)
        if panel.runModal():
            paths = [url.path() for url in panel.URLs()]
            self.delegate.queue_add_files(paths)

    def closeOverlay_(self, sender):
        del sender
        self.delegate.hide_overlay()

    def queueAddFiles_(self, sender):
        del sender
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)
        panel.setAllowsMultipleSelection_(True)
        if _HAS_UTTYPE:
            panel.setAllowedContentTypes_(_ALLOWED_CONTENT_TYPES)
        else:
            panel.setAllowedFileTypes_(MEDIA_EXTENSIONS)
        if panel.runModal():
            paths = [url.path() for url in panel.URLs()]
            self.delegate.queue_add_files(paths)

    def queueMoveUp_(self, sender):
        del sender
        idx = self._get_selected_queue_index()
        if idx is not None and idx > 0:
            item = self._queue_items[idx]
            self.delegate.queue_move_item(item.id, idx - 1)

    def queueMoveDown_(self, sender):
        del sender
        idx = self._get_selected_queue_index()
        if idx is not None and idx < len(self._queue_items) - 1:
            item = self._queue_items[idx]
            self.delegate.queue_move_item(item.id, idx + 1)

    def queueRemove_(self, sender):
        del sender
        idx = self._get_selected_queue_index()
        if idx is not None:
            item = self._queue_items[idx]
            self.delegate.queue_remove_item(item.id)

    def queueClear_(self, sender):
        del sender
        self.delegate.queue_clear_requested()

    def queueStart_(self, sender):
        del sender
        self.delegate.queue_start_requested()
