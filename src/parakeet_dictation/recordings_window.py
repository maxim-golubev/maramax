"""The Recordings window: play back, export, or re-transcribe saved dictation audio."""

from __future__ import annotations

import shutil
import threading
from datetime import datetime

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSMakeRect, NSMenuItem, NSPanel,
    NSPopUpButton, NSSavePanel, NSScrollView, NSSound, NSTextField, NSTextView,
    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from .main_thread import call_later
from .recordings import MAX_ARCHIVE_BYTES, MAX_RECORDINGS, RecordingStatus

_STATUS_LABELS = {
    RecordingStatus.DONE: "transcribed",
    RecordingStatus.SAVED: "not transcribed yet",
    RecordingStatus.FAILED: "no transcript",
    RecordingStatus.CANCELLED: "cancelled",
}
WIDTH = 640
HEIGHT = 390
MARGIN = 20
ROW = 32
RETENTION_NOTE = (f"Stored on this Mac · Up to {MAX_RECORDINGS} recordings / "
                  f"{MAX_ARCHIVE_BYTES // (1024 * 1024)} MB; the newest is always kept")


class RecordingsController(NSObject):
    def initWithDelegate_store_(self, delegate, store):
        self = objc.super(RecordingsController, self).init()
        if self is None:
            return None
        self.delegate = delegate
        self.store = store
        self.records = []
        self.sound = None
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.panel.setTitle_("Recordings")
        self.panel.setReleasedWhenClosed_(False)
        # A menu-bar app has no Dock icon to bring a hidden panel back with.
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)
        content = self.panel.contentView()
        inner = WIDTH - 2 * MARGIN
        self.picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, inner, ROW), False)
        self.picker.setTarget_(self)
        self.picker.setAction_("selectionChanged:")
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(MARGIN, 87, inner, 237))
        scroll.setHasVerticalScroller_(True)
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, inner, 237))
        self.text.setEditable_(False)
        self.text.setSelectable_(True)
        self.text.setRichText_(False)
        self.text.setFont_(NSFont.systemFontOfSize_(13))
        self.text.setTextContainerInset_((6, 8))
        self.text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(self.text)
        self.play = self._button("Play", "playAudio:")
        self.save = self._button("Save Audio…", "saveAudio:")
        self.retry = self._button("Transcribe Again", "retry:")
        self.note = NSTextField.labelWithString_(RETENTION_NOTE)
        self.note.setFrame_(NSMakeRect(MARGIN, 13, inner, 18))
        self.note.setFont_(NSFont.systemFontOfSize_(11))
        self.note.setTextColor_(NSColor.secondaryLabelColor())
        # Visible edges, not frames, sit on the margins (see OverlayController._place).
        self._place(self.picker, MARGIN, 335, inner)
        self._place(self.play, MARGIN, 42, 85)
        self._place(self.save, MARGIN + 85 + 8, 42, 115)
        self._place(self.retry, WIDTH - MARGIN - 155, 42, 155)
        for view in (self.picker, scroll, self.play, self.save, self.retry, self.note):
            content.addSubview_(view)
        return self

    @staticmethod
    def _place(control, x, row_bottom, width):
        natural = control.alignmentRectForFrame_(NSMakeRect(0, 0, width, ROW))
        aligned = NSMakeRect(x, row_bottom + (ROW - natural.size.height) / 2, width, natural.size.height)
        control.setFrame_(control.frameForAlignmentRect_(aligned))

    @objc.python_method
    def _button(self, title, action):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 80, ROW))
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.python_method
    def selected(self):
        index = self.picker.indexOfSelectedItem()
        return self.records[index] if 0 <= index < len(self.records) else None

    @objc.python_method
    def refresh(self):
        selected = self.selected()
        self.records = self.store.list_recordings()
        self.picker.removeAllItems()
        for record in self.records:
            try:
                when = datetime.fromisoformat(record.created_at).astimezone().strftime("%b %d, %H:%M:%S")
            except ValueError:
                when = record.created_at
            seconds = int(record.duration)
            title = (f"{when} · {seconds // 60}:{seconds % 60:02d} · "
                     f"{_STATUS_LABELS.get(record.status, record.status)}")
            # NSPopUpButton.addItemWithTitle replaces duplicate titles. Two
            # short captures in the same second must remain separate entries.
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            self.picker.menu().addItem_(item)
        if self.records:
            self.picker.selectItemAtIndex_(0)
        if selected:
            for index, record in enumerate(self.records):
                if record.id == selected.id:
                    self.picker.selectItemAtIndex_(index)
                    break
        if not self.records:
            self.picker.addItemWithTitle_("No saved recordings yet")
        self._show_selected()

    @objc.python_method
    def show_busy_state(self):
        """Playing and transcribing again wait for a dictation or transcription to end."""
        record = self.selected()
        for button in (self.play, self.retry):
            button.setEnabled_(record is not None and not self.delegate.is_busy)

    @objc.python_method
    def _show_selected(self):
        record = self.selected()
        self.show_busy_state()
        self.save.setEnabled_(record is not None)
        if record is None:
            self.text.setString_(
                "Your next dictation will appear here.\n\n"
                "Audio is saved before transcription, even when recognition fails. "
                "You can listen back, save a WAV file, or try transcribing again."
            )
            return
        device = record.diagnostics.get("device_name", "Unknown microphone")
        details = f"Microphone: {device}\nRecorded audio: {record.duration:.1f} seconds"
        if record.message:
            details += f"\n{record.message}"
        transcript = record.text or "No transcript yet. Use Play or Save Audio to inspect the capture."
        if record.raw_text and record.raw_text != record.text:
            transcript += f"\n\nBefore word replacements:\n{record.raw_text}"
        self.text.setString_(details + "\n\n" + transcript)

    @objc.python_method
    def show(self):
        self.refresh()
        if not self.panel.isVisible():
            self.panel.center()  # Only when it appears: it stays where the user moved it.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def stop_playback(self):
        if self.sound is not None:
            self.sound.stop()
            self.sound = None
        self.play.setTitle_("Play")

    def selectionChanged_(self, sender):
        del sender
        self.stop_playback()
        self.note.setStringValue_(RETENTION_NOTE)
        self._show_selected()

    def playAudio_(self, sender):
        del sender
        if self.sound is not None and self.sound.isPlaying():
            self.stop_playback()
            return
        record = self.selected()
        if record is None or self.delegate.is_busy:
            return
        self.sound = NSSound.alloc().initWithContentsOfFile_byReference_(str(self.store.audio_path(record.id)), True)
        if self.sound is None or not self.sound.play():
            self.note.setStringValue_("Could not play recording. Try Save Audio instead.")
            return
        self.play.setTitle_("Stop")
        call_later(0.25, self._check_playback, self.sound)

    @objc.python_method
    def _check_playback(self, sound):
        if sound is not self.sound:
            return
        if not sound.isPlaying():
            self.sound = None
            self.play.setTitle_("Play")
        else:
            call_later(0.25, self._check_playback, sound)

    def saveAudio_(self, sender):
        del sender
        record = self.selected()
        if record is None:
            return
        panel = NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(f"maramax-{record.id[:8]}.wav")
        if not panel.runModal():
            return
        source = self.store.audio_path(record.id)
        destination = str(panel.URL().path())

        def save():
            try:
                shutil.copyfile(source, destination)
                message = "Audio saved."
            except OSError as exc:
                message = f"Could not save audio: {exc}"
            AppHelper.callAfter(self.note.setStringValue_, message)

        threading.Thread(target=save, daemon=True).start()

    def retry_(self, sender):
        del sender
        record = self.selected()
        if record is not None:
            self.stop_playback()
            self.delegate.transcribe_recording(record.id)
            self._show_selected()

    def windowWillClose_(self, notification):
        del notification
        self.stop_playback()
