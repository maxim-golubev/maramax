import json

from parakeet_dictation.config import AppConfig


def test_defaults_when_file_missing(tmp_path):
    config = AppConfig.load(tmp_path / "settings.json")
    assert config.auto_copy_to_clipboard is True
    assert config.paste_to_active_app is False
    assert config.live_preview is True
    assert config.high_accuracy is False
    assert config.history_limit == 100


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig()
    config.live_preview = False
    config.paste_to_active_app = True
    config.history_limit = 50
    config.save(path)

    loaded = AppConfig.load(path)
    assert loaded.live_preview is False
    assert loaded.paste_to_active_app is True
    assert loaded.history_limit == 50


def test_corrupt_file_falls_back_to_defaults_and_is_kept_aside(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.live_preview is True
    # The next save must not destroy the only copy of the user's word list.
    config.save(path)
    assert (tmp_path / "settings.json.corrupt").read_text() == "{not json"


def test_settings_from_a_newer_version_survive_a_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"high_accuracy": True, "push_to_talk": True}), encoding="utf-8")
    config = AppConfig.load(path)
    config.live_preview = False
    config.save(path)
    saved = json.loads(path.read_text())
    assert saved["push_to_talk"] is True and saved["high_accuracy"] is True
    assert saved["live_preview"] is False


def test_settings_1_0_dropped_are_kept_for_an_older_version(tmp_path):
    # 0.9 and earlier read these; 1.0 always dictates on the bar. Rolling back keeps the choice.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"compact_dictation": False, "auto_start_recording": False}), encoding="utf-8")
    AppConfig.load(path).save(path)
    saved = json.loads(path.read_text())
    assert saved["compact_dictation"] is False and saved["auto_start_recording"] is False


def test_wrong_types_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({
            "live_preview": 1,
            "history_limit": -5,
            "paste_to_active_app": True,
        }),
        encoding="utf-8",
    )
    config = AppConfig.load(path)
    assert config.live_preview is True
    assert config.history_limit == 100
    assert config.paste_to_active_app is True


def test_partial_payload_keeps_other_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"auto_copy_to_clipboard": False}), encoding="utf-8")
    config = AppConfig.load(path)
    assert config.auto_copy_to_clipboard is False
    assert config.live_preview is True


def test_save_is_atomic(tmp_path):
    path = tmp_path / "settings.json"
    AppConfig().save(path)
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()


def test_microphone_choice_persists(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig(prefer_builtin_mic=False, input_device="AirPods")
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.input_device == "AirPods"
    assert loaded.prefer_builtin_mic is False


def test_the_bar_position_persists_and_nonsense_puts_it_back_in_its_default_place(tmp_path):
    path = tmp_path / "settings.json"
    assert AppConfig.load(path).bar_position is None
    AppConfig(bar_position=[0.25, 1.0]).save(path)
    assert AppConfig.load(path).bar_position == [0.25, 1.0]
    for stored in ([0.5], [0.5, 1.5], [0.5, -0.1], [True, 0.5], ["0.5", 0.5], [0.5, float("nan")], {"x": 0.5}, 0.5):
        path.write_text(json.dumps({"bar_position": stored}), encoding="utf-8")
        assert AppConfig.load(path).bar_position is None, stored
    path.write_text(json.dumps({"bar_position": [0, 1]}), encoding="utf-8")
    assert AppConfig.load(path).bar_position == [0.0, 1.0]


def test_invalid_microphone_choice_falls_back_to_automatic(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"input_device": 42}))
    assert AppConfig.load(path).input_device is None


def test_keep_microphone_ready_accepts_zero_and_rejects_nonsense(tmp_path):
    path = tmp_path / "settings.json"
    assert AppConfig.load(path).keep_mic_ready_seconds == 0
    for stored, expected in ((120, 120), (0, 0), (-5, 0), (86400, 0), (True, 0), ("30", 0)):
        path.write_text(json.dumps({"keep_mic_ready_seconds": stored}), encoding="utf-8")
        assert AppConfig.load(path).keep_mic_ready_seconds == expected
    config = AppConfig(keep_mic_ready_seconds=300)
    config.save(path)
    assert AppConfig.load(path).keep_mic_ready_seconds == 300
