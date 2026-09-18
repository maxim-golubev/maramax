import json

from parakeet_dictation.config import AppConfig


def test_defaults_when_file_missing(tmp_path):
    config = AppConfig.load(tmp_path / "settings.json")
    assert config.auto_start_recording is True
    assert config.auto_copy_to_clipboard is True
    assert config.paste_to_active_app is False
    assert config.live_preview is True
    assert config.high_accuracy is False
    assert config.history_limit == 100


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig()
    config.auto_start_recording = False
    config.paste_to_active_app = True
    config.history_limit = 50
    config.save(path)

    loaded = AppConfig.load(path)
    assert loaded.auto_start_recording is False
    assert loaded.paste_to_active_app is True
    assert loaded.live_preview is True
    assert loaded.history_limit == 50


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.auto_start_recording is True


def test_wrong_types_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({
            "auto_start_recording": "yes",
            "live_preview": 1,
            "history_limit": -5,
            "paste_to_active_app": True,
        }),
        encoding="utf-8",
    )
    config = AppConfig.load(path)
    assert config.auto_start_recording is True
    assert config.live_preview is True
    assert config.history_limit == 100
    assert config.paste_to_active_app is True


def test_partial_payload_keeps_other_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"auto_copy_to_clipboard": False}), encoding="utf-8")
    config = AppConfig.load(path)
    assert config.auto_copy_to_clipboard is False
    assert config.auto_start_recording is True


def test_save_is_atomic(tmp_path):
    path = tmp_path / "settings.json"
    AppConfig().save(path)
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()


def test_microphone_choice_and_compact_mode_persist(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig(compact_dictation=False, prefer_builtin_mic=False, input_device="AirPods")
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.input_device == "AirPods"
    assert loaded.compact_dictation is False
    assert loaded.prefer_builtin_mic is False


def test_old_settings_gain_compact_mode_without_changing_paste_preference(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"paste_to_active_app": False}))
    config = AppConfig.load(path)
    assert config.compact_dictation
    assert config.prefer_builtin_mic
    assert config.paste_to_active_app is False


def test_invalid_microphone_choice_falls_back_to_automatic(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"input_device": 42}))
    assert AppConfig.load(path).input_device is None
