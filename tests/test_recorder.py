"""The recorder is exercised with a fake backend; no audio device is opened."""

import pytest

from parakeet_dictation import recorder as module


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = True

    def is_active(self):
        return self.active

    def stop_stream(self):
        self.active = False

    def close(self):
        self.active = False

    def start_stream(self):
        raise AssertionError("PyAudio already starts the stream on open")


class FakeAudio:
    devices = ["MacBook Pro Microphone", "AirPods"]
    opens = []

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, index):
        return {"index": index, "name": self.devices[index], "maxInputChannels": 1}

    def get_default_input_device_info(self):
        return self.get_device_info_by_index(1)

    def open(self, **kwargs):
        self.opens.append(kwargs)
        return FakeStream(kwargs["stream_callback"])

    def terminate(self):
        pass


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(module.pyaudio, "PyAudio", FakeAudio)
    monkeypatch.setattr(FakeAudio, "opens", [])
    # The result must not depend on whether this Mac's lid happens to be shut.
    monkeypatch.setattr(module, "lid_closed", lambda: False)
    instance = module.AudioRecorder(recovery_dir=tmp_path)
    yield instance
    instance.cleanup()


def test_automatic_prefers_mac_mic_without_changing_system_default(recorder):
    assert recorder.start()
    assert FakeAudio.opens[-1]["input_device_index"] == 0
    assert recorder.capture_snapshot().device_name == "MacBook Pro Microphone"
    assert recorder.audio.get_default_input_device_info()["name"] == "AirPods"


def test_explicit_airpods_choice_is_respected(recorder):
    recorder.set_device("AirPods")
    assert recorder.start()
    assert FakeAudio.opens[-1]["input_device_index"] == 1


def test_missing_locked_mic_does_not_silently_switch(recorder):
    recorder.set_device("Disconnected Headset")
    assert recorder.start() is False
    assert "disconnected" in str(recorder.last_error)
    assert FakeAudio.opens == []


def test_system_default_remains_an_option(recorder):
    recorder.prefer_builtin = False
    assert recorder.start()
    assert FakeAudio.opens[-1]["input_device_index"] == 1


def test_stop_retains_complete_audio_for_recovery(recorder):
    assert recorder.start()
    pcm = b"\x01\x02" * 16000
    recorder._stream.callback(pcm, 16000, {}, 0)
    assert recorder.stop() == pcm
    assert recorder.preserve_recovery()
    assert recorder.load_recoverable_recording() == pcm


def test_zombie_callback_cannot_write_into_a_new_recording(recorder):
    assert recorder.start()
    old = recorder._stream
    recorder.stop()
    assert recorder.start()
    old.callback(b"\x01\x02" * 512, 512, {}, 0)
    assert recorder.frames == []
    assert recorder.capture_snapshot().callbacks == 0


def test_repeated_driver_failures_require_restart_instead_of_leaking_forever(recorder):
    recorder.abandoned_sessions = 2
    assert recorder.start() is False
    assert "restart" in str(recorder.last_error)
    assert FakeAudio.opens == []


def test_constructing_and_closing_recorder_does_not_initialize_audio(tmp_path, monkeypatch):
    def unexpected():
        pytest.fail("No PortAudio initialization at app startup")

    monkeypatch.setattr(module.pyaudio, "PyAudio", unexpected)
    recorder = module.AudioRecorder(recovery_dir=tmp_path)
    assert recorder.sample_width() == 2
    recorder.cleanup()


def test_repeated_sessions_release_workers_streams_and_spill_handles(recorder):
    previous_callback = None
    for index in range(50):
        assert recorder.start()
        stream = recorder._stream
        worker = recorder._recording_thread
        handle = recorder._recovery_file
        pcm = (index + 1).to_bytes(2, "little") * 512
        if previous_callback is not None:
            previous_callback(b"\xff\x7f" * 512, 512, {}, 0)
        stream.callback(pcm, 512, {}, 0)
        assert recorder.stop() == pcm
        assert not worker.is_alive()
        assert handle.closed
        assert not stream.active
        assert recorder._stream is None
        assert recorder._recovery_file is None
        assert recorder.frames == []
        assert recorder.capture_snapshot().callbacks == 1
        assert recorder.abandoned_sessions == 0
        previous_callback = stream.callback


def test_reopen_continues_the_same_recording_on_the_new_default(recorder, monkeypatch):
    recorder.prefer_builtin = False
    assert recorder.start()
    first = recorder._stream
    first.callback(b"\x01\x00" * 8000, 512, {}, 0)
    monkeypatch.setattr(FakeAudio, "devices", ["MacBook Pro Microphone"])
    monkeypatch.setattr(FakeAudio, "get_default_input_device_info",
                        lambda self: self.get_device_info_by_index(0))
    assert recorder.reopen()
    assert recorder._stream is not first and not first.active
    assert recorder.capture_snapshot().device_name == "MacBook Pro Microphone"
    first.callback(b"\x09\x00" * 512, 512, {}, 0)  # The retired stream is ignored.
    recorder._stream.callback(b"\x02\x00" * 8000, 512, {}, 0)
    assert recorder.stop() == b"\x01\x00" * 8000 + b"\x02\x00" * 8000
    assert recorder.preserve_recovery()
    assert recorder.load_recoverable_recording() == b"\x01\x00" * 8000 + b"\x02\x00" * 8000


def test_reopen_refuses_to_replace_a_locked_microphone(recorder, monkeypatch):
    recorder.set_device("AirPods")
    assert recorder.start()
    recorder._stream.callback(b"\x01\x00" * 512, 512, {}, 0)
    monkeypatch.setattr(FakeAudio, "devices", ["MacBook Pro Microphone"])
    assert recorder.reopen() is False
    assert "disconnected" in str(recorder.last_error)
    assert recorder.stop() == b"\x01\x00" * 512


def test_closed_lid_does_not_prefer_the_switched_off_builtin_microphone(recorder, monkeypatch):
    monkeypatch.setattr(module, "lid_closed", lambda: True)
    assert recorder.start()
    assert FakeAudio.opens[-1]["input_device_index"] == 1
    assert recorder.automatic_device_name() == "AirPods"


def test_rearm_starts_a_fresh_recording_on_the_open_stream(recorder):
    assert recorder.start()
    stream = recorder._stream
    stream.callback(b"\x01\x00" * 512, 512, {}, 0)
    assert recorder.rearm()
    assert recorder._stream is stream and len(FakeAudio.opens) == 1
    stream.callback(b"\x02\x00" * 512, 512, {}, 0)
    assert recorder.stop() == b"\x02\x00" * 512
    assert recorder.rearm() is False


def test_lid_state_is_readable_without_raising():
    assert module.lid_closed() in (True, False)
