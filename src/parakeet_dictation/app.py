from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import rumps
from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace
from PyObjCTools import AppHelper

from .autopaste import PasteError, accessibility_trusted, send_paste_keystroke
from .clipboard import ClipboardError, copy_text
from .config import AppConfig
from .export import ExportError, export_results
from .history import HistoryStore
from .hotkeys import GlobalHotKeyManager, HotKeyError
from .logger_config import setup_logging
from .overlay import OverlayController
from .paths import app_support_dir, resource_path
from .queue import TranscriptionQueue
from .transcription import AudioRecorder, ParakeetTranscriber, QwenTranscriber, TranscriptionError

_SETTING_LABELS = {
    "auto_start_recording": "Record Immediately on Option+Space",
    "auto_copy_to_clipboard": "Auto-Copy Result",
    "paste_to_active_app": "Paste Into Active App",
    "live_preview": "Live Preview While Speaking",
    "high_accuracy": "High-Accuracy Model (more RAM)",
}

logger = setup_logging()


class DictationApp(rumps.App):
    def __init__(self, config: AppConfig | None = None):
        self.status_icon_path = str(resource_path("assets", "menu_icon.png"))
        super().__init__(
            "Maramax",
            title=None,
            icon=self.status_icon_path,
            template=True,
            quit_button=None,
        )
        self._settings_path = app_support_dir() / "settings.json"
        self.config = config or AppConfig.load(self._settings_path)
        self.transcriber = ParakeetTranscriber()
        self.qwen = QwenTranscriber()
        self.recorder = AudioRecorder()
        self.history_store = HistoryStore(history_limit=self.config.history_limit)
        self.queue = TranscriptionQueue()
        self.current_transcript = ""
        self.recording_active = False
        self.is_transcribing = False
        self.overlay_visible = False
        self._overlay_session = 0
        self._hide_after_transcription = False
        self._force_copy_after_transcription = False
        self._cancel_event = threading.Event()
        self._queue_cancel_event = threading.Event()
        self._status_token = 0
        self._base_status = "Loading speech model\u2026"
        self._state_lock = threading.Lock()
        self.hotkey_manager: GlobalHotKeyManager | None = None
        self._hotkey_error_message: str | None = None
        self._previous_app = None
        self._live_thread: threading.Thread | None = None
        self._live_stop_event = threading.Event()

        self._settings_items = {
            name: rumps.MenuItem(label, callback=self._on_setting_toggled)
            for name, label in _SETTING_LABELS.items()
        }
        for name, item in self._settings_items.items():
            item.state = 1 if getattr(self.config, name) else 0

        self.status_item = rumps.MenuItem("Status: Loading speech model\u2026")
        self.menu = [
            rumps.MenuItem("Show Overlay"),
            rumps.MenuItem("Show History"),
            rumps.MenuItem("Toggle Recording"),
            rumps.MenuItem("Copy Last Transcript"),
            rumps.MenuItem("Open Media Files\u2026"),
            rumps.MenuItem("Clear History"),
            ("Settings", list(self._settings_items.values())),
            None,
            self.status_item,
            rumps.MenuItem("Quit"),
        ]

        self.overlay_controller = OverlayController.alloc().initWithDelegate_config_(self, self.config)
        self.overlay_controller.set_history_text(self.history_store.render())
        self.overlay_controller.set_current_text(
            "Use Option+Space to open the overlay.\n\n"
            "If auto-start is enabled, recording begins immediately; "
            "press Option+Space again (or Cmd+R) to stop."
        )

        self._start_model_watchdog()
        self._register_global_hotkeys()
        self._warmup_thread = threading.Thread(target=self.recorder.warm_up, daemon=True)
        self._warmup_thread.start()

    def _start_model_watchdog(self) -> None:
        threading.Thread(target=self._wait_for_model_readiness, daemon=True).start()

    def _wait_for_model_readiness(self) -> None:
        try:
            self.transcriber.wait_until_ready()
        except TranscriptionError as exc:
            logger.error(str(exc))
            self._push_status("Model failed to load", recording=False)
            return
        finally:
            # Stagger the heavy Qwen load until Parakeet is up so dictation
            # is usable seconds after launch and the loads don't contend.
            if self.config.high_accuracy:
                self.qwen.start_loading()

        if self._hotkey_error_message:
            self._push_status(self._hotkey_error_message, recording=False)
            return

        self._push_status("Ready", recording=False)

    def _register_global_hotkeys(self) -> None:
        try:
            self.hotkey_manager = GlobalHotKeyManager(self.handle_overlay_hotkey)
            self.hotkey_manager.register_default_overlay_shortcut()
            logger.info("Registered global shortcut: Option+Space")
        except HotKeyError as exc:
            logger.error(f"Global hotkey registration failed: {exc}")
            self._hotkey_error_message = "Option+Space unavailable. Check macOS shortcut conflicts."
            self._push_status(self._hotkey_error_message)

    def handle_overlay_hotkey(self) -> None:
        if self.recording_active:
            # Option+Space acts as a toggle: press again to finish dictating.
            self.stop_recording_requested(auto_copy=True, hide_after=True)
            return
        if self.overlay_visible:
            AppHelper.callAfter(self.overlay_controller.focus)
            return
        if self.config.auto_start_recording:
            AppHelper.callAfter(self._show_overlay_and_start_on_main)
        else:
            self.show_overlay()

    def show_overlay(self) -> None:
        # Keep the last transcript visible/copyable; only a new recording
        # clears it (start_recording).
        if not (self.recording_active or self.is_transcribing):
            self._reset_deferred_flags()
        self._show_overlay_on_main("result")

    def show_history_overlay(self) -> None:
        self._show_overlay_on_main("history")

    def _capture_previous_app(self) -> None:
        front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front_app is not None and front_app.processIdentifier() != os.getpid():
            self._previous_app = front_app

    def _show_overlay_on_main(self, mode: str) -> None:
        self._capture_previous_app()
        # Only invalidate worker sessions when no operation owns the display;
        # bumping mid-recording/transcription would silently discard live
        # drafts and the final result.
        if not (self.recording_active or self.is_transcribing):
            self._overlay_session += 1
        else:
            # The user explicitly re-opened the overlay — don't hide it when
            # the in-flight operation finishes.
            with self._state_lock:
                self._hide_after_transcription = False
        self.overlay_visible = True
        self._refresh_input_devices()
        self.overlay_controller.show_mode(mode)

    def _show_overlay_and_start_on_main(self) -> None:
        # A queued duplicate (rapid double Option+Space) must not bump the
        # session out from under the recording the first press started.
        if self.recording_active or self.is_transcribing:
            self.overlay_controller.focus()
            return
        self.current_transcript = ""
        self._reset_deferred_flags()
        self._capture_previous_app()
        self._overlay_session += 1
        self.overlay_visible = True
        # Mic first: the user starts speaking the moment they hit the hotkey,
        # so capture must begin before any window work.
        started = self.start_recording()
        self.overlay_controller.set_current_text("")
        self.overlay_controller.set_recording(started)
        self.overlay_controller.show_mode("result")
        self._refresh_input_devices()

    def hide_overlay(self) -> None:
        action = None
        with self._state_lock:
            if self.recording_active:
                action = "stop_recording"
            elif self.is_transcribing:
                self._cancel_event.set()
                self._queue_cancel_event.set()
                self._hide_after_transcription = True
                return
            else:
                session = self._overlay_session
                self.overlay_visible = False
                action = "hide"

        if action == "stop_recording":
            self.stop_recording_requested(auto_copy=True, hide_after=True)
        elif action == "hide":
            AppHelper.callAfter(self._hide_overlay_on_main, session)

    def _hide_overlay_on_main(self, session: int | None = None) -> None:
        if session is not None and session != self._overlay_session:
            return
        self.overlay_visible = False
        self.overlay_controller.hide()

    def toggle_recording_requested(self) -> None:
        if self.recording_active:
            self.stop_recording_requested()
        else:
            self.start_recording()

    def start_recording(self) -> bool:
        if not self.transcriber.is_ready():
            self._push_status("Model is still loading, please wait", recording=False)
            return False

        if self.is_transcribing:
            self._push_status("Wait for the current transcription to finish", recording=False)
            return False

        if self.recording_active:
            return True

        if not self.recorder.start():
            error = self.recorder.last_error
            message = f"Microphone unavailable: {error}" if error else "Could not start recording"
            self._push_status(message, recording=False)
            return False

        self._reset_deferred_flags()
        self.current_transcript = ""
        self.overlay_controller.set_current_text("")
        if not self.overlay_visible:
            self.show_overlay()
        else:
            AppHelper.callAfter(self.overlay_controller.prepare_for_recording)
        self.recording_active = True
        # Truthful feedback: "Recording\u2026" only once audio actually flows.
        self._push_status("Starting mic\u2026", recording=True)
        threading.Thread(
            target=self._announce_recording_when_live,
            args=(
                self._overlay_session,
                self.recorder.start_generation,
                self.recorder.signal_event,
                self.recorder.first_frame_event,
            ),
            daemon=True,
        ).start()
        if self.config.live_preview:
            self._start_live_preview()
        return True

    def _announce_recording_when_live(
        self,
        session: int,
        generation: int,
        signal_event: threading.Event,
        first_frame_event: threading.Event,
    ) -> None:
        # Wait for actual signal, not just frames: Bluetooth mics deliver
        # silent frames for 1-2s while switching into headset mode. The
        # events and generation are captured at start so a stale announcer
        # can never vouch for a *different* recording's microphone.
        if not signal_event.wait(timeout=5.0):
            if not first_frame_event.is_set():
                return
        if (
            self.recording_active
            and session == self._overlay_session
            and generation == self.recorder.start_generation
        ):
            self._push_status("Recording\u2026", recording=True)

    def stop_recording_requested(self, auto_copy: bool | None = None, hide_after: bool = False) -> None:
        if not self.recording_active:
            return

        with self._state_lock:
            if hide_after:
                self._hide_after_transcription = True
            if auto_copy:
                self._force_copy_after_transcription = True

        session = self._overlay_session
        self.recording_active = False
        self.is_transcribing = True
        self._cancel_event.clear()
        self._live_stop_event.set()
        pcm_bytes = self.recorder.stop()
        self._push_status("Transcribing\u2026", recording=False)
        AppHelper.callAfter(self.overlay_controller.set_transcribing, True)
        threading.Thread(
            target=self._transcribe_recording_worker,
            args=(pcm_bytes, auto_copy, session),
            daemon=True,
        ).start()

    def _check_cancel(self, current_pos, total_pos) -> None:
        del current_pos, total_pos
        if self._cancel_event.is_set():
            raise TranscriptionError("Cancelled")

    # -- Final-pass engine routing --
    #
    # Qwen3-ASR (when enabled and loaded) handles final passes for best
    # accuracy; Parakeet covers live drafts, the loading window, and any
    # Qwen failure. Qwen has no progress callback, so cancellation is
    # checked before inference; a completed result is always published.

    def _final_transcribe_pcm(self, pcm_bytes: bytes) -> str:
        if self.config.high_accuracy and self.qwen.is_ready() and not self._cancel_event.is_set():
            try:
                return self.qwen.transcribe_pcm(
                    pcm_bytes,
                    channels=self.recorder.channels,
                    sample_width=self.recorder.sample_width(),
                    rate=self.recorder.rate,
                )
            except Exception as exc:
                logger.error(f"High-accuracy transcription failed, falling back: {exc}")
        return self.transcriber.transcribe_pcm(
            pcm_bytes,
            channels=self.recorder.channels,
            sample_width=self.recorder.sample_width(),
            rate=self.recorder.rate,
            progress_callback=self._check_cancel,
        )

    def _final_transcribe_file(self, path: str, progress_callback, cancel_event: threading.Event) -> str:
        if self.config.high_accuracy and self.qwen.is_ready() and not cancel_event.is_set():
            try:
                return self.qwen.transcribe_file(path)
            except Exception as exc:
                logger.error(f"High-accuracy transcription failed, falling back: {exc}")
        return self.transcriber.transcribe_file(path, progress_callback=progress_callback)

    def _transcribe_recording_worker(self, pcm_bytes: bytes, auto_copy: bool | None, session: int) -> None:
        try:
            # The live preview stream must finish before the offline pass: it
            # holds the shared encoder in streaming (local attention) mode.
            if not self._finish_live_preview():
                raise TranscriptionError(
                    "Transcription engine stalled — please try again"
                )
            text = self._final_transcribe_pcm(pcm_bytes)
            if not text:
                # Clear any leftover live draft: it is display-only and must
                # not outlive a final pass that found no speech.
                self._set_current_text_on_main("", session)
                if self._cancel_event.is_set():
                    self._push_status("Cancelled", recording=False, revert_after=5)
                else:
                    self._push_status("No speech detected", recording=False, revert_after=5)
                return

            # Transcription succeeded — always publish the result even if
            # cancel was requested while inference was running.  The cancel
            # only interrupts via _check_cancel raising TranscriptionError;
            # if we got here, the work is done and shouldn't be discarded.
            self._publish_transcript(
                text=text,
                source_kind="microphone",
                source_label="Live Dictation",
                auto_copy=self.config.auto_copy_to_clipboard if auto_copy is None else auto_copy,
                session=session,
            )
        except TranscriptionError as exc:
            if self._cancel_event.is_set():
                self._push_status("Cancelled", recording=False, revert_after=5)
            else:
                logger.error(str(exc))
                self._push_status(str(exc), recording=False, revert_after=5)
        except Exception as exc:
            logger.error(f"Unexpected transcription error: {exc}")
            self._push_status("Transcription failed unexpectedly", recording=False, revert_after=5)
        finally:
            self.is_transcribing = False
            AppHelper.callAfter(self.overlay_controller.set_transcribing, False)
            AppHelper.callAfter(self._restore_base_status)
            self._finalize_deferred_overlay_actions()

    # -- Direct file transcription (single-file shortcut) --

    def transcribe_file_directly(self, path: str) -> None:
        """Transcribe a single file immediately, bypassing the queue."""
        if not path:
            return

        if not self.transcriber.is_ready():
            self._push_status("Model is still loading, please wait", recording=False)
            return

        if self.recording_active or self.is_transcribing:
            self._push_status(
                "Finish the current operation first", recording=self.recording_active,
            )
            return

        self._reset_deferred_flags()
        self.current_transcript = ""
        self.is_transcribing = True
        self._cancel_event.clear()
        self._overlay_session += 1
        session = self._overlay_session
        self.overlay_visible = True

        filename = Path(path).name
        AppHelper.callAfter(self._prepare_file_transcription_on_main)
        self._push_status(f"Transcribing {filename}\u2026", recording=False)
        threading.Thread(
            target=self._transcribe_file_worker,
            args=(path, filename, session),
            daemon=True,
        ).start()

    def _prepare_file_transcription_on_main(self) -> None:
        self.overlay_controller.set_current_text("")
        self.overlay_controller.show_mode("result")
        self.overlay_controller.set_transcribing(True)

    def _transcribe_file_worker(self, path: str, filename: str, session: int) -> None:
        def _progress(current_pos, total_pos):
            if self._cancel_event.is_set():
                raise TranscriptionError("Cancelled")
            pct = int(current_pos / total_pos * 100) if total_pos > 0 else 0
            self._push_status(f"{filename}: {pct}%", recording=False)

        try:
            text = self._final_transcribe_file(path, _progress, self._cancel_event)
            if not text:
                if self._cancel_event.is_set():
                    self._push_status("Cancelled", recording=False, revert_after=5)
                else:
                    self._push_status("No speech detected", recording=False, revert_after=5)
                return

            # Same invariant as mic transcription: if transcribe_file returned
            # text, publish it even if cancel was requested mid-inference.
            self._publish_transcript(
                text=text,
                source_kind="file",
                source_label=filename,
                auto_copy=self.config.auto_copy_to_clipboard,
                session=session,
            )
        except TranscriptionError as exc:
            if self._cancel_event.is_set():
                self._push_status("Cancelled", recording=False, revert_after=5)
            else:
                logger.error(str(exc))
                self._push_status(str(exc), recording=False, revert_after=5)
        except Exception as exc:
            logger.error(f"Unexpected transcription error: {exc}")
            self._push_status(
                "Transcription failed unexpectedly", recording=False, revert_after=5,
            )
        finally:
            self.is_transcribing = False
            AppHelper.callAfter(self.overlay_controller.set_transcribing, False)
            AppHelper.callAfter(self._restore_base_status)
            self._finalize_deferred_overlay_actions()

    # -- Queue delegate methods --

    def queue_add_files(self, paths) -> None:
        normalized = [str(Path(path)) for path in paths if path]
        if not normalized:
            return

        self.queue.add_many(normalized)
        self._refresh_queue_on_main()
        self._show_overlay_on_main("queue")

    def queue_remove_item(self, item_id: str) -> None:
        self.queue.remove(item_id)
        self._refresh_queue_on_main()

    def queue_move_item(self, item_id: str, new_index: int) -> None:
        self.queue.move(item_id, new_index)
        self._refresh_queue_on_main()

    def queue_clear_requested(self) -> None:
        self.queue.clear()
        self._refresh_queue_on_main()

    def queue_start_requested(self) -> None:
        if not self.transcriber.is_ready():
            self._push_status("Model is still loading, please wait", recording=False)
            return

        if self.is_transcribing or self.recording_active:
            self._push_status("Finish the current operation first", recording=self.recording_active)
            return

        if self.queue.pending_count() == 0:
            self._push_status("No files in queue", recording=False, revert_after=5)
            return

        output_config = self.overlay_controller.show_output_mode_dialog()
        if output_config is None:
            return

        self.is_transcribing = True
        self._queue_cancel_event.clear()
        self._cancel_event.clear()
        session = self._overlay_session
        AppHelper.callAfter(self.overlay_controller.set_queue_processing, True)
        AppHelper.callAfter(self.overlay_controller.set_transcribing, True)
        self._push_status("Processing queue\u2026", recording=False)
        threading.Thread(
            target=self._process_queue_worker,
            args=(output_config, session),
            daemon=True,
        ).start()

    def _process_queue_worker(self, output_config, session: int) -> None:
        items = self.queue.items()
        pending = [item for item in items if item.status == "pending"]
        # Export only items processed in this run; "done" items from earlier
        # runs were already exported and must not be duplicated.
        run_ids = {item.id for item in pending}

        try:
            for index, item in enumerate(pending, start=1):
                if self._queue_cancel_event.is_set():
                    self.queue.set_status(item.id, "cancelled")
                    self._refresh_queue_on_main()
                    continue

                self.queue.set_status(item.id, "processing")
                self._refresh_queue_on_main()

                def _progress(current_pos, total_pos, _fn=item.filename, _idx=index, _total=len(pending)):
                    if self._queue_cancel_event.is_set():
                        raise TranscriptionError("Cancelled")
                    pct = int(current_pos / total_pos * 100) if total_pos > 0 else 0
                    prefix = f"[{_idx}/{_total}] " if _total > 1 else ""
                    self._push_status(f"{prefix}{_fn}: {pct}%", recording=False)

                self._push_status(
                    f"[{index}/{len(pending)}] {item.filename}", recording=False,
                )

                try:
                    text = self._final_transcribe_file(item.path, _progress, self._queue_cancel_event)
                except TranscriptionError as exc:
                    if self._queue_cancel_event.is_set():
                        self.queue.set_status(item.id, "cancelled")
                    else:
                        self.queue.set_status(item.id, "failed", error=str(exc))
                        logger.error(f"Queue item failed: {item.filename}: {exc}")
                    self._refresh_queue_on_main()
                    continue
                except Exception as exc:
                    self.queue.set_status(item.id, "failed", error=str(exc))
                    logger.error(f"Queue item error: {item.filename}: {exc}")
                    self._refresh_queue_on_main()
                    continue

                if not text:
                    self.queue.set_status(item.id, "failed", error="No speech detected")
                    self._refresh_queue_on_main()
                    continue

                self.queue.set_status(item.id, "done", result_text=text)
                self.history_store.add_entry("file", item.filename, text)
                self._refresh_queue_on_main()

            # Export results
            completed_items = [
                i for i in self.queue.items()
                if i.id in run_ids and i.status == "done" and i.result_text
            ]
            if completed_items and not self._queue_cancel_event.is_set():
                try:
                    summary = export_results(completed_items, output_config)
                    self._push_status(summary, recording=False, revert_after=5)
                    if output_config.mode.value == "clipboard":
                        self._flash_copy_feedback_on_main()
                except ExportError as exc:
                    logger.error(f"Export failed: {exc}")
                    self._push_status(f"Export failed: {exc}", recording=False, revert_after=5)
            elif self._queue_cancel_event.is_set():
                if completed_items:
                    try:
                        summary = export_results(completed_items, output_config)
                        self._push_status(
                            f"Queue cancelled. {summary}", recording=False, revert_after=5,
                        )
                    except ExportError:
                        self._push_status("Queue cancelled", recording=False, revert_after=5)
                else:
                    self._push_status("Queue cancelled", recording=False, revert_after=5)
            else:
                failed_items = [i for i in self.queue.items() if i.id in run_ids and i.status == "failed"]
                if failed_items:
                    self._push_status("All items failed", recording=False, revert_after=5)
                else:
                    self._push_status("No transcription output", recording=False, revert_after=5)

            self._refresh_history_on_main()

        finally:
            self.is_transcribing = False
            AppHelper.callAfter(self.overlay_controller.set_transcribing, False)
            AppHelper.callAfter(self.overlay_controller.set_queue_processing, False)
            AppHelper.callAfter(self._restore_base_status)
            self._refresh_queue_on_main()
            self._finalize_deferred_overlay_actions()

    def handle_media_files(self, paths) -> None:
        self.queue_add_files(paths)

    def _refresh_queue_on_main(self) -> None:
        items = self.queue.items()
        AppHelper.callAfter(self.overlay_controller.set_queue_items, items)

    def _publish_transcript(
        self, text: str, source_kind: str, source_label: str, auto_copy: bool, session: int,
    ) -> None:
        self.current_transcript = text
        self.history_store.add_entry(source_kind, source_label, text)
        self._set_current_text_on_main(text, session)
        self._refresh_history_on_main()

        with self._state_lock:
            force_copy = self._force_copy_after_transcription
        # Auto-paste works via Cmd+V, so it requires the clipboard copy
        # regardless of the auto-copy setting.
        should_paste = self.config.paste_to_active_app and source_kind == "microphone"
        copied = False
        if auto_copy or force_copy or should_paste:
            copied = self._copy_text_with_feedback(
                text,
                success_status="Copied transcript to clipboard",
                failure_status="Transcript ready, but clipboard copy failed",
            )
        else:
            self._push_status("Transcript ready", recording=False, revert_after=5)

        if copied and should_paste:
            AppHelper.callAfter(self._paste_into_previous_app_on_main, session)

    def copy_current_transcript(self) -> None:
        text = self.current_transcript.strip()
        if not text:
            # Fall back to whatever the overlay is showing (e.g. the live
            # draft during a recording) — the user is looking right at it.
            text = (self.overlay_controller.current_text or "").strip()
        if not text:
            self._push_status("No transcript to copy", recording=self.recording_active)
            return

        self._copy_text_with_feedback(
            text,
            success_status="Copied transcript to clipboard",
            failure_status="Clipboard copy failed",
            recording=self.recording_active,
        )

    def handle_device_selected(self, device_name: str | None) -> None:
        self.recorder.set_device(device_name)

    def _refresh_input_devices(self) -> None:
        # Device enumeration can rebuild the PortAudio session (~100-250ms);
        # off the main thread so the overlay never beachballs.
        def _enumerate():
            devices = self.recorder.list_input_devices()
            selected = self.recorder.get_selected_device_name()
            AppHelper.callAfter(self.overlay_controller.update_input_devices, devices, selected)

        threading.Thread(target=_enumerate, daemon=True).start()

    def clear_history_requested(self) -> None:
        self.history_store.clear()
        self._refresh_history_on_main()
        self._push_status("History cleared", recording=self.recording_active, revert_after=5)

    def _set_current_text_on_main(self, text: str, session: int) -> None:
        AppHelper.callAfter(self._apply_current_text_on_main, text, session)

    def _apply_current_text_on_main(self, text: str, session: int) -> None:
        if session != self._overlay_session:
            return
        self.overlay_controller.set_current_text(text)

    def _refresh_history_on_main(self) -> None:
        AppHelper.callAfter(self.overlay_controller.set_history_text, self.history_store.render())

    def _flash_copy_feedback_on_main(self) -> None:
        AppHelper.callAfter(self.overlay_controller.flash_copy_feedback)

    def _copy_text_with_feedback(
        self,
        text: str,
        success_status: str,
        failure_status: str,
        recording: bool = False,
    ) -> bool:
        try:
            copy_text(text)
        except ClipboardError as exc:
            logger.error(f"Clipboard error: {exc}")
            self._push_status(failure_status, recording=recording)
            return False

        self._flash_copy_feedback_on_main()
        self._push_status(success_status, recording=recording, revert_after=5)
        return True

    def _start_live_preview(self) -> None:
        existing = self._live_thread
        if existing is not None and existing.is_alive():
            # A wedged previous preview still holds the streaming encoder —
            # never start a second stream on the same model.
            logger.warning("Previous live preview still running; preview disabled this recording")
            return
        session = self._overlay_session
        self._live_stop_event = threading.Event()
        self._live_thread = threading.Thread(
            target=self._live_preview_worker,
            args=(session, self._live_stop_event),
            daemon=True,
        )
        self._live_thread.start()

    def _live_preview_worker(self, session: int, stop_event: threading.Event) -> None:
        try:
            self.transcriber.stream_drafts(
                frames_provider=lambda: self.recorder.frames,
                stop_event=stop_event,
                on_draft=lambda text: self._set_current_text_on_main(text, session),
                rate=self.recorder.rate,
            )
        except Exception as exc:
            logger.warning(f"Live preview unavailable: {exc}")

    def _finish_live_preview(self) -> bool:
        """True when the streaming pass has fully released the encoder.
        On False the offline pass MUST NOT run (the encoder is still in
        streaming attention mode and would produce garbage)."""
        thread = self._live_thread
        if thread is None:
            return True
        self._live_stop_event.set()
        thread.join(timeout=30.0)
        if thread.is_alive():
            logger.error("Live preview thread wedged; refusing offline pass")
            return False
        self._live_thread = None
        return True

    def _paste_into_previous_app_on_main(self, session: int) -> None:
        if session != self._overlay_session:
            return
        if not accessibility_trusted():
            self._push_status(
                "Auto-paste needs Accessibility permission", recording=False, revert_after=8,
            )
            self._open_accessibility_settings()
            return

        previous_app = self._previous_app
        self.overlay_visible = False
        self.overlay_controller.hide()
        if previous_app is None or previous_app.isTerminated():
            self._push_status(
                "Copied — auto-paste skipped (previous app closed)",
                recording=False,
                revert_after=5,
            )
            return
        previous_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)

        def _send_after_focus_returns():
            time.sleep(0.3)
            # Never blind-fire Cmd+V: only paste if the app we re-activated
            # actually ended up frontmost (the user may have switched away).
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is None or front.processIdentifier() != previous_app.processIdentifier():
                logger.warning("Auto-paste skipped: frontmost app changed")
                self._push_status(
                    "Copied — auto-paste skipped (focus changed)",
                    recording=False,
                    revert_after=5,
                )
                return
            try:
                send_paste_keystroke()
            except PasteError as exc:
                logger.error(f"Auto-paste failed: {exc}")
                self._push_status("Auto-paste failed", recording=False, revert_after=5)

        threading.Thread(target=_send_after_focus_returns, daemon=True).start()

    def _on_setting_toggled(self, sender) -> None:
        for name, item in self._settings_items.items():
            if item is sender:
                value = not getattr(self.config, name)
                setattr(self.config, name, value)
                item.state = 1 if value else 0
                self._save_settings()
                if name == "paste_to_active_app" and value and not accessibility_trusted():
                    self._push_status(
                        "Grant Accessibility access to enable auto-paste",
                        recording=None,
                        revert_after=8,
                    )
                    self._open_accessibility_settings()
                elif name == "high_accuracy":
                    if value:
                        self.qwen.start_loading()
                        self._push_status(
                            "Loading high-accuracy model…", recording=None, revert_after=8,
                        )
                    else:
                        self.qwen.unload()
                        self._push_status(
                            "High-accuracy model unloaded", recording=None, revert_after=5,
                        )
                return

    def _save_settings(self) -> None:
        try:
            self.config.save(self._settings_path)
        except OSError as exc:
            logger.error(f"Failed to save settings: {exc}")

    def _open_accessibility_settings(self) -> None:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])

    def _reset_deferred_flags(self) -> None:
        with self._state_lock:
            self._hide_after_transcription = False
            self._force_copy_after_transcription = False

    def _finalize_deferred_overlay_actions(self) -> None:
        with self._state_lock:
            hide_after = self._hide_after_transcription
            session = self._overlay_session
            self._hide_after_transcription = False
            self._force_copy_after_transcription = False
        if hide_after:
            # _hide_overlay_on_main owns the flag flip; setting it here would
            # desync state when the session-guarded hide is skipped.
            AppHelper.callAfter(self._hide_overlay_on_main, session)

    def _push_status(self, message: str, recording: bool | None = None, revert_after: float = 0) -> None:
        AppHelper.callAfter(self._apply_status_on_main, message, recording, revert_after)

    def _apply_status_on_main(self, message: str, recording: bool | None, revert_after: float = 0) -> None:
        self._status_token += 1
        if revert_after == 0:
            self._base_status = message
        self.title = None
        self.status_item.title = f"Status: {message}"
        self.overlay_controller.set_status(message)
        if recording is not None:
            self.overlay_controller.set_recording(recording)
        if revert_after > 0:
            token = self._status_token
            timer = threading.Timer(revert_after, lambda: AppHelper.callAfter(self._revert_status, token))
            timer.daemon = True
            timer.start()

    def _restore_base_status(self) -> None:
        self._base_status = "Ready"

    def _revert_status(self, token: int) -> None:
        if token != self._status_token:
            return
        self.status_item.title = f"Status: {self._base_status}"
        self.overlay_controller.set_status(self._base_status)

    def cleanup(self) -> None:
        # The mic warm-up must not still be inside PortAudio when the audio
        # session is terminated or the interpreter finalizes (segfaults).
        self._warmup_thread.join(timeout=3.0)

        if self.hotkey_manager is not None:
            try:
                self.hotkey_manager.cleanup()
            except Exception as exc:
                logger.error(f"Hotkey cleanup failed: {exc}")

        try:
            self.recorder.cleanup()
        except Exception as exc:
            logger.error(f"Recorder cleanup failed: {exc}")

    @rumps.clicked("Show Overlay")
    def menu_show_overlay(self, sender):
        del sender
        self.show_overlay()

    @rumps.clicked("Show History")
    def menu_show_history(self, sender):
        del sender
        self.show_history_overlay()

    @rumps.clicked("Toggle Recording")
    def menu_toggle_recording(self, sender):
        del sender
        self.toggle_recording_requested()

    @rumps.clicked("Copy Last Transcript")
    def menu_copy_last(self, sender):
        del sender
        self.copy_current_transcript()

    @rumps.clicked("Open Media Files\u2026")
    def menu_open_files(self, sender):
        del sender
        self.show_overlay()
        AppHelper.callAfter(self.overlay_controller.openFiles_, None)

    @rumps.clicked("Clear History")
    def menu_clear_history(self, sender):
        del sender
        self.clear_history_requested()

    @rumps.clicked("Quit")
    def menu_quit(self, sender):
        del sender
        self.cleanup()
        rumps.quit_application()
