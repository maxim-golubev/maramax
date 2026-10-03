"""The dictation controller: carries out each user action according to the current phase."""

from __future__ import annotations

import enum
import subprocess
import threading
import time
from pathlib import Path

import rumps
from AppKit import NSApplication, NSMenu, NSMenuItem
from PyObjCTools import AppHelper

from . import __version__, recovery
from .audio_format import seconds as pcm_seconds
from .audio_format import whole_samples
from .autopaste import PasteError, PasteTarget, accessibility_trusted, send_paste_keystroke
from .capture import CaptureHealth, CaptureSnapshot
from .clipboard import ClipboardError, contains_text, copy_text
from .config import AppConfig
from .corrections import apply_replacements, vocabulary_hint
from .export import ExportError, OutputMode, export_results
from .file_queue import QueueStatus, TranscriptionQueue
from .history import HistoryStore, Source, adopt_legacy_history
from .hotkeys import STOP, GlobalHotKeyManager, HotKeyError, HotKeySpec, dictation_shortcut, shortcut_problem
from .indicator import DictationIndicator
from .isolated_recorder import IsolatedAudioRecorder
from .logger_config import logger
from .main_thread import call_later
from .overlay import Mode, OverlayController
from .paths import app_bundle, app_support_dir, resource_path
from .preferences import PreferencesController
from .recordings import RecordingStatus, RecordingStore, recovery_candidate
from .recordings_window import RecordingsController
from .transcription import ParakeetTranscriber, QwenTranscriber, TranscriptionCancelled, TranscriptionError
from .update_offer import CHECK_TITLE, UpdateOffer
from .welcome import WelcomeController

_SETTING_LABELS = {
    "compact_dictation": "Use the compact dictation bar",
    "prefer_builtin_mic": "Prefer the Mac’s own microphone in Automatic",
    "auto_start_recording": "Start dictating as soon as the shortcut is pressed",
    "auto_copy_to_clipboard": "Copy the transcript to the clipboard",
    "paste_to_active_app": "Paste into the active app",
    "live_preview": "Show a live preview in the full window",
    "high_accuracy": "Use the high-accuracy model",
    "use_corrections": "Apply my word replacements",
    "check_for_updates": "Check for updates automatically",
}
_PASTE_LABEL = _SETTING_LABELS["paste_to_active_app"]



def intro_text(shortcut: str, shortcut_starts: bool) -> str:
    """`shortcut_starts`: the shortcut starts a dictation (auto_start_recording);
    otherwise it brings up this window, where Cmd+R or Dictate starts one."""
    start = (f"Press {shortcut} to dictate. Press it again, or {STOP.label}, to finish." if shortcut_starts else
             f"Press {STOP.label} or Dictate to start, and again to finish. {shortcut} brings this window "
             "back from any app.")
    return (f"{start}\n\n"
            f"Your transcript is copied automatically. Turn on “{_PASTE_LABEL}” in Settings "
            "for direct insertion. Saved audio and retries are in Recordings.")


def empty_history_text(shortcut: str) -> str:
    return f"No transcriptions yet.\n\nUse {shortcut} to dictate, or drop audio and video files into this window."


_HEALTH_STATUS = {
    CaptureHealth.WAITING: "Waiting for microphone signal…",
    CaptureHealth.RECEIVING: "Recording…",
    CaptureHealth.RECONNECTING: "Microphone lost — switching input…",
    CaptureHealth.SILENT: "No microphone signal — check your input",
    CaptureHealth.QUIET: "Microphone is quiet — check your input",
    CaptureHealth.MISSING: "Microphone is not delivering audio",
    CaptureHealth.DISCONNECTED: "Microphone stopped delivering audio",
}
# How long the compact bar stays up after a dictation ends.
BAR_SECONDS_AFTER_SUCCESS = 2.0
BAR_SECONDS_AFTER_PROBLEM = 8.0


class Phase(enum.Enum):
    """What the app is doing. Exactly one at a time, which is why this is one
    value and not a set of flags that could contradict each other."""
    IDLE = "idle"
    CONNECTING = "connecting"      # microphone requested, not yet open
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"  # a dictation, a file, the queue, or a recovery


def empty_capture_outcome(*, has_audio: bool, has_signal: bool, faint: bool, cancelled: bool,
                          audio_kept: bool) -> tuple[RecordingStatus, str]:
    """What to record and tell the user when a dictation produced no text."""
    retention = "audio kept for retry" if audio_kept else "could not save audio"
    if cancelled:
        return RecordingStatus.CANCELLED, f"Cancelled — {retention}" if has_audio else "Cancelled"
    if not has_audio:
        return RecordingStatus.FAILED, "No audio received from microphone — check your input"
    if not has_signal:
        return RecordingStatus.FAILED, "Microphone delivered silence — check your input"
    if faint:
        return RecordingStatus.FAILED, f"No speech heard — signal too faint; {retention}"
    return RecordingStatus.FAILED, f"No transcript returned — {retention}"


def queue_run_summary(*, cancelled: bool, exported: str | None, export_error: str | None,
                      any_failed: bool) -> str:
    """The one-line outcome of a queue run. `exported` is the export's own
    summary when something was written or copied."""
    if export_error is not None and not cancelled:
        return f"Export failed: {export_error}"
    if cancelled:
        return f"Queue cancelled. {exported}" if exported else "Queue cancelled"
    if exported:
        return exported
    return "All items failed" if any_failed else "No transcription output"


class DictationApp(rumps.App):
    def __init__(self, config: AppConfig | None = None, support_dir: Path | None = None):
        super().__init__(
            "Maramax",
            title=None,
            icon=str(resource_path("assets", "menu_icon.png")),
            template=True,
            quit_button=None,
        )
        self._support_dir = support_dir or app_support_dir()
        self._settings_path = self._support_dir / "settings.json"
        self.config = config or AppConfig.load(self._settings_path)
        self._dictate = dictation_shortcut(*self.config.dictation_shortcut)
        self.transcriber = ParakeetTranscriber()
        self.qwen = QwenTranscriber(
            on_load_failed=self._on_qwen_load_failed,
            on_loaded=lambda: AppHelper.callAfter(self._refresh_preferences),
        )
        self.recorder = IsolatedAudioRecorder(self._support_dir)
        self._configure_recorder()
        self.recordings = RecordingStore(self._support_dir / "recordings")
        # A leftover in-progress capture means a previous session crashed or
        # hung mid-recording — keep it recoverable.
        self._leftover_found = self.recorder.preserve_recovery() or bool(recovery.unsaved_recordings(self._support_dir))
        if self._leftover_found:
            logger.info("Found unsaved recording from a previous session")
            threading.Thread(target=self._adopt_recovered_audio, daemon=True).start()
        adopt_legacy_history(self._support_dir)
        self.history_store = HistoryStore(self._support_dir, history_limit=self.config.history_limit)
        self.queue = TranscriptionQueue()
        self.current_transcript = ""
        self.overlay_visible = False

        self._phase = Phase.IDLE
        # Bumped whenever a new operation takes ownership of the display.
        # Workers and delayed callbacks carry the value they started under
        # and drop their result if it has moved on.
        self._session = 0
        self._stop_when_connected = False
        self._hide_window_when_done = False
        self._compact_session = False
        self._drafts_session: int | None = None
        self._start_cancel = threading.Event()
        self._start_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._queue_cancel_event = threading.Event()
        self._shutting_down = False

        self._status_token = 0
        self._resting_status = self.transcriber.status_message()
        self._last_status = self._resting_status
        self._capture_health: CaptureHealth | None = None
        self._capture_warning = ""
        self._capture_device = ""
        self._capture_at_stop: CaptureSnapshot | None = None

        self.hotkey_manager: GlobalHotKeyManager | None = None
        self._hotkey_error_message: str | None = None
        self._paste_target = PasteTarget()
        self._previous_app = None
        self._recordings_window: RecordingsController | None = None
        self._preferences_window: PreferencesController | None = None
        self._welcome_window: WelcomeController | None = None

        self.status_item = rumps.MenuItem(f"Status: {self._resting_status}")
        self.record_menu = rumps.MenuItem("Start Dictation")
        update_item = rumps.MenuItem(CHECK_TITLE)
        self.menu = [
            self.record_menu,
            rumps.MenuItem("Open Transcript"),
            rumps.MenuItem("Recordings…"),
            None,
            rumps.MenuItem("Settings…"),
            update_item,
            ("More", [rumps.MenuItem(name) for name in (
                "History", "Open Media Files…", "Copy Last Transcript", "Recover Last Recording",
                "Retry Speech Model", "Clear History & Recordings…", "Welcome…",
            )]),
            None,
            self.status_item,
            rumps.MenuItem("Quit"),
        ]

        self.overlay_controller = OverlayController.alloc().initWithDelegate_(self)
        self.indicator = DictationIndicator.alloc().initWithDelegate_(self)
        self.overlay_controller.set_status(self._resting_status)
        self.overlay_controller.set_history_text(self._history_text())
        self._show_intro_text()

        self.updates = UpdateOffer(
            menu_item=update_item, current_version=__version__, installed_app=app_bundle(),
            support_dir=self._support_dir, config=self.config, save_settings=self._save_settings, is_busy=self._is_in_use,
            quit_app=rumps.quit_application, on_change=self._show_update_status,
        )

        self._install_edit_menu()
        self._start_model_watchdog()
        self._register_global_hotkeys()
        self.updates.start()
        if not self.config.onboarded:
            call_later(1.0, self.show_welcome)
        # Opening even a temporary mic at launch can change a Bluetooth
        # playback route. Capture is opened only after an explicit request.

    # -- Phase --

    @property
    def recording_active(self) -> bool:
        return self._phase in (Phase.CONNECTING, Phase.RECORDING)

    @property
    def is_transcribing(self) -> bool:
        return self._phase is Phase.TRANSCRIBING

    @property
    def is_busy(self) -> bool:
        return self._phase is not Phase.IDLE

    def _set_phase(self, phase: Phase) -> None:
        """The one place the phase changes. Settings and Recordings disable
        what cannot be used while busy, so they are told when that flips."""
        was_busy = self.is_busy
        self._phase = phase
        if self.is_busy == was_busy:
            return
        if self._preferences_window is not None:
            self._preferences_window.show_busy_state()
        if self._recordings_window is not None:
            self._recordings_window.show_busy_state()
        self.refresh_input_devices()  # Not listed while busy; listed again once idle.

    def _is_in_use(self) -> bool:
        """Busy, or quitting now (to install an update) would cut something
        short: an outcome still on the bar, or a WAV still being saved."""
        return (self.is_busy or self.indicator.is_finished()
                or (self._recordings_window is not None and self._recordings_window.is_saving()))

    def _begin_transcribing(self) -> int:
        """Take ownership of the display for a new non-microphone operation."""
        self._hide_window_when_done = False
        self.current_transcript = ""
        self._set_phase(Phase.TRANSCRIBING)
        self._cancel_event.clear()
        self._session += 1
        self.overlay_visible = True
        return self._session

    # -- Start-up --

    @staticmethod
    def _install_edit_menu() -> None:
        """A menu-bar app has no visible menu bar, but AppKit still routes
        Cmd+X/C/V/A/Z/W through the main menu. Without one, text fields in
        Settings cannot be pasted into."""
        application = NSApplication.sharedApplication()
        if application.mainMenu() is not None:
            return
        edit = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in (
            ("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"), ("Close Window", "performClose:", "w"),
        ):
            edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
        holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        holder.setSubmenu_(edit)
        main = NSMenu.alloc().initWithTitle_("Maramax")
        main.addItem_(holder)
        application.setMainMenu_(main)

    def _configure_recorder(self) -> None:
        """The one place microphone settings reach the recorder."""
        self.recorder.device_name = self.config.input_device
        self.recorder.prefer_builtin = self.config.prefer_builtin_mic
        self.recorder.keep_warm_seconds = self.config.keep_mic_ready_seconds

    def _adopt_recovered_audio(self) -> None:
        """Move captures left behind by a crash or a failed archive into
        Recordings, oldest first, where they can be played, exported, and
        transcribed like any other."""
        for unsaved in recovery.unsaved_recordings(self._support_dir):
            try:
                pcm = recovery.load_unsaved(unsaved)
                if not pcm:
                    continue
                record = self.recordings.save(whole_samples(pcm), {"device_name": "Unknown microphone"})
                if record is None:
                    continue
                self.recordings.update(record.id, message="Recovered after an interrupted session")
                recovery.discard_unsaved(unsaved)
            except Exception as exc:
                # The file stays, so Recover Last Recording still works.
                logger.error(f"Could not move recovered audio {unsaved.name} into Recordings: {exc}")

    def _start_model_watchdog(self) -> None:
        threading.Thread(target=self._wait_for_model_readiness, daemon=True).start()

    def _wait_for_model_readiness(self) -> None:
        try:
            self.transcriber.wait_until_ready()
        except TranscriptionError as exc:
            logger.error(str(exc))
            self._push_status(self.transcriber.status_message())
            return
        finally:
            # Stagger the heavy Qwen load until Parakeet is up so dictation
            # is usable seconds after launch and the loads don't contend.
            if self.config.high_accuracy:
                self.qwen.start_loading()
            AppHelper.callAfter(self._refresh_preferences)

        self._prepare_recorder()
        self._push_status("Ready" if self._hotkey_error_message is None else self._hotkey_error_message)
        if self._leftover_found:
            self._leftover_found = False
            self._push_status("Unsaved recording found — see Recordings", revert_after=12)

    def _prepare_recorder(self) -> None:
        # The audio helper is launched ahead of time (no device is opened),
        # so the hotkey pays for the driver open only.
        if not self._shutting_down:
            self.recorder.prepare()

    def _register_global_hotkeys(self) -> None:
        try:
            self.hotkey_manager = GlobalHotKeyManager(self.dictation_hotkey_pressed)
            self.hotkey_manager.set_dictation_shortcut(self._dictate)
            logger.info(f"Registered global shortcut: {self._dictate.label}")
        except HotKeyError as exc:
            logger.error(f"Global hotkey registration failed: {exc}")
            self._hotkey_error_message = (f"{self._dictate.label} unavailable — choose another shortcut in "
                                          "Settings")
            self._push_status(self._hotkey_error_message)

    # -- What the user asks for --

    def dictation_hotkey_pressed(self) -> None:
        if self.recording_active:
            # The hotkey is a toggle: press again to finish dictating.
            self.stop_recording_requested(hide_after=True)
        elif self.overlay_visible and (self.is_transcribing or not self.config.auto_start_recording):
            # Busy, or set to start from the window: bring the window forward.
            self.overlay_controller.focus()
        elif self.config.auto_start_recording:
            self.start_recording()
        else:
            self.open_transcript_window()

    def open_transcript_window(self, mode: Mode = Mode.RESULT) -> None:
        # Keep the last transcript visible and copyable; only a new
        # recording clears it (start_recording).
        if not self.is_busy:
            self._previous_app = self._paste_target.current()
            # Only invalidate worker sessions when no operation owns the
            # display; bumping mid-recording or mid-transcription would
            # silently discard live drafts and the final result.
            self._session += 1
        # The user explicitly opened the window — don't hide it when the
        # operation in flight finishes.
        self._hide_window_when_done = False
        self._compact_session = False
        self.indicator.hide()
        if self.current_transcript:
            self.overlay_controller.set_current_text(self.current_transcript)
        self.overlay_visible = True
        self.overlay_controller.show_mode(mode)
        if self._phase is Phase.RECORDING:
            self.overlay_controller.show_active_microphone(self._capture_device)
            self._start_drafts_if_wanted()

    def dismiss_requested(self) -> None:
        """Escape, Close, Cmd+W, and the bar's button: finish a recording,
        cancel a transcription, or just close the window, by phase."""
        if self.recording_active:
            self.stop_recording_requested(hide_after=True)
        elif self.is_transcribing:
            self._cancel_event.set()
            self._queue_cancel_event.set()
            self._hide_window_when_done = True
            self._show_status("Cancelling…")
        else:
            self._hide_window()

    def _hide_window(self) -> None:
        self.overlay_visible = False
        self.overlay_controller.hide()

    def toggle_recording_requested(self) -> None:
        if self.recording_active:
            self.stop_recording_requested()
        else:
            self.start_recording()

    def retry_speech_model(self) -> None:
        if self.is_busy:
            self._push_status("Finish the current operation before retrying the model", revert_after=5)
            return
        if self.transcriber.retry_loading():
            self._push_status(self.transcriber.status_message())
            self._start_model_watchdog()
        else:
            self._push_status(self.transcriber.status_message(), revert_after=5)

    def _refresh_preferences(self) -> None:
        if self._preferences_window is not None:
            self._preferences_window.refresh()

    def _show_update_status(self) -> None:
        # Only the update line: a download reports every percent.
        if self._preferences_window is not None:
            self._preferences_window.show_update_status()

    # -- Recording --

    def start_recording(self) -> bool:
        if not self.transcriber.is_ready():
            message = self.transcriber.status_message()
            if self.config.compact_dictation and not self.overlay_visible:
                self._compact_session = True
                self.indicator.show(self._dictate.label)
                self.indicator.finish(message, BAR_SECONDS_AFTER_PROBLEM)
            self._push_status(message)
            return False

        if self.is_transcribing:
            self._push_status("Wait for the current transcription to finish", revert_after=5)
            return False

        if self.recording_active:
            return True

        self._hide_window_when_done = False
        self._previous_app = self._paste_target.current()
        self._session += 1
        self.current_transcript = ""
        self._capture_warning = ""
        self._capture_health = None
        self._capture_at_stop = None
        self._stop_when_connected = False
        self._compact_session = self.config.compact_dictation and not self.overlay_visible
        self._set_phase(Phase.CONNECTING)
        session = self._session
        # The microphone opens first: every millisecond of window drawing
        # ahead of it is speech that would not be captured. The cancel
        # event belongs to this one attempt.
        self._start_cancel = threading.Event()
        self._start_thread = threading.Thread(
            target=self._start_recording_worker, args=(session, self._start_cancel), daemon=True)
        self._start_thread.start()
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()
        self.overlay_controller.prepare_for_recording()
        if self._compact_session:
            self.indicator.show(self._dictate.label)
            self._set_recording_shortcut(True)
        else:
            self.overlay_visible = True
            self.overlay_controller.show_mode(Mode.RESULT)
        self._show_status("Connecting microphone…")
        call_later(10, self._check_microphone_start, session)
        return True

    def _start_recording_worker(self, session: int, cancel: threading.Event) -> None:
        try:
            started = self.recorder.start(cancel)
        except Exception as exc:
            # start() reports failures itself; anything that still escapes
            # must not strand the app in CONNECTING with the stop shortcut held.
            logger.exception("Microphone start raised")
            self.recorder.last_error = exc
            started = False
        if self._shutting_down:
            if started:
                self.recorder.stop()
                self.recorder.preserve_recovery()
            return
        AppHelper.callAfter(self._recording_started, started, session)

    def _recording_started(self, started: bool, session: int) -> None:
        if session != self._session or self._shutting_down:
            return
        if not started:
            self._set_phase(Phase.IDLE)
            self._set_recording_shortcut(False)
            if self._stop_when_connected and self._hide_window_when_done:
                # Esc, Close, or the shortcut cancelled the connection: that
                # closes the window too. A failure keeps it, to be read.
                self._hide_window()
            self._hide_window_when_done = False
            # "Connecting microphone…" must not come back when this message
            # times out.
            self._resting_status = "Ready"
            message = ("Connection cancelled" if self._stop_when_connected
                       else f"Microphone unavailable: {self.recorder.last_error}")
            self._show_status(message, revert_after=8)
            if self._compact_session:
                self.indicator.finish(message, BAR_SECONDS_AFTER_PROBLEM)
            # Launching a process forks this one; keep that off the main thread.
            threading.Thread(target=self._prepare_recorder, daemon=True).start()
            return
        self._set_phase(Phase.RECORDING)
        if self._stop_when_connected:
            self.stop_recording_requested()
            return
        self._capture_device = self.recorder.capture_snapshot().device_name
        self.overlay_controller.show_active_microphone(self._capture_device)
        self._monitor_capture(session)
        # A passive bar needs input levels, not a second model pass whose
        # drafts are never displayed. Expanding the window enables preview.
        if self._phase is Phase.RECORDING and not self._compact_session:
            self._start_drafts_if_wanted()

    def _check_microphone_start(self, session: int) -> None:
        if session == self._session and self._phase is Phase.CONNECTING and not self._shutting_down:
            self._show_status("Resetting microphone connection…")

    def _set_recording_shortcut(self, enabled: bool) -> None:
        if self.hotkey_manager is None:
            return
        session = self._session

        def stop_current_session():
            if session == self._session:
                self.stop_recording_requested()

        try:
            self.hotkey_manager.set_recording_shortcut(stop_current_session if enabled else None)
        except HotKeyError as exc:
            logger.warning(f"{STOP.label} unavailable; use {self._dictate.label}: {exc}")

    def _monitor_capture(self, session: int) -> None:
        if session != self._session or self._phase is not Phase.RECORDING or self._shutting_down:
            return
        snapshot = self.recorder.capture_snapshot()
        if self._compact_session:
            self.indicator.set_capture(snapshot)
        else:
            self.overlay_controller.set_capture(snapshot)
        health = snapshot.health
        if self.recorder.last_error is not None:
            # The audio helper reported a failure or exited: nothing more
            # will arrive, so finish with what was captured.
            health = CaptureHealth.DISCONNECTED
        if snapshot.device_name != self._capture_device:
            # The helper carried the recording over to another input.
            self._capture_device = snapshot.device_name
            self._capture_warning = f"Microphone changed — now using {snapshot.device_name}"
        if health != self._capture_health:
            self._capture_health = health
            self._show_status(_HEALTH_STATUS[health])
        if health in (CaptureHealth.MISSING, CaptureHealth.DISCONNECTED):
            self._capture_warning = "Microphone stopped — recording may be incomplete"
            self.stop_recording_requested()
            return
        call_later(0.15, self._monitor_capture, session)

    def stop_recording_requested(self, hide_after: bool = False) -> None:
        if not self.recording_active:
            return
        if hide_after:
            self._hide_window_when_done = True

        if self._phase is Phase.CONNECTING:
            self._stop_when_connected = True
            self._start_cancel.set()
            self._show_status("Cancelling microphone connection…")
            return

        session = self._session
        self._capture_at_stop = self.recorder.capture_snapshot()
        self._set_recording_shortcut(False)
        self._set_phase(Phase.TRANSCRIBING)
        self._cancel_event.clear()
        self._show_status("Transcribing…")
        self.overlay_controller.set_transcribing(True)
        if self._compact_session:
            self.indicator.set_transcribing()
        # recorder.stop() waits on the audio helper, which can take seconds
        # (or be wedged outright on a Bluetooth route change), so it must
        # never run on the main thread. The worker owns the stop.
        threading.Thread(target=self._transcribe_recording_worker, args=(session,), daemon=True).start()

    # -- Recognition --
    #
    # Qwen3-ASR (when enabled and loaded) handles final passes; Parakeet
    # covers live drafts, the loading window, and any Qwen failure. Qwen has
    # no progress callback, so cancellation is checked before inference; a
    # completed result is always published.

    def _check_cancel(self, current_pos, total_pos) -> None:
        del current_pos, total_pos
        if self._cancel_event.is_set():
            raise TranscriptionCancelled("Cancelled")

    def _vocabulary_hint(self) -> str | None:
        # The high-accuracy model can be told how the user's names and
        # jargon are spelled; the replacement targets are exactly that list.
        return vocabulary_hint(self.config.replacements) if self.config.use_corrections else None

    def _final_pass(self, qwen_pass, parakeet_pass, cancel_event: threading.Event) -> str:
        """Which engine produces the final text. The draft stream must have
        let go of Parakeet's encoder first; if it never does, only Qwen can
        still produce a transcript."""
        encoder_free = self.transcriber.finish_drafts()
        if cancel_event.is_set():
            raise TranscriptionCancelled("Cancelled")
        if self.qwen.is_ready() and (self.config.high_accuracy or not encoder_free):
            try:
                text = qwen_pass()
                if text:
                    return text
                logger.warning("High-accuracy model returned no text; trying the standard model")
            except Exception as exc:
                # Any Qwen failure falls back to the standard engine.
                logger.error(f"High-accuracy transcription failed, falling back: {exc}")
        if cancel_event.is_set():
            raise TranscriptionCancelled("Cancelled")
        if not encoder_free:
            raise TranscriptionError("Transcription engine stalled — restart the app")
        return parakeet_pass()

    def _final_transcribe_pcm(self, pcm_bytes: bytes) -> str:
        return self._final_pass(
            lambda: self.qwen.transcribe_pcm(pcm_bytes, context=self._vocabulary_hint()),
            lambda: self.transcriber.transcribe_pcm(pcm_bytes, progress_callback=self._check_cancel),
            self._cancel_event,
        )

    def _final_transcribe_file(self, path: str, progress_callback, cancel_event: threading.Event) -> str:
        return self._final_pass(
            lambda: self.qwen.transcribe_file(path, context=self._vocabulary_hint()),
            lambda: self.transcriber.transcribe_file(path, progress_callback=progress_callback),
            cancel_event,
        )

    def _settle_spill(self, record) -> bool:
        """After a dictation the audio lives in exactly one place: the
        archive when it was written, otherwise an unsaved recording of its
        own. True when it is kept somewhere."""
        if record is not None:
            self.recorder.discard_recovery()
            return True
        return self.recorder.preserve_recovery()

    def _transcribe_recording_worker(self, session: int) -> None:
        record = None
        diagnostics: dict = {}
        outcome = RecordingStatus.FAILED
        result_text = ""
        raw_text = ""
        result_message = "Transcription did not complete"
        delivered = False
        audio_kept = False
        stop_started = time.monotonic()
        inference_started: float | None = None
        try:
            pcm_bytes = self.recorder.stop()
            if self.recorder.last_error is not None:
                self._capture_warning = "Microphone connection interrupted — received audio was retained"
            snapshot = self._capture_at_stop or self.recorder.capture_snapshot()
            diagnostics = snapshot.diagnostics() | {
                "stop_seconds": time.monotonic() - stop_started,
                "captured_seconds": pcm_seconds(pcm_bytes),
                "audio_worker_resets": self.recorder.reset_count,
                "warm_start": self.recorder.warm_start,
                "active_threads": threading.active_count(),
            }
            # Save before inference, including silent/empty-result captures.
            # A model returning no text must never decide audio retention.
            try:
                record = self.recordings.save(pcm_bytes, diagnostics)
            except Exception as exc:
                logger.error(f"Could not archive recording; keeping recovery spill: {exc}")
            # From here the archive is the copy that counts. Dropping the
            # spill now means a crash during recognition leaves one "not
            # transcribed yet" recording, not that plus a duplicate.
            audio_kept = self._settle_spill(record)
            inference_started = time.monotonic()
            has_signal = any(pcm_bytes)
            if has_signal:
                text = self._final_transcribe_pcm(pcm_bytes)
            else:
                # Nothing to recognize, but a draft stream still has to be
                # stopped or it would follow the next recording.
                self.transcriber.finish_drafts()
                text = ""

            if not text:
                # Clear any leftover live draft: it is display-only and must
                # not outlive a final pass that found no speech.
                self._set_current_text_on_main("", session)
                outcome, result_message = empty_capture_outcome(
                    has_audio=bool(pcm_bytes), has_signal=has_signal,
                    faint=snapshot.faint and snapshot.audio_seconds > 0,
                    cancelled=self._cancel_event.is_set(), audio_kept=audio_kept,
                )
                self._push_status(result_message, revert_after=8)
                return

            # Transcription succeeded — always publish the result even if
            # cancel was requested while inference was running. The cancel
            # only interrupts by raising TranscriptionCancelled; if we got
            # here, the work is done and shouldn't be discarded.
            raw_text = text
            result_text, delivered = self._publish_transcript(text, Source.MICROPHONE, "Live Dictation", session)
            outcome = RecordingStatus.DONE
            result_message = self._capture_warning
            if self._capture_warning:
                self._push_status(self._capture_warning, revert_after=8)
        except TranscriptionCancelled:
            outcome = RecordingStatus.CANCELLED
            result_message = "Cancelled — audio kept for retry" if audio_kept else "Cancelled — could not save audio"
            self._push_status(result_message, revert_after=8)
        except TranscriptionError as exc:
            # A real failure stays one even when Esc was pressed meanwhile.
            logger.error(str(exc))
            result_message = f"{exc} — recording saved (see Recordings)" if audio_kept else str(exc)
            self._push_status(result_message, revert_after=8)
        except Exception:
            # The worker must always hand the UI back; the audio is settled
            # first and the cause is in the log with its traceback.
            logger.exception("Unexpected transcription error")
            if not audio_kept:
                audio_kept = self._settle_spill(record)  # The failure came before the audio was settled.
            result_message = ("Transcription failed — recording saved (see Recordings)"
                              if audio_kept else "Transcription failed unexpectedly")
            self._push_status(result_message, revert_after=8)
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
            self._prepare_recorder()
            quick = outcome is RecordingStatus.DONE and delivered and not self._capture_warning
            AppHelper.callAfter(self._complete_operation_on_main, session,
                                BAR_SECONDS_AFTER_SUCCESS if quick else BAR_SECONDS_AFTER_PROBLEM)

    def _complete_operation_on_main(self, session: int, bar_seconds: float = BAR_SECONDS_AFTER_PROBLEM) -> None:
        if session != self._session or self._shutting_down:
            return
        # Release operation state on the UI thread, after queued result
        # callbacks. A new hotkey cannot race old completion callbacks.
        self._set_phase(Phase.IDLE)
        self.record_menu.title = "Start Dictation"
        self.overlay_controller.set_transcribing(False)
        self.overlay_controller.set_queue_processing(False)
        self._resting_status = "Ready"
        if self._hide_window_when_done:
            self._hide_window_when_done = False
            self._hide_window()
        if self._compact_session:
            self.indicator.finish(self._last_status, bar_seconds)
        self._refresh_recordings_window()

    # -- A single media file --

    def transcribe_file_directly(self, path: str) -> None:
        """Transcribe a single file immediately, bypassing the queue."""
        if not path or not self._can_begin_transcribing():
            return
        session = self._begin_transcribing()
        filename = Path(path).name
        self._show_transcribing_window()
        self._show_status(f"Transcribing {filename}…")
        threading.Thread(target=self._transcribe_file_worker, args=(path, filename, session), daemon=True).start()

    def _can_begin_transcribing(self) -> bool:
        if not self.transcriber.is_ready():
            self._push_status(self.transcriber.status_message())
            return False
        if self.is_busy:
            self._push_status("Finish the current operation first", revert_after=5)
            return False
        return True

    def _show_transcribing_window(self) -> None:
        self._compact_session = False
        self.indicator.hide()
        self.overlay_controller.set_current_text("")
        self.overlay_controller.show_mode(Mode.RESULT)
        self.overlay_controller.set_transcribing(True)

    def _transcribe_file_worker(self, path: str, filename: str, session: int) -> None:
        def _progress(current_pos, total_pos):
            if self._cancel_event.is_set():
                raise TranscriptionCancelled("Cancelled")
            pct = int(current_pos / total_pos * 100) if total_pos > 0 else 0
            self._push_status(f"{filename}: {pct}%")

        try:
            text = self._final_transcribe_file(path, _progress, self._cancel_event)
            if not text:
                self._push_status("Cancelled" if self._cancel_event.is_set() else "No speech detected",
                                  revert_after=5)
                return

            # Same invariant as mic transcription: if transcribe_file returned
            # text, publish it even if cancel was requested mid-inference.
            self._publish_transcript(text, Source.FILE, filename, session)
        except TranscriptionCancelled:
            self._push_status("Cancelled", revert_after=5)
        except TranscriptionError as exc:
            logger.error(str(exc))
            self._push_status(str(exc), revert_after=5)
        except Exception:
            logger.exception(f"Unexpected error transcribing {filename}")
            self._push_status("Transcription failed unexpectedly", revert_after=5)
        finally:
            AppHelper.callAfter(self._complete_operation_on_main, session)

    # -- Recordings: recovery and "Transcribe Again" --

    def recover_last_recording(self) -> None:
        """Transcribe the capture most in need of it: audio that never
        reached the recognizer, else the newest without a transcript, else
        the newest unsaved recording that could not be moved into the
        archive, else simply the newest recording."""
        records = self.recordings.list_recordings()
        candidate = recovery_candidate(records)
        unsaved = recovery.unsaved_recordings(self._support_dir)
        if candidate is not None:
            self.transcribe_recording(candidate.id)
        elif unsaved:
            self.transcribe_recording(unsaved[-1])
        elif records:
            self.transcribe_recording(records[0].id)
        else:
            self._push_status("No recording to recover", revert_after=5)

    def transcribe_recording(self, recording: str | Path) -> None:
        """Transcribe an archived recording (its id) or an unsaved recording
        kept for recovery (its file)."""
        if not self._can_begin_transcribing():
            return
        self._previous_app = self._paste_target.current()
        session = self._begin_transcribing()
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()
        self._show_transcribing_window()
        self._show_status("Transcribing saved recording…")
        threading.Thread(target=self._recover_worker, args=(session, recording), daemon=True).start()

    def _recover_worker(self, session: int, recording: str | Path) -> None:
        try:
            # Loaded here, not on the menu-click (main) thread: an hour of
            # PCM is ~115 MB.
            pcm_bytes = (self.recordings.load_pcm(recording) if isinstance(recording, str)
                         else recovery.load_unsaved(recording))
            if not pcm_bytes:
                self._push_status("No recording to recover", revert_after=5)
                return
            pcm_bytes = whole_samples(pcm_bytes)
            if not any(pcm_bytes):
                raise TranscriptionError("This recording contains digital silence — choose another microphone")
            text = self._final_transcribe_pcm(pcm_bytes)
            if not text:
                cancelled = self._cancel_event.is_set()
                if isinstance(recording, str) and not cancelled:
                    # It has been tried now, so Recover Last Recording moves
                    # on to audio that has not.
                    self.recordings.update(recording, status=RecordingStatus.FAILED,
                                           message="No transcript returned")
                self._push_status("Cancelled" if cancelled
                                  else "No transcript returned — recording kept for retry", revert_after=8)
                return

            published, _ = self._publish_transcript(text, Source.RECOVERY, "Recovered Recording", session)
            if isinstance(recording, Path):
                # The unsaved recording becomes an ordinary one.
                record = self.recordings.save(pcm_bytes)
                if record is None:
                    return
                recovery.discard_unsaved(recording)
                recording_id = record.id
            else:
                recording_id = recording
            self.recordings.update(recording_id, status=RecordingStatus.DONE, text=published, raw_text=text,
                                   message="")
        except TranscriptionCancelled:
            # The audio stays where it is: recovery can be retried.
            self._push_status("Cancelled", revert_after=5)
        except TranscriptionError as exc:
            logger.error(str(exc))
            self._push_status(str(exc), revert_after=5)
        except Exception:
            logger.exception("Unexpected recovery error")
            self._push_status("Recovery failed unexpectedly", revert_after=5)
        finally:
            AppHelper.callAfter(self._complete_operation_on_main, session)

    def _on_qwen_load_failed(self, message: str) -> None:
        del message  # Logged by the transcriber.
        if not self.config.high_accuracy:
            # The user already toggled the setting off while the load was in
            # flight — don't warn about a model they no longer want.
            return
        self._push_status(self.qwen.status_message(), revert_after=8)
        AppHelper.callAfter(self._refresh_preferences)

    # -- The file queue --

    def queue_add_files(self, paths) -> None:
        normalized = [str(Path(path)) for path in paths if path]
        if not normalized:
            return
        self.queue.add_many(normalized)
        self._refresh_queue_on_main()
        if not self.is_busy:
            # Not over a recording's Stop button and live draft, or a run's
            # progress: the files wait in the Queue tab.
            self.open_transcript_window(Mode.QUEUE)

    def queue_remove_file(self, file_id: str) -> None:
        self.queue.remove(file_id)
        self._refresh_queue_on_main()

    def queue_move_file(self, file_id: str, new_index: int) -> None:
        self.queue.move(file_id, new_index)
        self._refresh_queue_on_main()

    def queue_clear_requested(self) -> None:
        self.queue.clear()
        self._refresh_queue_on_main()

    def queue_start_requested(self) -> None:
        if not self._can_begin_transcribing():
            return
        # Files left over from a cancelled run are run again.
        self.queue.requeue_cancelled()
        if self.queue.pending_count() == 0:
            self._push_status("No files waiting in the queue", revert_after=5)
            return

        output_config = self.overlay_controller.show_output_mode_dialog()
        if output_config is None:
            return
        # The dialog runs a nested event loop: the hotkey may have started
        # a dictation while it was open.
        if self.is_busy:
            self._push_status("Finish the current operation first", revert_after=5)
            return

        self._set_phase(Phase.TRANSCRIBING)
        self._compact_session = False
        self.indicator.hide()
        self._queue_cancel_event.clear()
        self._cancel_event.clear()
        session = self._session
        self.overlay_controller.set_queue_processing(True)
        self.overlay_controller.set_transcribing(True)
        self._refresh_queue_on_main()
        self._show_status("Processing queue…")
        threading.Thread(target=self._transcribe_queue_worker, args=(output_config, session), daemon=True).start()

    def _transcribe_queue_worker(self, output_config, session: int) -> None:
        pending = [item for item in self.queue.items() if item.status == QueueStatus.PENDING]
        # Export only items processed in this run; "done" items from earlier
        # runs were already exported and must not be duplicated.
        run_ids = {item.id for item in pending}

        try:
            for index, item in enumerate(pending, start=1):
                if self._queue_cancel_event.is_set():
                    self.queue.set_status(item.id, QueueStatus.CANCELLED)
                    self._refresh_queue_on_main()
                    continue

                self.queue.set_status(item.id, QueueStatus.PROCESSING)
                self._refresh_queue_on_main()
                prefix = f"[{index}/{len(pending)}] " if len(pending) > 1 else ""

                def _progress(current_pos, total_pos, _label=f"{prefix}{item.filename}"):
                    if self._queue_cancel_event.is_set():
                        raise TranscriptionCancelled("Cancelled")
                    pct = int(current_pos / total_pos * 100) if total_pos > 0 else 0
                    self._push_status(f"{_label}: {pct}%")

                self._push_status(f"{prefix}{item.filename}")

                try:
                    text = self._final_transcribe_file(item.path, _progress, self._queue_cancel_event)
                except TranscriptionCancelled:
                    self.queue.set_status(item.id, QueueStatus.CANCELLED)
                    self._refresh_queue_on_main()
                    continue
                except TranscriptionError as exc:
                    self.queue.set_status(item.id, QueueStatus.FAILED, error=str(exc))
                    logger.error(f"Queue item failed: {item.filename}: {exc}")
                    self._refresh_queue_on_main()
                    continue
                except Exception as exc:
                    # One bad file must not stop the rest of the queue.
                    self.queue.set_status(item.id, QueueStatus.FAILED, error=str(exc))
                    logger.exception(f"Queue item error: {item.filename}")
                    self._refresh_queue_on_main()
                    continue

                if not text:
                    self.queue.set_status(item.id, QueueStatus.FAILED, error="No speech detected")
                    self._refresh_queue_on_main()
                    continue

                self.queue.set_status(item.id, QueueStatus.DONE, result_text=text)
                self.history_store.add_entry(Source.FILE, item.filename, text)
                self._refresh_queue_on_main()

            ran = [item for item in self.queue.items() if item.id in run_ids]
            completed = [item for item in ran if item.status == QueueStatus.DONE and item.result_text]
            exported = export_error = None
            if completed:
                try:
                    exported = export_results(completed, output_config)
                    if output_config.mode is OutputMode.CLIPBOARD:
                        self._flash_copy_feedback_on_main()
                except ExportError as exc:
                    logger.error(f"Export failed: {exc}")
                    export_error = str(exc)
            self._push_status(queue_run_summary(
                cancelled=self._queue_cancel_event.is_set(), exported=exported, export_error=export_error,
                any_failed=any(item.status == QueueStatus.FAILED for item in ran),
            ), revert_after=5)
            self._refresh_history_on_main()
        finally:
            self._refresh_queue_on_main()
            AppHelper.callAfter(self._complete_operation_on_main, session)

    def _refresh_queue_on_main(self) -> None:
        AppHelper.callAfter(self.overlay_controller.set_queue_files, self.queue.items())

    # -- Publishing a transcript --

    def _publish_transcript(self, text: str, source: Source, source_label: str, session: int) -> tuple[str, bool]:
        """Put a transcript in history, on screen, and where the settings
        say it should go. Returns the text after word replacements, and
        whether every delivery that was asked for happened."""
        raw_text = text
        if self.config.use_corrections and source in (Source.MICROPHONE, Source.RECOVERY):
            text = apply_replacements(text, self.config.replacements)
        self.current_transcript = text
        self.history_store.add_entry(source, source_label, text, raw_text=raw_text)
        self._set_current_text_on_main(text, session)
        self._refresh_history_on_main()

        # Auto-paste works via Cmd+V, so it requires the clipboard copy
        # regardless of the copy setting. A cancel that arrived too late to
        # stop the transcript still stops it being typed into another app.
        should_paste = (self.config.paste_to_active_app and source is Source.MICROPHONE
                        and not self._cancel_event.is_set())
        if not (self.config.auto_copy_to_clipboard or should_paste):
            self._push_status("Transcript ready", revert_after=5)
            return text, True
        copied = self._copy_text_with_feedback(
            text,
            success_status="Copied transcript to clipboard",
            failure_status="Transcript ready, but clipboard copy failed",
        )
        if copied and should_paste:
            AppHelper.callAfter(self._paste_into_previous_app_on_main, session, text)
        return text, copied

    def copy_current_transcript(self) -> None:
        text = self.current_transcript.strip()
        if not text:
            # Fall back to whatever the window is showing (e.g. the live
            # draft during a recording) — the user is looking right at it.
            text = self.overlay_controller.current_text.strip()
        if not text:
            self._push_status("No transcript to copy", revert_after=5)
            return

        self._copy_text_with_feedback(
            text,
            success_status="Copied transcript to clipboard",
            failure_status="Clipboard copy failed",
        )

    def _copy_text_with_feedback(self, text: str, success_status: str, failure_status: str) -> bool:
        try:
            copy_text(text)
        except ClipboardError as exc:
            logger.error(f"Clipboard error: {exc}")
            self._push_status(failure_status, revert_after=8)
            return False

        self._flash_copy_feedback_on_main()
        self._push_status(success_status, revert_after=5)
        return True

    def _paste_into_previous_app_on_main(self, session: int, expected_text: str) -> None:
        if session != self._session or self._shutting_down:
            return
        if not accessibility_trusted():
            self._push_status("Auto-paste needs Accessibility permission", revert_after=8)
            self.open_accessibility_settings()
            return

        target = self._previous_app
        compact = self._compact_session
        if compact and not self._paste_target.is_frontmost(target):
            self._push_status("Copied — auto-paste skipped (focus changed)", revert_after=8)
            return
        self._hide_window()
        if target is None or target.isTerminated():
            self._push_status("Copied — auto-paste skipped (previous app closed)", revert_after=8)
            return
        if not compact:
            self._paste_target.bring_forward(target)

        def _send_after_focus_returns():
            if session != self._session or self._shutting_down or self._cancel_event.is_set():
                return
            # Never blind-fire Cmd+V: only paste if the app we re-activated
            # actually ended up frontmost (the user may have switched away).
            if not self._paste_target.is_frontmost(target):
                logger.warning("Auto-paste skipped: frontmost app changed")
                self._push_status("Copied — auto-paste skipped (focus changed)", revert_after=8)
                return
            if not contains_text(expected_text):
                self._push_status("Auto-paste skipped — clipboard changed; transcript is in History", revert_after=8)
                return
            try:
                send_paste_keystroke()
            except PasteError as exc:
                logger.error(f"Auto-paste failed: {exc}")
                self._push_status("Auto-paste failed", revert_after=8)

        # Keep the last session/focus checks and dispatch on the UI thread.
        # A delayed callback avoids a sleeping worker per dictation and lets
        # new recording or shutdown events invalidate the pending paste.
        call_later(0 if compact else 0.3, _send_after_focus_returns)

    # -- Settings (called by the Settings window) --

    def toggle_setting(self, name: str) -> None:
        value = not getattr(self.config, name)
        setattr(self.config, name, value)
        self._save_settings()
        if name == "paste_to_active_app" and value and not accessibility_trusted():
            self._push_status("Grant Accessibility access to enable auto-paste", revert_after=8)
            self.open_accessibility_settings()
        elif name == "high_accuracy":
            if value:
                self.qwen.start_loading()
                self._push_status(self.qwen.status_message(), revert_after=8)
            else:
                self.qwen.unload()
                self._push_status("High-accuracy model unloaded", revert_after=5)
        elif name == "prefer_builtin_mic":
            self._microphone_settings_changed()
        elif name == "auto_start_recording":
            self._show_intro_text()  # It says what the shortcut does.
        self._refresh_preferences()

    def replace_word_rules(self, rules: list[dict[str, str]]) -> bool:
        self.config.replacements = rules
        return self._save_settings()

    def select_input_device(self, device_name: str | None) -> None:
        if not self.is_busy:
            self.config.input_device = device_name
            self._save_settings()
            self._microphone_settings_changed()
        # Otherwise the picker is put back: the change was not applied.
        self._refresh_preferences()

    def set_keep_microphone_ready(self, seconds: int) -> None:
        self.config.keep_mic_ready_seconds = seconds
        self._save_settings()
        self._microphone_settings_changed()

    def _microphone_settings_changed(self) -> None:
        self._configure_recorder()
        # A device being kept open was opened under the old settings. A
        # recording in progress picks the new duration up when it stops.
        threading.Thread(target=self.recorder.release_device, daemon=True).start()
        self.refresh_input_devices()

    def refresh_input_devices(self) -> None:
        preferences = self._preferences_window
        if self.is_busy or preferences is None or not preferences.shows_microphones():
            return

        # Device enumeration runs in a helper process (~150 ms); off the
        # main thread so Settings never beachballs.
        def _enumerate():
            devices = self.recorder.list_input_devices()
            if devices is None:
                # Audio session busy — keep the current popup rather than
                # showing a false "no input devices found".
                return
            AppHelper.callAfter(preferences.update_input_devices, devices,
                                self.config.input_device, self.recorder.automatic_device_name)

        threading.Thread(target=_enumerate, daemon=True).start()

    def _save_settings(self) -> bool:
        try:
            self.config.save(self._settings_path)
            return True
        except OSError as exc:
            logger.error(f"Failed to save settings: {exc}")
            self._push_status("Settings could not be saved — check available disk space", revert_after=8)
            return False

    def open_accessibility_settings(self) -> None:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])

    # -- The dictation shortcut (the picker in Settings and Welcome) --

    def current_shortcut(self) -> HotKeySpec:
        return self._dictate

    def choose_shortcut(self, key_code: int, modifiers: int) -> str | None:
        """Make this the dictation shortcut. Returns why it cannot be, or None."""
        problem = shortcut_problem(key_code, modifiers)
        if problem is not None:
            return problem
        spec = dictation_shortcut(key_code, modifiers)
        if self.hotkey_manager is not None:
            try:
                self.hotkey_manager.set_dictation_shortcut(spec)
            except HotKeyError as exc:
                logger.warning(f"Could not register {spec.label}: {exc}")
                self.resume_shortcut()  # The previous one, if recording had paused it.
                return f"{spec.label} is already used by another app. Choose another."
        self._dictate = spec
        self._hotkey_error_message = None
        self.config.dictation_shortcut = [key_code, modifiers]
        self._save_settings()
        logger.info(f"Dictation shortcut is now {spec.label}")
        self._show_intro_text()
        self._refresh_history_on_main()
        self._refresh_preferences()
        if self._welcome_window is not None:
            self._welcome_window.refresh()
        return None

    def pause_shortcut(self) -> None:
        """While new keys are recorded, the current shortcut must not fire."""
        if self.hotkey_manager is not None:
            self.hotkey_manager.set_dictation_shortcut(None)

    def resume_shortcut(self) -> None:
        if self.hotkey_manager is not None:
            try:
                self.hotkey_manager.set_dictation_shortcut(self._dictate)
            except HotKeyError as exc:
                logger.error(f"Could not register {self._dictate.label} again: {exc}")
                self._hotkey_error_message = f"{self._dictate.label} unavailable — choose another shortcut in Settings"
                self._push_status(self._hotkey_error_message)

    # -- Welcome --

    def show_welcome(self) -> None:
        if self._welcome_window is None:
            self._welcome_window = WelcomeController.alloc().initWithDelegate_(self)
        self._welcome_window.show()

    def finish_welcome(self) -> None:
        if not self.config.onboarded:
            self.config.onboarded = True
            self._save_settings()

    def set_paste_into_apps(self, enabled: bool) -> None:
        self.config.paste_to_active_app = enabled
        self._save_settings()
        self._refresh_preferences()

    def paste_permitted(self) -> bool:
        return accessibility_trusted()

    # -- History and recordings windows --

    def show_recordings(self) -> None:
        if self._recordings_window is None:
            self._recordings_window = RecordingsController.alloc().initWithDelegate_store_(self, self.recordings)
        self._recordings_window.show()

    def _refresh_recordings_window(self) -> None:
        if self._recordings_window is not None:
            self._recordings_window.refresh()

    def clear_history_requested(self) -> None:
        if self.is_busy:
            self._push_status("Finish the current operation before clearing history", revert_after=5)
            return
        confirmed = rumps.alert(
            title="Clear History and Recordings?",
            message="This deletes saved transcripts and recording audio from this Mac.",
            ok="Clear", cancel=True,
        ) == 1
        # The alert runs a nested event loop in which the hotkey still
        # works: a dictation started behind it must not have its audio
        # deleted from under it.
        if not confirmed or self.is_busy:
            if confirmed:
                self._push_status("Nothing was cleared: a dictation is in progress", revert_after=8)
            return
        if self._recordings_window is not None:
            self._recordings_window.stop_playback()
        try:
            self.recordings.clear()
        except OSError as exc:
            self._push_status(f"Could not clear recordings: {exc}", revert_after=8)
            return
        self.recorder.discard_recovery()
        recovery.discard_every_unsaved(self._support_dir)
        cleared = self.history_store.clear()
        self.current_transcript = ""
        self.overlay_controller.set_current_text("")
        self._refresh_history_on_main()
        self._refresh_recordings_window()
        self._push_status("History cleared" if cleared else
                          "Audio cleared, but transcript history could not be deleted from disk",
                          revert_after=8)

    def _show_intro_text(self) -> None:
        self.overlay_controller.set_intro_text(intro_text(self._dictate.label, self.config.auto_start_recording))

    def _history_text(self) -> str:
        rendered = self.history_store.render()
        return empty_history_text(self._dictate.label) if rendered is None else rendered

    def _refresh_history_on_main(self) -> None:
        AppHelper.callAfter(self.overlay_controller.set_history_text, self._history_text())

    def _set_current_text_on_main(self, text: str, session: int) -> None:
        AppHelper.callAfter(self._apply_current_text_on_main, text, session)

    def _apply_current_text_on_main(self, text: str, session: int) -> None:
        if session == self._session:
            self.overlay_controller.set_current_text(text)

    def _flash_copy_feedback_on_main(self) -> None:
        AppHelper.callAfter(self.overlay_controller.flash_copy_feedback)

    def _start_drafts_if_wanted(self) -> None:
        if not self.config.live_preview or self._drafts_session == self._session:
            return
        session = self._session
        if self.transcriber.start_drafts(
            frames_provider=lambda: self.recorder.frames,
            on_draft=lambda text: self._set_current_text_on_main(text, session),
        ):
            self._drafts_session = session

    # -- Status line --
    #
    # A status with revert_after is temporary: when it expires the resting
    # status comes back. The token keeps a stale expiry from overwriting a
    # newer message.

    def _push_status(self, message: str, revert_after: float = 0) -> None:
        """From any thread."""
        AppHelper.callAfter(self._show_status, message, revert_after)

    def _show_status(self, message: str, revert_after: float = 0) -> None:
        """Main thread only."""
        self._last_status = message
        self._status_token += 1
        if revert_after == 0:
            self._resting_status = message
        self.status_item.title = f"Status: {message}"
        self.record_menu.title = ("Stop Dictation" if self.recording_active else
                                  "Transcribing…" if self.is_transcribing else "Start Dictation")
        self.overlay_controller.set_status(message)
        self.overlay_controller.set_recording(self.recording_active)
        if self._compact_session:
            if self.indicator.is_finished():
                # An outcome that arrives after the bar finished (for example
                # "auto-paste skipped") gets its own time on screen.
                self.indicator.finish(message, revert_after if revert_after > 0 else BAR_SECONDS_AFTER_PROBLEM)
            else:
                self.indicator.set_status(message)
        if revert_after > 0:
            call_later(revert_after, self._revert_status, self._status_token)

    def _revert_status(self, token: int) -> None:
        if token != self._status_token:
            return
        self.status_item.title = f"Status: {self._resting_status}"
        self.overlay_controller.set_status(self._resting_status)
        if self._compact_session and not self.indicator.is_finished():
            self.indicator.set_status(self._resting_status)

    # -- Shutdown --

    def cleanup(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._cancel_event.set()
        self._queue_cancel_event.set()
        self._start_cancel.set()
        self._paste_target.stop()

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

        # Teardown waits on the audio helper. Leave the main loop responsive
        # and the spill file recoverable even if it never answers.
        closer = threading.Thread(target=close_audio, daemon=True)
        closer.start()
        closer.join(timeout=2)

    # -- Menu --

    @rumps.clicked("Start Dictation")
    def menu_toggle_recording(self, sender):
        del sender
        self.toggle_recording_requested()

    @rumps.clicked("Open Transcript")
    def menu_open_transcript(self, sender):
        del sender
        self.open_transcript_window()

    @rumps.clicked("Recordings…")
    def menu_recordings(self, sender):
        del sender
        self.show_recordings()

    @rumps.clicked("Settings…")
    def menu_settings(self, sender):
        del sender
        if self._preferences_window is None:
            self._preferences_window = PreferencesController.alloc().initWithDelegate_labels_(self, _SETTING_LABELS)
        self._preferences_window.show()

    @rumps.clicked(CHECK_TITLE)
    def menu_check_for_updates(self, sender):
        del sender
        self.updates.check_requested()

    @rumps.clicked("More", "History")
    def menu_show_history(self, sender):
        del sender
        self.open_transcript_window(Mode.HISTORY)

    @rumps.clicked("More", "Open Media Files…")
    def menu_open_files(self, sender):
        del sender
        if not self._can_begin_transcribing():
            return  # Said before the file panel, not after a choice it cannot act on.
        self.open_transcript_window()
        AppHelper.callAfter(self.overlay_controller.openFiles_, None)

    @rumps.clicked("More", "Copy Last Transcript")
    def menu_copy_last(self, sender):
        del sender
        self.copy_current_transcript()

    @rumps.clicked("More", "Recover Last Recording")
    def menu_recover_last(self, sender):
        del sender
        self.recover_last_recording()

    @rumps.clicked("More", "Retry Speech Model")
    def menu_retry_model(self, sender):
        del sender
        self.retry_speech_model()

    @rumps.clicked("More", "Clear History & Recordings…")
    def menu_clear_history(self, sender):
        del sender
        self.clear_history_requested()

    @rumps.clicked("More", "Welcome…")
    def menu_welcome(self, sender):
        del sender
        self.show_welcome()

    @rumps.clicked("Quit")
    def menu_quit(self, sender):
        del sender
        rumps.quit_application()
