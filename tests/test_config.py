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
