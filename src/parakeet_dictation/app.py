"""Application controller: owns dictation state, engine routing, and every worker thread."""

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
from .clipboard import ClipboardError, contains_text, copy_text
from .capture import CaptureSnapshot
from .config import AppConfig
from .corrections import apply_replacements
from .export import ExportError, export_results
from .history import HistoryStore
from .hotkeys import GlobalHotKeyManager, HotKeyError
from .indicator import DictationIndicator
from .logger_config import setup_logging
from .overlay import OverlayController
from .paths import app_support_dir, resource_path
from .preferences import PreferencesController
from .queue import TranscriptionQueue
from .isolated_recorder import IsolatedAudioRecorder as AudioRecorder
from .recordings import RecordingStore
from .recordings_window import RecordingsController
from .transcription import ParakeetTranscriber, QwenTranscriber, TranscriptionError

_SETTING_LABELS = {
    "compact_dictation": "Use Compact Dictation Bar",
    "prefer_builtin_mic": "Prefer Mac Microphone in Automatic Mode",
    "auto_start_recording": "Record Immediately on Option+Space",
    "auto_copy_to_clipboard": "Auto-Copy Result",
    "paste_to_active_app": "Paste Into Active App",
    "live_preview": "Live Preview While Speaking",
    "high_accuracy": "High-Accuracy Model (more RAM)",
    "use_corrections": "Apply My Word Replacements",
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
        self.qwen = QwenTranscriber(on_load_failed=self._on_qwen_load_failed)
        self.recorder = AudioRecorder(prefer_builtin=self.config.prefer_builtin_mic)
        self.recorder.set_device(self.config.input_device)
        self.recordings = RecordingStore(app_support_dir() / "recordings")
        # A leftover in-progress capture means a previous session crashed or
        # hung mid-recording — keep it recoverable.
        self._recovery_available = self.recorder.preserve_recovery() or self.recorder.has_recoverable_recording()
        if self._recovery_available:
            logger.info("Found unsaved recording from a previous session")
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
        self._starting = False
        self._stop_when_started = False
        self._start_thread: threading.Thread | None = None
        self._shutting_down = False
        self._compact_session = False
        self._capture_health = ""
        self._capture_warning = ""
        self._capture_at_stop: CaptureSnapshot | None = None
        self._last_status = "Loading speech model…"
        self._recordings_window: RecordingsController | None = None
        self._preferences_window: PreferencesController | None = None

        self._settings_items = {
            name: rumps.MenuItem(label, callback=self._on_setting_toggled)
            for name, label in _SETTING_LABELS.items()
        }
        for name, item in self._settings_items.items():
            item.state = 1 if getattr(self.config, name) else 0

        self.status_item = rumps.MenuItem("Status: Loading speech model\u2026")
        self.record_menu = rumps.MenuItem("Start Dictation")
        self.menu = [
            self.record_menu,
            rumps.MenuItem("Open Transcript"),
            rumps.MenuItem("Recordings…"),
            None,
            rumps.MenuItem("Settings…"),
            ("More", [rumps.MenuItem(name) for name in (
                "History", "Open Media Files…", "Copy Last Transcript", "Recover Last Recording",
                "Retry Speech Model", "Clear History & Recordings…", "Quick Start…",
            )]),
            None,
            self.status_item,
            rumps.MenuItem("Quit"),
        ]

        self.overlay_controller = OverlayController.alloc().initWithDelegate_config_(self, self.config)
        self.indicator = DictationIndicator.alloc().initWithDelegate_(self)
        self.overlay_controller.set_history_text(self.history_store.render())
        self.overlay_controller.set_current_text(
            "Press Option+Space to dictate. Press it again, or Cmd+R, to finish.\n\n"
            "Your transcript is copied automatically. Enable Paste Into Active App in Settings "
            "for direct insertion. Saved audio and retries are in Recordings."
        )

        self._start_model_watchdog()
        self._register_global_hotkeys()
        # Opening even a temporary mic at launch can change a Bluetooth
        # playback route. Capture is opened only after an explicit request.

    def _start_model_watchdog(self) -> None:
        threading.Thread(target=self._wait_for_model_readiness, daemon=True).start()

    def _wait_for_model_readiness(self) -> None:
        try:
            self.transcriber.wait_until_ready()
        except TranscriptionError as exc:
            logger.error(str(exc))
            self._push_status(self._model_unavailable_message(), recording=False)
            return
        finally:
            # Stagger the heavy Qwen load until Parakeet is up so dictation
            # is usable seconds after launch and the loads don't contend.
            if self.config.high_accuracy:
                self.qwen.start_loading()
            AppHelper.callAfter(self._refresh_preferences)

        if self._hotkey_error_message:
            self._push_status(self._hotkey_error_message, recording=False)
        else:
            self._push_status("Ready", recording=False)
        if self._recovery_available:
            self._push_status(
                "Unsaved recording found — use Recover Last Recording",
                recording=False,
                revert_after=12,
            )

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
        if not (self.recording_active or self.is_transcribing):
            self._capture_previous_app()
        self._compact_session = False
        self.indicator.hide()
        if self.current_transcript:
            self.overlay_controller.set_current_text(self.current_transcript)
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
        if self.recording_active:
            self.overlay_controller.show_active_microphone(self.recorder.capture_snapshot().device_name)
        if self.recording_active and not self._starting and self.config.live_preview and self._live_thread is None:
            self._start_live_preview()

    def _show_overlay_and_start_on_main(self) -> None:
        # A queued duplicate (rapid double Option+Space) must not bump the
        # session out from under the recording the first press started.
        if self.recording_active or self.is_transcribing:
            return
        self.start_recording()

    def _model_unavailable_message(self) -> str:
        if getattr(self.transcriber, "load_error", None) is not None:
            return "Speech model unavailable — check connection, then Retry Speech Model"
        return "Preparing speech model — first launch downloads weights"

    def retry_speech_model(self) -> None:
        if self.recording_active or self.is_transcribing:
            self._push_status("Finish the current operation before retrying the model", revert_after=5)
            return
        if self.transcriber.retry_loading():
            self._push_status("Preparing speech model…", False)
            self._start_model_watchdog()
        else:
            self._push_status("Speech model ready" if self.transcriber.is_ready()
                              else self._model_unavailable_message(), False, 5)

    def _refresh_preferences(self) -> None:
        if self._preferences_window is not None:
            self._preferences_window.refresh()

    def hide_overlay(self) -> None:
        action = None
        with self._state_lock:
            if self.recording_active:
                action = "stop_recording"
            elif self.is_transcribing:
                self._cancel_event.set()
                self._queue_cancel_event.set()
                self._hide_after_transcription = True
                self._push_status("Cancelling…", recording=False)
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
            message = self._model_unavailable_message()
            if self.config.compact_dictation and not self.overlay_visible:
                self._compact_session = True
                self.indicator.show()
                self.indicator.finish(message)
            self._push_status(message, recording=False)
            return False

        if self.is_transcribing:
            self._push_status("Wait for the current transcription to finish", recording=False)
            return False

        if self.recording_active:
            return True

        self._reset_deferred_flags()
        self._capture_previous_app()
        self._overlay_session += 1
        self.current_transcript = ""
        self._capture_warning = ""
        self._capture_health = ""
        self._capture_at_stop = None
        self._stop_when_started = False
        self._compact_session = self.config.compact_dictation and not self.overlay_visible
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()
        self.overlay_controller.prepare_for_recording()
        self.recording_active = True
        self._starting = True
        if self._compact_session:
            self.indicator.show()
            self._set_recording_shortcut(True)
        else:
            self.overlay_visible = True
            self.overlay_controller.show_mode("result")
        self._apply_status_on_main("Connecting microphone…", recording=True)
        session = self._overlay_session
        self._start_thread = threading.Thread(target=self._start_recording_worker, args=(session,), daemon=True)
        self._start_thread.start()
        AppHelper.callLater(10, self._check_microphone_start, session)
        return True

    def _start_recording_worker(self, session: int) -> None:
        try:
            started = self.recorder.start()
        except Exception as exc:
            self.recorder.last_error = exc
            started = False
        if self._shutting_down:
            if started:
                self.recorder.stop()
                self.recorder.preserve_recovery()
            return
        AppHelper.callAfter(self._recording_started, started, session)

    def _recording_started(self, started: bool, session: int) -> None:
        if session != self._overlay_session or self._shutting_down:
            return
        self._starting = False
        if not started:
            self.recording_active = False
            self._set_recording_shortcut(False)
            error = self.recorder.last_error
            self._apply_status_on_main("Connection cancelled" if self._stop_when_started else
                                      f"Microphone unavailable: {error}", False, 8)
            if self._compact_session:
                self.indicator.finish(self._last_status)
            return
        if self._stop_when_started:
            self.stop_recording_requested()
            return
        self._monitor_capture(session)
        self.overlay_controller.show_active_microphone(self.recorder.capture_snapshot().device_name)
        # A passive bar needs input levels, not a second model pass whose
        # drafts are never displayed. Expanding the window enables preview.
        if self.recording_active and self.config.live_preview and not self._compact_session:
            self._start_live_preview()

    def _check_microphone_start(self, session: int) -> None:
        if session == self._overlay_session and self._starting and not self._shutting_down:
            self._apply_status_on_main("Resetting microphone connection…", True)

    def _set_recording_shortcut(self, enabled: bool) -> None:
        if self.hotkey_manager is None:
            return
        session = self._overlay_session

        def stop_current_session():
            if session == self._overlay_session:
                self.stop_recording_requested()

        try:
            self.hotkey_manager.set_recording_shortcut(stop_current_session if enabled else None)
        except HotKeyError as exc:
            logger.warning(f"Cmd+R unavailable; use Option+Space: {exc}")

    def _monitor_capture(self, session: int) -> None:
        if session != self._overlay_session or not self.recording_active or self._shutting_down:
            return
        snapshot = self.recorder.capture_snapshot()
        if self._compact_session:
            self.indicator.set_capture(snapshot)
        health = snapshot.health
        if health != self._capture_health:
            self._capture_health = health
            message = {
                "waiting": "Waiting for microphone signal…",
                "receiving": "Recording…",
                "silent": "No microphone signal — check your input",
                "quiet": "Microphone is quiet — check your input",
                "missing": "Microphone is not delivering audio",
                "disconnected": "Microphone stopped delivering audio",
            }[health]
            self._apply_status_on_main(message, True)
        if health in ("missing", "disconnected"):
            self._capture_warning = "Microphone stopped — recording may be incomplete"
            self.stop_recording_requested()
            return
        AppHelper.callLater(0.15, self._monitor_capture, session)

    def stop_recording_requested(self, auto_copy: bool | None = None, hide_after: bool = False) -> None:
        if not self.recording_active:
            return

        with self._state_lock:
            if hide_after:
                self._hide_after_transcription = True
            if auto_copy:
                self._force_copy_after_transcription = True

        if self._starting:
            self._stop_when_started = True
            cancel = getattr(self.recorder, "cancel_start", None)
            if cancel is not None:
                cancel()
            self._apply_status_on_main("Cancelling microphone connection…", True)
            return

        session = self._overlay_session
        self._capture_at_stop = self.recorder.capture_snapshot()
        self._set_recording_shortcut(False)
        self.recording_active = False
        self.is_transcribing = True
        self._cancel_event.clear()
        self._live_stop_event.set()
        self._push_status("Transcribing\u2026", recording=False)
        AppHelper.callAfter(self.overlay_controller.set_transcribing, True)
        if self._compact_session:
            self.indicator.set_transcribing()
        # recorder.stop() joins the recording thread and touches PortAudio \u2014
        # either can block for seconds (or wedge outright on Bluetooth route
        # changes), so it must never run on the main thread. The worker owns
        # the stop; the few ms of extra audio captured before it runs are
        # harmless trailing silence.
        threading.Thread(
            target=self._transcribe_recording_worker,
            args=(auto_copy, session),
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

    def _qwen_transcribe_recorder_pcm(self, pcm_bytes: bytes) -> str:
        return self.qwen.transcribe_pcm(
            pcm_bytes,
            channels=self.recorder.channels,
            sample_width=self.recorder.sample_width(),
            rate=self.recorder.rate,
        )

    def _final_transcribe_pcm(self, pcm_bytes: bytes) -> str:
        if self._cancel_event.is_set():
            raise TranscriptionError("Cancelled")
        if self.config.high_accuracy and self.qwen.is_ready() and not self._cancel_event.is_set():
            try:
                text = self._qwen_transcribe_recorder_pcm(pcm_bytes)
                if text:
                    return text
                logger.warning("High-accuracy model returned no text; trying Parakeet")
            except Exception as exc:
                logger.error(f"High-accuracy transcription failed, falling back: {exc}")
        if self._cancel_event.is_set():
            raise TranscriptionError("Cancelled")
        return self.transcriber.transcribe_pcm(
            pcm_bytes,
            channels=self.recorder.channels,
            sample_width=self.recorder.sample_width(),
            rate=self.recorder.rate,
            progress_callback=self._check_cancel,
        )

    def _final_transcribe_file(self, path: str, progress_callback, cancel_event: threading.Event) -> str:
        if cancel_event.is_set():
            raise TranscriptionError("Cancelled")
        if self.config.high_accuracy and self.qwen.is_ready() and not cancel_event.is_set():
            try:
                text = self.qwen.transcribe_file(path)
                if text:
                    return text
                logger.warning("High-accuracy model returned no text; trying Parakeet")
            except Exception as exc:
                logger.error(f"High-accuracy transcription failed, falling back: {exc}")
        if cancel_event.is_set():
            raise TranscriptionError("Cancelled")
        return self.transcriber.transcribe_file(path, progress_callback=progress_callback)

    def _transcribe_recording_worker(self, auto_copy: bool | None, session: int) -> None:
        record = None
        diagnostics: dict = {}
        outcome = "failed"
        result_text = ""
        raw_text = ""
        result_message = "Transcription did not complete"
        stop_started = time.monotonic()
        inference_started: float | None = None
        try:
            pcm_bytes = self.recorder.stop()
            if getattr(self.recorder, "last_error", None) is not None:
                self._capture_warning = "Microphone connection interrupted — received audio was retained"
            snapshot = self._capture_at_stop or self.recorder.capture_snapshot()
            diagnostics = snapshot.diagnostics() | {
                "stop_seconds": time.monotonic() - stop_started,
                "captured_seconds": len(pcm_bytes) / 32000,
                "abandoned_audio_sessions": self.recorder.abandoned_sessions,
                "audio_worker_resets": getattr(self.recorder, "reset_count", 0),
                "active_threads": threading.active_count(),
            }
            # Save before inference, including silent/empty-result captures.
            # A model returning no text must never decide audio retention.
            try:
                record = self.recordings.save(pcm_bytes, diagnostics)
            except Exception as exc:
                logger.error(f"Could not archive recording; keeping recovery spill: {exc}")
            inference_started = time.monotonic()
            # The live preview stream must finish before the offline pass: it
            # holds the shared encoder in streaming (local attention) mode.
            has_signal = any(pcm_bytes)
            if not pcm_bytes or not has_signal:
                text = ""
            elif self._finish_live_preview():
                text = self._final_transcribe_pcm(pcm_bytes)
            elif self.qwen.is_ready() and not self._cancel_event.is_set():
                # The Parakeet encoder is stuck in streaming mode, but Qwen
                # is an independent model — rescue the dictation with it.
                logger.warning("Live preview wedged; using high-accuracy model for final pass")
                text = self._qwen_transcribe_recorder_pcm(pcm_bytes)
            else:
                raise TranscriptionError("Transcription engine stalled")

            if not text:
                # Clear any leftover live draft: it is display-only and must
                # not outlive a final pass that found no speech.
                self._set_current_text_on_main("", session)
                preserved = self.recorder.preserve_recovery(only_if_larger=True)
                retention = "audio kept for retry" if record is not None or preserved else "could not save audio"
                if self._cancel_event.is_set():
                    outcome = "cancelled"
                    result_message = f"Cancelled — {retention}" if pcm_bytes else "Cancelled"
                elif not pcm_bytes:
                    result_message = "No audio received from microphone — check your input"
                elif not has_signal:
                    result_message = "Microphone delivered silence — check your input"
                else:
                    result_message = f"No transcript returned — {retention}"
                self._push_status(result_message, recording=False, revert_after=8)
                return

            # Transcription succeeded — always publish the result even if
            # cancel was requested while inference was running.  The cancel
            # only interrupts via _check_cancel raising TranscriptionError;
            # if we got here, the work is done and shouldn't be discarded.
            raw_text = text
            published_text = self._publish_transcript(
                text=text,
                source_kind="microphone",
                source_label="Live Dictation",
                auto_copy=self.config.auto_copy_to_clipboard if auto_copy is None else auto_copy,
                session=session,
            )
            outcome = "done"
            result_text = published_text or text
            result_message = self._capture_warning
            if self._capture_warning:
                self._push_status(self._capture_warning, False, 8)
            if record is not None:
                self.recorder.discard_recovery()
            else:
                self.recorder.preserve_recovery(only_if_larger=True)
        except TranscriptionError as exc:
            if self._cancel_event.is_set():
                # An accidental cancel stays recoverable — but a quick
                # cancelled capture must not clobber a longer recording
                # still awaiting recovery.
                preserved = self.recorder.preserve_recovery(only_if_larger=True)
                outcome = "cancelled"
                result_message = ("Cancelled — audio kept for retry" if record is not None or preserved
                                  else "Cancelled — could not save audio")
                self._push_status(result_message, recording=False, revert_after=8)
            else:
                preserved = self.recorder.preserve_recovery()
                logger.error(str(exc))
                message = (
                    f"{exc} — recording saved (Recover Last Recording)" if preserved or record is not None else str(exc)
                )
                result_message = message
                self._push_status(message, recording=False, revert_after=8)
        except Exception as exc:
            preserved = self.recorder.preserve_recovery()
            logger.error(f"Unexpected transcription error: {exc}")
            message = (
                "Transcription failed — recording saved (Recover Last Recording)"
                if preserved or record is not None
                else "Transcription failed unexpectedly"
            )
            result_message = message
            self._push_status(message, recording=False, revert_after=8)
        finally:
            diagnostics["stop_to_result_seconds"] = time.monotonic() - stop_started
            if inference_started is not None:
                diagnostics["recognition_seconds"] = time.monotonic() - inference_started
            if record is not None:
                try:
                    self.recordings.update(record.id, status=outcome, text=result_text,
                                           raw_text=raw_text, message=result_message, diagnostics=diagnostics)
                except Exception as exc:
                    logger.error(f"Could not update recording details: {exc}")
            logger.info(f"Recording outcome={outcome} measurements={diagnostics}")
            AppHelper.callAfter(self._complete_transcription_on_main, session)

    def _complete_transcription_on_main(self, session: int) -> None:
        if session != self._overlay_session or self._shutting_down:
            return
        # Release operation state on the UI thread, after queued result
        # callbacks. A new hotkey cannot race old completion callbacks.
        self.is_transcribing = False
        if hasattr(self, "record_menu"):
            self.record_menu.title = "Start Dictation"
        self.overlay_controller.set_queue_processing(False)
        self.overlay_controller.set_transcribing(False)
        self._restore_base_status()
        self._finalize_deferred_overlay_actions()
        if self._compact_session:
            quick_success = self._last_status in ("Copied transcript to clipboard", "Transcript ready")
            self.indicator.finish(self._last_status, duration=2 if quick_success else 8)
        self._refresh_recordings_window()
        if self.overlay_visible:
            self._refresh_input_devices()

    # -- Direct file transcription (single-file shortcut) --

    def transcribe_file_directly(self, path: str) -> None:
        """Transcribe a single file immediately, bypassing the queue."""
        if not path:
            return

        if not self.transcriber.is_ready():
            self._push_status(self._model_unavailable_message(), recording=False)
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
        self._compact_session = False
        self.indicator.hide()
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
            # A wedged live-preview thread leaves the shared Parakeet encoder
            # in streaming mode — running the offline pass then would produce
            # garbage. The source file stays on disk, so fail instead.
            if not self._finish_live_preview():
                raise TranscriptionError("Transcription engine stalled — restart the app")
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
            AppHelper.callAfter(self._complete_transcription_on_main, session)

    # -- Recording recovery --

    def recover_last_recording(self, recording_id: str | None = None) -> None:
        """Transcribe the crash/failure-preserved capture from disk."""
        if not self.transcriber.is_ready():
            self._push_status(self._model_unavailable_message(), recording=False)
            return

        if self.recording_active or self.is_transcribing:
            self._push_status(
                "Finish the current operation first", recording=self.recording_active,
            )
            return

        records = self.recordings.list_recordings()
        if recording_id is None:
            unsaved = [record for record in records if record.status != "done"]
            if unsaved:
                recording_id = unsaved[0].id
            elif not self.recorder.has_recoverable_recording() and records:
                recording_id = records[0].id
        if recording_id is None and not self.recorder.has_recoverable_recording():
            self._push_status("No recording to recover", recording=False, revert_after=5)
            return

        self._reset_deferred_flags()
        self.current_transcript = ""
        self.is_transcribing = True
        self._cancel_event.clear()
        self._capture_previous_app()
        self._overlay_session += 1
        session = self._overlay_session
        self.overlay_visible = True
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()

        AppHelper.callAfter(self._prepare_file_transcription_on_main)
        self._push_status("Transcribing recovered recording…", recording=False)
        threading.Thread(
            target=self._recover_worker,
            args=(session, recording_id),
            daemon=True,
        ).start()

    def _recover_worker(self, session: int, recording_id: str | None = None) -> None:
        try:
            # Loaded here, not on the menu-click (main) thread: an hour of
            # PCM is ~115 MB.
            pcm_bytes = (self.recordings.load_pcm(recording_id) if recording_id is not None
                         else self.recorder.load_recoverable_recording())
            if not pcm_bytes:
                self._push_status("No recording to recover", recording=False, revert_after=5)
                return
            # Same wedged-encoder guard as live dictation, with the same
            # Qwen rescue; on failure the file is kept for a retry.
            if not any(pcm_bytes):
                raise TranscriptionError("This recording contains digital silence — choose another microphone")
            if self._finish_live_preview():
                text = self._final_transcribe_pcm(pcm_bytes)
            elif self.qwen.is_ready() and not self._cancel_event.is_set():
                logger.warning("Live preview wedged; using high-accuracy model for recovery")
                text = self._qwen_transcribe_recorder_pcm(pcm_bytes)
            else:
                raise TranscriptionError("Transcription engine stalled")

            if not text:
                if self._cancel_event.is_set():
                    self._push_status("Cancelled", recording=False, revert_after=5)
                else:
                    self._push_status(
                        "No transcript returned — recording kept for retry", recording=False, revert_after=8,
                    )
                return

            published_text = self._publish_transcript(
                text=text,
                source_kind="recovery",
                source_label="Recovered Recording",
                auto_copy=self.config.auto_copy_to_clipboard,
                session=session,
            )
            if recording_id is not None:
                self.recordings.update(recording_id, status="done", text=published_text or text, raw_text=text, message="")
            else:
                # Migrate legacy recovery audio to the recording browser.
                record = self.recordings.save(pcm_bytes)
                if record is not None:
                    self.recordings.update(record.id, status="done", text=published_text or text, raw_text=text)
                    self.recorder.discard_recoverable_recording()
        except TranscriptionError as exc:
            # Keep the file: recovery can be retried.
            if self._cancel_event.is_set():
                self._push_status("Cancelled", recording=False, revert_after=5)
            else:
                logger.error(str(exc))
                self._push_status(str(exc), recording=False, revert_after=5)
        except Exception as exc:
            logger.error(f"Unexpected recovery error: {exc}")
            self._push_status("Recovery failed unexpectedly", recording=False, revert_after=5)
        finally:
            AppHelper.callAfter(self._complete_transcription_on_main, session)

    def _on_qwen_load_failed(self, message: str) -> None:
        del message
        if not self.config.high_accuracy:
            # The user already toggled the setting off while the load was in
            # flight — don't warn about a model they no longer want.
            return
        self._push_status(
            "High-accuracy model failed to load — using standard model",
            recording=None,
            revert_after=8,
        )

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
            self._push_status(self._model_unavailable_message(), recording=False)
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
        self._compact_session = False
        self.indicator.hide()
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
            # Same guard as single-file transcription: a wedged live preview
            # leaves the shared encoder in streaming mode.
            if not self._finish_live_preview():
                self._push_status(
                    "Transcription engine stalled — restart the app",
                    recording=False,
                    revert_after=8,
                )
                return
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
            self._refresh_queue_on_main()
            AppHelper.callAfter(self._complete_transcription_on_main, session)

    def handle_media_files(self, paths) -> None:
        self.queue_add_files(paths)

    def _refresh_queue_on_main(self) -> None:
        items = self.queue.items()
        AppHelper.callAfter(self.overlay_controller.set_queue_items, items)

    def show_recordings(self) -> None:
        if self._recordings_window is None:
            self._recordings_window = RecordingsController.alloc().initWithDelegate_store_(self, self.recordings)
        self._recordings_window.show()

    def _refresh_recordings_window(self) -> None:
        if self._recordings_window is not None:
            self._recordings_window.refresh()

    def _publish_transcript(
        self, text: str, source_kind: str, source_label: str, auto_copy: bool, session: int,
    ) -> str:
        raw_text = text
        if self.config.use_corrections and source_kind in ("microphone", "recovery"):
            text = apply_replacements(text, self.config.replacements)
        self.current_transcript = text
        self.history_store.add_entry(source_kind, source_label, text, raw_text=raw_text)
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
            AppHelper.callAfter(self._paste_into_previous_app_on_main, session, text)
        return text

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
        if self.recording_active or self.is_transcribing:
            return
        self.recorder.set_device(device_name)
        self.config.input_device = device_name
        self._save_settings()

    def _refresh_input_devices(self) -> None:
        if (self.recording_active or self.is_transcribing or self._preferences_window is None
                or self._preferences_window.tabs.selectedSegment() != 1):
            return
        # Device enumeration can rebuild the PortAudio session (~100-250ms);
        # off the main thread so the overlay never beachballs.
        def _enumerate():
            devices = self.recorder.list_input_devices()
            if devices is None:
                # Audio session busy — keep the current popup rather than
                # showing a false "no input devices found".
                return
            selected = self.recorder.get_selected_device_name()
            AppHelper.callAfter(self._preferences_window.update_input_devices, devices, selected)

        threading.Thread(target=_enumerate, daemon=True).start()

    def clear_history_requested(self) -> None:
        if self.recording_active or self.is_transcribing:
            self._push_status("Finish the current operation before clearing history")
            return
        if rumps.alert(
            title="Clear History and Recordings?",
            message="This deletes saved transcripts and recording audio from this Mac.",
            ok="Clear", cancel=True,
        ) != 1:
            return
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()
        try:
            self.recordings.clear()
        except OSError as exc:
            self._push_status(f"Could not clear recordings: {exc}", revert_after=8)
            return
        self.recorder.discard_recovery()
        self.recorder.discard_recoverable_recording()
        cleared = self.history_store.clear()
        self.current_transcript = ""
        self.overlay_controller.set_current_text("")
        self._refresh_history_on_main()
        self._refresh_recordings_window()
        self._push_status("History cleared" if cleared else
                          "Audio cleared, but transcript history could not be deleted from disk",
                          recording=self.recording_active, revert_after=8)

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

    def _paste_into_previous_app_on_main(self, session: int, expected_text: str) -> None:
        if session != self._overlay_session or self._shutting_down:
            return
        if not accessibility_trusted():
            self._push_status(
                "Auto-paste needs Accessibility permission", recording=False, revert_after=8,
            )
            self._open_accessibility_settings()
            return

        previous_app = self._previous_app
        compact = self._compact_session
        if compact:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if previous_app is None or front is None or front.processIdentifier() != previous_app.processIdentifier():
                self._push_status("Copied — auto-paste skipped (focus changed)", False, 8)
                return
        self.overlay_visible = False
        self.overlay_controller.hide()
        if previous_app is None or previous_app.isTerminated():
            self._push_status(
                "Copied — auto-paste skipped (previous app closed)",
                recording=False,
                revert_after=5,
            )
            return
        if not compact:
            previous_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)

        def _send_after_focus_returns():
            if session != self._overlay_session or self._shutting_down:
                return
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
            if not contains_text(expected_text):
                self._push_status(
                    "Auto-paste skipped (clipboard changed); transcript saved",
                    recording=False, revert_after=8,
                )
                return
            try:
                send_paste_keystroke()
            except PasteError as exc:
                logger.error(f"Auto-paste failed: {exc}")
                self._push_status("Auto-paste failed", recording=False, revert_after=5)

        # Keep the last session/focus checks and dispatch on the UI thread.
        # A delayed callback avoids a sleeping worker per dictation and lets
        # new recording or shutdown events invalidate the pending paste.
        AppHelper.callLater(0 if compact else 0.3, _send_after_focus_returns)

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
                elif name == "prefer_builtin_mic":
                    self.recorder.prefer_builtin = value
                    self._refresh_input_devices()
                self._refresh_preferences()
                return

    def _save_settings(self) -> bool:
        try:
            self.config.save(self._settings_path)
            return True
        except OSError as exc:
            logger.error(f"Failed to save settings: {exc}")
            self._push_status("Settings could not be saved — check available disk space", revert_after=8)
            return False

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
        self._last_status = message
        self._status_token += 1
        if revert_after == 0:
            self._base_status = message
        self.title = None
        self.status_item.title = f"Status: {message}"
        self.record_menu.title = ("Stop Dictation" if self.recording_active else
                                  "Transcribing…" if self.is_transcribing else "Start Dictation")
        self.overlay_controller.set_status(message)
        if self._compact_session:
            self.indicator.set_status(message)
        if recording is not None:
            self.overlay_controller.set_recording(recording)
        if revert_after > 0:
            token = self._status_token
            AppHelper.callLater(revert_after, self._revert_status, token)

    def _restore_base_status(self) -> None:
        self._base_status = "Ready"

    def _revert_status(self, token: int) -> None:
        if token != self._status_token:
            return
        self.status_item.title = f"Status: {self._base_status}"
        self.overlay_controller.set_status(self._base_status)

    def cleanup(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._live_stop_event.set()
        self._cancel_event.set()
        self._queue_cancel_event.set()

        if self.hotkey_manager is not None:
            try:
                self.hotkey_manager.cleanup()
            except Exception as exc:
                logger.error(f"Hotkey cleanup failed: {exc}")

        def close_audio():
            if self._start_thread is not None:
                self._start_thread.join()
            try:
                self.recorder.cleanup()
            except Exception as exc:
                logger.error(f"Recorder cleanup failed: {exc}")

        # PortAudio teardown can wedge too. Leave the main loop responsive
        # and the spill file recoverable even if a driver never returns.
        closer = threading.Thread(target=close_audio, daemon=True)
        closer.start()
        closer.join(timeout=2)

    @rumps.clicked("Open Transcript")
    def menu_show_overlay(self, sender):
        del sender
        self.show_overlay()

    @rumps.clicked("More", "History")
    def menu_show_history(self, sender):
        del sender
        self.show_history_overlay()

    @rumps.clicked("Settings…")
    def menu_settings(self, sender):
        del sender
        if self._preferences_window is None:
            self._preferences_window = PreferencesController.alloc().initWithDelegate_labels_(self, _SETTING_LABELS)
        self._preferences_window.show()

    @rumps.clicked("More", "Retry Speech Model")
    def menu_retry_model(self, sender):
        del sender
        self.retry_speech_model()

    @rumps.clicked("More", "Quick Start…")
    def menu_quick_start(self, sender):
        del sender
        rumps.alert(title="Welcome to Maramax", message=(
            "Press Option+Space to start dictating and again to finish. Cmd+R also finishes a recording.\n\n"
            "The small bar leaves your current app focused. Your words copy to the clipboard. "
            "Enable Paste Into Active App in Settings for automatic insertion; macOS will require Accessibility access.\n\n"
            "Automatic input prefers the Mac microphone, so your headphones can stay an output device. "
            "Choose AirPods explicitly in Open Transcript to use their microphone.\n\n"
            "Recordings keeps audio for playback, export, and retry. "
            "Speech recognition runs locally; model weights download on first use."
        ))

    @rumps.clicked("Start Dictation")
    def menu_toggle_recording(self, sender):
        del sender
        self.toggle_recording_requested()

    @rumps.clicked("More", "Copy Last Transcript")
    def menu_copy_last(self, sender):
        del sender
        self.copy_current_transcript()

    @rumps.clicked("More", "Recover Last Recording")
    def menu_recover_last(self, sender):
        del sender
        self.recover_last_recording()

    @rumps.clicked("Recordings…")
    def menu_recordings(self, sender):
        del sender
        self.show_recordings()

    @rumps.clicked("More", "Open Media Files\u2026")
    def menu_open_files(self, sender):
        del sender
        self.show_overlay()
        AppHelper.callAfter(self.overlay_controller.openFiles_, None)

    @rumps.clicked("More", "Clear History & Recordings…")
    def menu_clear_history(self, sender):
        del sender
        self.clear_history_requested()

    @rumps.clicked("Quit")
    def menu_quit(self, sender):
        del sender
        self.cleanup()
        rumps.quit_application()
