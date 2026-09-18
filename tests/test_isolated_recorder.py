"""Real subprocess failure tests with synthetic PCM; never open an audio device."""
import subprocess
import sys
import json
import time

from parakeet_dictation.isolated_recorder import IsolatedAudioRecorder, worker_command
from parakeet_dictation.recovery import in_progress_path


def command(body):
    return [sys.executable, '-u', '-c', 'import sys,json,time,base64\njson.loads(sys.stdin.readline())\n' + body]


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
    for _ in range(8):
        assert recorder.start()
        assert recorder.stop() == b''
        assert recorder._process is None
        assert recorder._reader is None
    recorder.cleanup()


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
    assert recorder.stop() == b'\x01\x00' * 16000
    assert recorder.preserve_recovery()
    recorder.cleanup()
