"""Real subprocess tests with synthetic PCM; never open an audio device."""
import logging
import subprocess
import sys
import json
import threading
import time

from parakeet_dictation import audio_worker, recovery
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.isolated_recorder import IsolatedAudioRecorder, worker_command
from parakeet_dictation.recovery import in_progress_path


def command(body):
    return [sys.executable, '-u', '-c', 'import sys,json,time,base64\njson.loads(sys.stdin.readline())\n' + body]


# The real helper with PortAudio replaced: every opened stream delivers a
# constant sample that names the device it was opened on.
FAKE_AUDIO = r'''
import sys, threading, time, types
m = types.ModuleType("pyaudio")
m.paInt16, m.paContinue, m.paComplete, m.paInputOverflow = 8, 0, 1, 2
STATE = {"devices": ["MacBook Pro Microphone", "AirPods"], "default": 1, "opens": 0}
VANISH_AFTER = %(vanish_after)d   # buffers the first stream delivers before its device disappears
MUTE_FIRST = %(mute_first)d       # the first stream opens but never delivers
DEFAULT_MOVES = %(default_moves)d # the system default input changes before the second request

class Stream:
    def __init__(self, callback, index):
        self.callback, self.index, self.active = callback, index, True
        self.first = STATE["opens"] == 1
        threading.Thread(target=self._pump, daemon=True).start()
    def _pump(self):
        sent = 0
        while self.active:
            if self.first and (MUTE_FIRST or (VANISH_AFTER and sent >= VANISH_AFTER)):
                if VANISH_AFTER:
                    STATE["devices"], STATE["default"] = ["MacBook Pro Microphone"], 0
                time.sleep(0.01)
                continue
            sample = bytes([self.index + 1, 0])
            if self.callback(sample * 512, 512, {}, 0)[1] == m.paComplete:
                break
            sent += 1
            time.sleep(0.004)
    def is_active(self): return self.active
    def stop_stream(self): self.active = False
    def close(self): self.active = False

class PyAudio:
    def get_device_count(self): return len(STATE["devices"])
    def get_device_info_by_index(self, i):
        return {"index": i, "name": STATE["devices"][i], "maxInputChannels": 1}
    def get_default_input_device_info(self): return self.get_device_info_by_index(STATE["default"])
    def open(self, **kw):
        STATE["opens"] += 1
        return Stream(kw["stream_callback"], kw["input_device_index"])
    def terminate(self): pass

m.PyAudio = PyAudio
sys.modules["pyaudio"] = m
import parakeet_dictation.recorder as recorder
recorder.lid_closed = lambda: False  # Independent of this Mac's real lid,
recorder.builtin_input_names = lambda: {"MacBook Pro Microphone"}  # and of its microphones.
REQUESTS = []

def default_input_device():  # What CoreAudio would say; PortAudio's copy is STATE.
    REQUESTS.append(True)
    if DEFAULT_MOVES and len(REQUESTS) == 2:
        STATE["default"] = 0
    return STATE["default"]

recorder.default_input_device = default_input_device
if %(leaks_session)d:
    # What a wedged close leaves behind: PortAudio could not be shut down.
    released = recorder.AudioRecorder.cleanup
    recorder.AudioRecorder.cleanup = lambda self: released(self) and False
from parakeet_dictation.audio_worker import main
main()
'''


def helper(vanish_after=0, mute_first=False, leaks_session=False, default_moves=False):
    return [sys.executable, '-u', '-c', FAKE_AUDIO % {
        "vanish_after": vanish_after, "mute_first": int(mute_first), "leaks_session": int(leaks_session),
        "default_moves": int(default_moves)}]


def recorder_for(tmp_path, cmd, device=None, prefer_builtin=True, **kwargs):
    recorder = IsolatedAudioRecorder(tmp_path, command=cmd, **kwargs)
    recorder.device_name = device
    recorder.prefer_builtin = prefer_builtin
    return recorder


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(.01)
    return condition()


def test_worker_ping_without_gui_or_audio():
    result = subprocess.run(worker_command(), input='{"operation":"ping"}\n', text=True,
                            capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout)['event'] == 'pong'


def test_stalled_start_is_bounded_and_next_attempt_works(tmp_path):
    recorder = recorder_for(tmp_path, command('time.sleep(60)'), start_timeout=.15)
    before = time.monotonic()
    assert not recorder.start()
    assert time.monotonic() - before < 3
    assert recorder._process is None
    assert recorder.reset_count == 1
    recorder._start_timeout = 5  # Generous: these are real interpreter launches.
    recorder._command = command('print(json.dumps({"event":"ready","device":"Fake"}),flush=True)\nsys.stdin.readline()\nprint(json.dumps({"event":"done"}),flush=True)')
    helpers = []
    for _ in range(8):
        assert recorder.start()
        helpers.append(recorder._process)
        assert recorder.stop() == b''
        assert not recorder.recording
    recorder.cleanup()
    assert recorder._process is None
    assert recorder._reader is None
    assert all(process.poll() is not None for process in helpers)
    assert recorder.reset_count == 1  # Helpers that finished on their own were not forced.


def test_cancel_belongs_to_one_attempt_and_cannot_leak_into_the_next(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    cancelled = threading.Event()
    cancelled.set()
    before = time.monotonic()
    assert not recorder.start(cancelled)
    assert time.monotonic() - before < 1
    assert 'cancelled' in str(recorder.last_error)
    assert recorder._process is None  # Cancelled before any helper was involved.
    attempt = threading.Event()
    assert recorder.start(attempt)    # A later attempt is unaffected.
    assert recorder.last_error is None
    attempt.set()  # A cancel for a start that already succeeded changes nothing afterwards.
    recorder.stop()
    assert recorder.start()
    recorder.stop()
    recorder.cleanup()


def test_cancel_during_a_slow_open_returns_promptly(tmp_path):
    recorder = recorder_for(tmp_path, command('time.sleep(60)'))
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    before = time.monotonic()
    assert not recorder.start(cancel)
    assert time.monotonic() - before < 3
    assert 'cancelled' in str(recorder.last_error)
    recorder.cleanup()


def test_five_minutes_retained_when_native_stop_hangs(tmp_path):
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
chunk = base64.b64encode(b'\\x01\\x00' * 16000).decode()
for _ in range(300):
    print(json.dumps({"event":"audio","pcm":chunk}),flush=True)
sys.stdin.readline()
time.sleep(60)
'''
    recorder = recorder_for(tmp_path, command(body), stop_timeout=.15)
    assert recorder.start()
    expected = b'\x01\x00' * (16000 * 300)
    # The spill is written after the meter is fed, so wait on the file itself.
    assert wait_for(lambda: in_progress_path(tmp_path).stat().st_size == len(expected), timeout=10)
    assert in_progress_path(tmp_path).read_bytes() == expected
    assert recorder.stop() == expected
    assert recorder.reset_count == 1
    assert recorder.preserve_recovery()
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == [expected]
    recorder.cleanup()


def test_a_stop_timeout_keeps_the_audio_already_in_the_pipe(tmp_path):
    """The helper sent the tail but never DONE, and the app's reader was
    behind (here: held up by the data lock, as a stalled disk write would).
    Retiring the helper must not drop what is already in the pipe."""
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
print(json.dumps({"event":"audio","pcm":base64.b64encode(b'\\x01\\x00' * 1600).decode()}),flush=True)
sys.stdin.readline()
for _ in range(3):
    print(json.dumps({"event":"audio","pcm":base64.b64encode(b'\\x02\\x00' * 1600).decode()}),flush=True)
time.sleep(60)
'''
    recorder = recorder_for(tmp_path, command(body), stop_timeout=.15)
    assert recorder.start()
    assert wait_for(lambda: len(recorder.frames) == 1)
    recorder._data_lock.acquire()
    threading.Timer(0.6, recorder._data_lock.release).start()
    pcm = recorder.stop()
    assert pcm == b'\x01\x00' * 1600 + b'\x02\x00' * 4800, len(pcm)
    assert recorder.reset_count == 1 and "stopped responding" in str(recorder.last_error)
    recorder.cleanup()


def test_a_capture_that_could_not_be_set_aside_is_never_truncated(tmp_path, monkeypatch):
    in_progress_path(tmp_path).write_bytes(b'\x05\x00' * 16000)
    monkeypatch.setattr(recovery, "promote_in_progress", lambda base_dir: False)   # The rename failed.
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
print(json.dumps({"event":"audio","pcm":base64.b64encode(b'\\x01\\x00' * 1600).decode()}),flush=True)
sys.stdin.readline()
print(json.dumps({"event":"done"}),flush=True)
time.sleep(60)
'''
    recorder = recorder_for(tmp_path, command(body))
    assert recorder.start()
    assert recorder.stop() == b'\x01\x00' * 1600    # This capture is kept in memory instead.
    assert in_progress_path(tmp_path).read_bytes() == b'\x05\x00' * 16000
    recorder.discard_recovery()                       # This capture was archived...
    assert in_progress_path(tmp_path).read_bytes() == b'\x05\x00' * 16000  # ...the earlier one stays.
    monkeypatch.undo()                                # The rename works again.
    recorder.discard_recovery()
    assert not in_progress_path(tmp_path).exists()
    assert [recovery.load_unsaved(path) for path in recovery.unsaved_recordings(tmp_path)] == [b'\x05\x00' * 16000]
    recorder.cleanup()


def test_an_archived_capture_drops_its_own_spill(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    assert recorder.start()
    assert wait_for(lambda: in_progress_path(tmp_path).stat().st_size > recovery.MIN_RECOVERABLE_BYTES)
    recorder.stop()
    recorder.discard_recovery()
    assert not in_progress_path(tmp_path).exists()
    assert recovery.unsaved_recordings(tmp_path) == []
    recorder.cleanup()


def test_helper_crash_retains_received_audio(tmp_path):
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
time.sleep(.1)
print(json.dumps({"event":"audio","pcm":base64.b64encode(b'\\x01\\x00' * 16000).decode()}),flush=True)
'''
    recorder = recorder_for(tmp_path, command(body))
    assert recorder.start()
    recorder._reader.join(timeout=3)
    assert recorder.last_error is not None
    before = time.monotonic()
    assert recorder.stop() == b'\x01\x00' * 16000
    assert time.monotonic() - before < 1  # A dead helper is not waited for.
    assert recorder.preserve_recovery()
    recorder.cleanup()


def test_disk_error_while_spilling_does_not_end_the_recording(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    assert recorder.start()
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)

    class FullDisk:
        def write(self, _pcm):
            raise OSError(28, "No space left on device")

        def close(self):
            raise OSError(28, "No space left on device")

    with recorder._data_lock:
        recorder._spill = FullDisk()
    before = recorder.capture_snapshot().audio_seconds
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > before + 0.2)
    assert recorder._spill is None and recorder._reader.is_alive()
    assert recorder.stop()
    assert recorder.last_error is None
    recorder.cleanup()


def test_standby_helper_is_reused_and_only_opens_audio_on_request(tmp_path):
    recorder = recorder_for(tmp_path, helper(), prefer_builtin=False)
    recorder.prepare()
    standby = recorder._process
    assert standby is not None and standby.poll() is None
    for _ in range(3):
        assert recorder.start()
        assert recorder._process is standby
        assert not recorder.warm_start
        assert recorder.capture_snapshot().device_name == "AirPods"
        assert recorder.capture_snapshot().open_delay is not None
        assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.1)
        pcm = recorder.stop()
        assert pcm and set(pcm[::2]) == {2} and not any(pcm[1::2])
        assert recorder.last_error is None
    assert wait_for(recorder._accepting.is_set)
    assert recorder.reset_count == 0
    recorder.cleanup()
    assert standby.poll() is not None


def test_stop_keeps_the_tail_spoken_while_the_key_was_pressed(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    assert recorder.start()
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    at_stop = recorder.capture_snapshot().audio_seconds
    pcm = recorder.stop()
    # The fake device runs about eight times faster than real time, so the
    # 0.2 s wall-clock tail is over a second of its audio; a tail shortened
    # to a few polls would be a fraction of that.
    assert len(pcm) / 32000 - at_stop > 1.0
    recorder.cleanup()


def test_kept_warm_microphone_starts_instantly_then_lets_go(tmp_path):
    recorder = recorder_for(tmp_path, helper(), prefer_builtin=False)
    recorder.keep_warm_seconds = 0.6
    assert recorder.start()
    assert not recorder.warm_start
    recorder.stop()
    before = time.monotonic()
    assert recorder.start()
    assert time.monotonic() - before < 0.5
    assert recorder.warm_start
    assert recorder.capture_snapshot().open_delay is not None
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    first = recorder.stop()
    assert first and set(first[::2]) == {2}
    assert wait_for(recorder._accepting.is_set)  # Warm: ready for the next request.
    time.sleep(1.0)                              # The 0.6 s window ends.
    assert recorder.frames == []                 # Audio heard while waiting never reached the app.
    assert recorder._process.poll() is None      # The same helper is now idle,
    assert recorder.start()
    assert not recorder.warm_start               # and opens the device afresh.
    recorder.stop()
    recorder.cleanup()


def test_turning_keep_warm_off_applies_to_the_recording_in_progress(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    recorder.keep_warm_seconds = 30
    assert recorder.start()
    recorder.keep_warm_seconds = 0  # Changed in Settings mid-dictation.
    recorder.stop()
    assert wait_for(recorder._accepting.is_set)
    assert recorder.start()
    assert not recorder.warm_start
    recorder.stop()
    recorder.cleanup()


def test_release_closes_a_microphone_that_is_being_kept_warm(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    recorder.keep_warm_seconds = 30
    assert recorder.start()
    recorder.stop()
    assert wait_for(recorder._accepting.is_set)
    helper_process = recorder._process
    recorder.release_device()
    assert recorder.start()  # Requests are handled in order: the release comes first.
    assert not recorder.warm_start
    assert recorder._process is helper_process
    recorder.stop()
    recorder.cleanup()


def test_changing_microphone_does_not_reuse_a_warm_stream(tmp_path):
    recorder = recorder_for(tmp_path, helper(), prefer_builtin=False)
    recorder.keep_warm_seconds = 5
    assert recorder.start()
    recorder.stop()
    recorder.device_name = "MacBook Pro Microphone"
    assert recorder.start()
    assert not recorder.warm_start
    assert recorder.capture_snapshot().device_name == "MacBook Pro Microphone"
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    pcm = recorder.stop()
    assert set(pcm[::2]) == {1}
    recorder.cleanup()


def test_automatic_follows_a_new_default_input_instead_of_a_warm_stream(tmp_path):
    recorder = recorder_for(tmp_path, helper(default_moves=True), prefer_builtin=False)
    recorder.keep_warm_seconds = 5
    assert recorder.start()
    assert recorder.capture_snapshot().device_name == "AirPods"
    recorder.stop()
    assert wait_for(recorder._accepting.is_set)  # Kept warm on AirPods.
    assert recorder.start()                      # The user picked another input meanwhile.
    assert not recorder.warm_start
    assert recorder.capture_snapshot().device_name == "MacBook Pro Microphone"
    recorder.stop()
    recorder.cleanup()


def test_recording_continues_on_another_input_when_the_device_vanishes(tmp_path):
    recorder = recorder_for(tmp_path, helper(vanish_after=40), prefer_builtin=False)
    assert recorder.start()
    assert recorder.capture_snapshot().device_name == "AirPods"
    assert wait_for(lambda: recorder.capture_snapshot().device_name == "MacBook Pro Microphone", timeout=6)
    before = recorder.capture_snapshot().audio_seconds
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > before + 0.2)
    assert recorder.capture_snapshot().health == "receiving"
    pcm = recorder.stop()
    assert recorder.last_error is None
    assert set(pcm[::2]) == {1, 2}  # Both devices' audio, in one recording.
    assert pcm[:2] == b'\x02\x00' and pcm[-2:] == b'\x01\x00'
    recorder.cleanup()


def test_stream_that_never_delivers_is_replaced_before_the_app_gives_up(tmp_path):
    recorder = recorder_for(tmp_path, helper(mute_first=True), prefer_builtin=False)
    assert recorder.start()
    healths = set()

    def delivering():
        healths.add(recorder.capture_snapshot().health)
        return recorder.capture_snapshot().audio_seconds > 0.2

    assert wait_for(delivering, timeout=8)
    assert "missing" not in healths and "disconnected" not in healths
    assert recorder.stop()
    assert recorder.last_error is None
    recorder.cleanup()


def test_locked_microphone_that_vanishes_ends_the_recording_without_switching(tmp_path):
    recorder = recorder_for(tmp_path, helper(vanish_after=40), device="AirPods")
    assert recorder.start()
    before = time.monotonic()
    assert wait_for(lambda: recorder.last_error is not None, timeout=6)
    assert time.monotonic() - before < 4  # Reported once the reopen fails, not after a dead wait.
    assert "no other input" in str(recorder.last_error)
    assert "Selected microphone disconnected: AirPods" in str(recorder.last_error)  # The helper's reason.
    pcm = recorder.stop()
    assert pcm and set(pcm[::2]) == {2}
    recorder.cleanup()


def test_helper_whose_audio_session_leaked_is_not_used_again(tmp_path):
    recorder = recorder_for(tmp_path, helper(leaks_session=True))
    assert recorder.start()
    poisoned = recorder._process
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    assert recorder.stop()
    assert wait_for(lambda: poisoned.poll() is not None)  # It left rather than record from a stale device list.
    assert recorder.start()
    assert recorder._process is not poisoned
    assert recorder.reset_count == 0
    assert recorder.stop()
    recorder.cleanup()


def test_device_listing_reports_what_automatic_would_use(tmp_path):
    recorder = recorder_for(tmp_path, helper(), prefer_builtin=False)
    assert recorder.list_input_devices() == ["MacBook Pro Microphone", "AirPods"]
    assert recorder.automatic_device_name == "AirPods"
    recorder.prefer_builtin = True
    recorder.list_input_devices()
    assert recorder.automatic_device_name == "MacBook Pro Microphone"
    assert recorder._process is None  # Listing never consumes the standby helper.
    recorder.cleanup()


def test_helper_that_survives_being_killed_does_not_strand_the_lock(tmp_path):
    recorder = recorder_for(tmp_path, helper())

    class Unkillable:
        pid = 1
        stdin = stdout = None

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("helper", timeout)

    recorder._kill(Unkillable())  # Logged, not raised.
    assert recorder._operation_lock.acquire(blocking=False)
    recorder._operation_lock.release()
    recorder.cleanup()


def test_helper_whose_output_closes_just_before_it_exits_is_still_replaced(tmp_path):
    marker = tmp_path / "first-attempt"
    body = f'''
import os
if not os.path.exists({str(marker)!r}):
    open({str(marker)!r}, "w").close()
    os.close(1)       # The reader sees the end of output...
    time.sleep(0.2)   # ...before the exit status exists.
    os._exit(0)
print(json.dumps({{"event":"ready","device":"Fake"}}),flush=True)
sys.stdin.readline()
print(json.dumps({{"event":"done"}}),flush=True)
'''
    recorder = recorder_for(tmp_path, command(body))
    assert recorder.start()
    assert recorder.last_error is None and recorder.reset_count == 0
    recorder.stop()
    recorder.cleanup()


def test_slow_to_die_helper_does_not_close_the_recorder_for_good(tmp_path):
    recorder = recorder_for(tmp_path, helper())

    class Lingering:
        pid = 1
        stdin = stdout = None

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("helper", timeout)

    release = threading.Event()
    recorder._process = Lingering()
    recorder._reader = threading.Thread(target=release.wait, daemon=True)  # Waiting on its pipe, holding no lock.
    recorder._reader.start()
    with recorder._operation_lock:
        recorder._retire_process()
    release.set()
    assert not recorder._closed
    assert recorder.start()  # A fresh helper; the old reader was simply left behind.
    recorder.stop()
    recorder.cleanup()


def test_standby_helper_that_was_killed_is_replaced_without_failing_the_recording(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    recorder.prepare()
    dead = recorder._process
    dead.kill()
    dead.wait(timeout=2)
    assert recorder.start()
    assert recorder._process is not dead
    assert recorder.last_error is None
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    assert recorder.stop()
    recorder.cleanup()


def test_helper_that_exits_on_the_request_gets_one_fresh_attempt(tmp_path):
    marker = tmp_path / "first-attempt"
    body = f'''
import os
if not os.path.exists({str(marker)!r}):
    open({str(marker)!r}, "w").close()
    sys.exit(0)
print(json.dumps({{"event":"ready","device":"Fake"}}),flush=True)
sys.stdin.readline()
print(json.dumps({{"event":"done"}}),flush=True)
'''
    recorder = recorder_for(tmp_path, command(body))
    before = time.monotonic()
    assert recorder.start()
    assert time.monotonic() - before < 3
    assert marker.exists()
    recorder.stop()
    marker.unlink()
    recorder._command = command('sys.exit(0)')  # Every attempt dies: report it, don't hang.
    before = time.monotonic()
    assert not recorder.start()
    assert time.monotonic() - before < 3
    assert "unexpectedly" in str(recorder.last_error)
    recorder.cleanup()


def test_the_helpers_own_diagnostics_reach_the_app_log(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="maramax")
    recorder = recorder_for(tmp_path, helper(vanish_after=40), device="AirPods")
    assert recorder.start()
    pid = recorder._process.pid
    # Logged only inside the helper, by the reopen that failed.
    assert wait_for(lambda: f"Audio helper {pid}: ERROR Microphone could not be reopened" in caplog.text)
    recorder.stop()
    recorder.cleanup()


def test_a_listing_the_helper_refused_says_why(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="maramax")
    recorder = recorder_for(tmp_path, command(
        'print(json.dumps({"event":"error","message":"PortAudio enumeration failed"}),flush=True)'))
    assert recorder.list_input_devices() is None
    assert "Could not list microphones: PortAudio enumeration failed" in caplog.text
    recorder._command = command('sys.exit(0)')
    assert recorder.list_input_devices() is None
    assert "exited without answering" in caplog.text
    recorder.cleanup()


def test_a_helper_whose_output_cannot_be_read_is_not_used_again(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="maramax")
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
sys.stdin.readline()
print(json.dumps({"event":"done"}),flush=True)
print(json.dumps({"event":"idle"}),flush=True)
print("not a message",flush=True)
time.sleep(60)
'''
    recorder = recorder_for(tmp_path, command(body))
    assert recorder.start()
    unreadable = recorder._process
    recorder.stop()
    assert wait_for(lambda: f"Audio helper {unreadable.pid} reader stopped" in caplog.text)
    assert not recorder._accepting.is_set()
    before = time.monotonic()
    assert recorder.start()  # A fresh helper, not a wait for an answer nobody reads.
    assert time.monotonic() - before < 3
    assert recorder._process is not unreadable and recorder.reset_count == 1
    recorder.stop()
    recorder.cleanup()


def test_the_helper_keeps_no_copy_of_audio_it_has_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(audio_worker, "send", lambda kind, **fields: sent.append(fields["pcm"]))

    class Recorder:
        frames = [b"\x01\x00", b"\x02\x00"]

        def capture_snapshot(self):
            return CaptureMeter().snapshot()

    recorder = Recorder()
    audio_worker.AudioHelper._forward(recorder)
    assert recorder.frames == []
    recorder.frames.append(b"\x03\x00")
    audio_worker.AudioHelper._forward(recorder)
    audio_worker.AudioHelper._forward(recorder)  # Nothing new: nothing sent.
    assert sent == ["AQACAA==", "AwA="]


def test_a_busy_listing_is_an_error_not_an_empty_list(monkeypatch):
    sent = []
    monkeypatch.setattr(audio_worker, "send", lambda kind, **fields: sent.append((kind, fields)))
    monkeypatch.setattr(audio_worker.AudioRecorder, "list_input_devices", lambda self: None)
    lister = object.__new__(audio_worker.AudioHelper)  # No request reader: stdin is pytest's.
    lister.recorder = None
    lister._warm_key = None
    lister._list({"prefer_builtin": True})
    assert sent == [(audio_worker.Event.ERROR, {"message": "the audio session was busy"})]


def test_a_stream_failover_reopened_is_not_kept_warm_for_the_device_it_replaced(tmp_path):
    recorder = recorder_for(tmp_path, helper(mute_first=True), prefer_builtin=False)
    recorder.keep_warm_seconds = 5
    assert recorder.start()
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.2, timeout=8)  # On a reopened stream.
    recorder.stop()
    assert wait_for(recorder._accepting.is_set)
    assert recorder.start()
    assert not recorder.warm_start              # Opened afresh for what the request names.
    recorder.stop()
    recorder.cleanup()


def test_quitting_mid_dictation_keeps_a_capture_that_had_no_spill_of_its_own(tmp_path, monkeypatch):
    in_progress_path(tmp_path).write_bytes(b"\x09\x00" * 16000)             # An earlier capture, still not set aside.
    monkeypatch.setattr(recovery, "promote_in_progress", lambda base: False)
    recorder = recorder_for(tmp_path, helper())
    assert recorder.start() and not recorder.spill_holds_capture
    wait_for(lambda: sum(map(len, recorder.frames)) > 64000)
    recorder.cleanup()                                                      # The app quits.
    kept = recovery.unsaved_recordings(tmp_path)
    assert len(kept) == 1 and kept[0].stat().st_size > 64000                # Written from memory.
    assert in_progress_path(tmp_path).read_bytes() == b"\x09\x00" * 16000   # The earlier one is untouched.


def test_a_spill_that_does_not_close_does_not_cost_the_audio(tmp_path):
    recorder = recorder_for(tmp_path, helper())
    try:
        assert recorder.start()
        wait_for(lambda: sum(map(len, recorder.frames)) > 16000)

        class Unclosable:
            def close(self):
                raise OSError(5, "Input/output error")
        recorder._spill.close()
        recorder._spill = Unclosable()
        assert len(recorder.stop()) > 16000
    finally:
        recorder.cleanup()
