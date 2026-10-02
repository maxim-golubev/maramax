"""Real subprocess tests with synthetic PCM; never open an audio device."""
import subprocess
import sys
import json
import time

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
m.get_sample_size = lambda fmt: 2
STATE = {"devices": ["MacBook Pro Microphone", "AirPods"], "default": 1, "opens": 0}
VANISH_AFTER = %(vanish_after)d

class Stream:
    def __init__(self, callback, index):
        self.callback, self.index, self.active = callback, index, True
        self.first = STATE["opens"] == 1
        threading.Thread(target=self._pump, daemon=True).start()
    def _pump(self):
        sent = 0
        while self.active:
            if VANISH_AFTER and self.first and sent >= VANISH_AFTER:
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
import parakeet_dictation.recorder
parakeet_dictation.recorder.lid_closed = lambda: False  # Independent of this Mac's real lid.
from parakeet_dictation.audio_worker import main
main()
'''


def helper(vanish_after=0):
    return [sys.executable, '-u', '-c', FAKE_AUDIO % {"vanish_after": vanish_after}]


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
    recorder = IsolatedAudioRecorder(tmp_path, command=command('time.sleep(60)'), start_timeout=.15)
    before = time.monotonic()
    assert not recorder.start()
    assert time.monotonic() - before < 3
    assert recorder._process is None
    assert recorder.reset_count == 1
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


def test_connection_can_be_cancelled_before_start(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=command('time.sleep(60)'))
    recorder.cancel_start()
    before = time.monotonic()
    assert not recorder.start()
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
    recorder = IsolatedAudioRecorder(tmp_path, command=command(body), stop_timeout=.15)
    assert recorder.start()
    expected = b'\x01\x00' * (16000 * 300)
    deadline = time.monotonic() + 10
    while recorder.capture_snapshot().audio_seconds < 300 and time.monotonic() < deadline:
        time.sleep(.01)
    assert in_progress_path(tmp_path).read_bytes() == expected
    assert recorder.stop() == expected
    assert recorder.reset_count == 1
    assert recorder.preserve_recovery()
    assert recorder.load_recoverable_recording() == expected
    recorder.cleanup()


def test_helper_crash_retains_received_audio(tmp_path):
    body = '''
print(json.dumps({"event":"ready","device":"Fake"}),flush=True)
time.sleep(.1)
print(json.dumps({"event":"audio","pcm":base64.b64encode(b'\\x01\\x00' * 16000).decode()}),flush=True)
'''
    recorder = IsolatedAudioRecorder(tmp_path, command=command(body))
    assert recorder.start()
    recorder._reader.join(timeout=3)
    assert recorder.last_error is not None
    before = time.monotonic()
    assert recorder.stop() == b'\x01\x00' * 16000
    assert time.monotonic() - before < 1  # A dead helper is not waited for.
    assert recorder.preserve_recovery()
    recorder.cleanup()


def test_cancel_that_arrives_after_the_start_cannot_abort_the_next_recording(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper())
    assert recorder.start()
    recorder.cancel_start()  # The app's cancel raced a start that had already succeeded.
    recorder.stop()
    assert recorder.start()
    assert recorder.last_error is None
    recorder.stop()
    recorder.cleanup()


def test_standby_helper_is_reused_and_only_opens_audio_on_request(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(), prefer_builtin=False)
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
    recorder = IsolatedAudioRecorder(tmp_path, command=helper())
    assert recorder.start()
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    at_stop = recorder.capture_snapshot().audio_seconds
    pcm = recorder.stop()
    # The fake device runs faster than real time; 0.2 s of wall-clock tail
    # is well over 0.2 s of its audio.
    assert len(pcm) / 32000 - at_stop > 0.2
    recorder.cleanup()


def test_kept_warm_microphone_starts_instantly_then_lets_go(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(), prefer_builtin=False)
    recorder.keep_warm_seconds = 0.6
    assert recorder.start()
    assert not recorder.warm_start
    recorder.stop()
    before = time.monotonic()
    assert recorder.start()
    assert time.monotonic() - before < 0.5
    assert recorder.warm_start
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    first = recorder.stop()
    assert first and set(first[::2]) == {2}
    # Audio heard while waiting never reaches the app.
    recorder._accepting.clear()
    assert wait_for(recorder._accepting.is_set, timeout=3)  # "idle" after the window closes
    assert recorder.frames == []
    assert recorder.start()
    assert not recorder.warm_start
    recorder.stop()
    recorder.cleanup()


def test_changing_microphone_does_not_reuse_a_warm_stream(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(), prefer_builtin=False)
    recorder.keep_warm_seconds = 5
    assert recorder.start()
    recorder.stop()
    recorder.set_device("MacBook Pro Microphone")
    assert recorder.start()
    assert not recorder.warm_start
    assert recorder.capture_snapshot().device_name == "MacBook Pro Microphone"
    assert wait_for(lambda: recorder.capture_snapshot().audio_seconds > 0.05)
    pcm = recorder.stop()
    assert set(pcm[::2]) == {1}
    recorder.cleanup()


def test_recording_continues_on_another_input_when_the_device_vanishes(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(vanish_after=40), prefer_builtin=False)
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


def test_locked_microphone_that_vanishes_does_not_switch_devices(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(vanish_after=40))
    recorder.set_device("AirPods")
    assert recorder.start()
    assert wait_for(lambda: recorder.capture_snapshot().health == "disconnected", timeout=12)
    pcm = recorder.stop()
    assert pcm and set(pcm[::2]) == {2}
    recorder.cleanup()


def test_device_listing_reports_what_automatic_would_use(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper(), prefer_builtin=False)
    devices = recorder.list_input_devices()
    assert [device.name for device in devices] == ["MacBook Pro Microphone", "AirPods"]
    assert recorder.automatic_device_name == "AirPods"
    recorder.prefer_builtin = True
    recorder.list_input_devices()
    assert recorder.automatic_device_name == "MacBook Pro Microphone"
    assert recorder._process is None  # Listing never consumes the standby helper.
    recorder.cleanup()


def test_standby_helper_that_was_killed_is_replaced_without_failing_the_recording(tmp_path):
    recorder = IsolatedAudioRecorder(tmp_path, command=helper())
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
    recorder = IsolatedAudioRecorder(tmp_path, command=command(body))
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
