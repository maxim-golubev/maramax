import json
import threading

from parakeet_dictation import app as module
from parakeet_dictation.config import AppConfig
from parakeet_dictation.corrections import apply_replacements, normalize_rules
from parakeet_dictation.history import HistoryStore


def test_replacements_respect_boundaries_and_preserve_literal_output():
    rules = [{"heard": "max", "replacement": "Maxim"},
             {"heard": "mara max", "replacement": r"Maramax\1"}]
    assert apply_replacements("MARA MAX, max; maximum.", rules) == r"Maramax\1, Maxim; maximum."


def test_rules_do_not_cascade_or_treat_phrases_as_regex():
    rules = [{"heard": "a.b", "replacement": "next"}, {"heard": "next", "replacement": "last"}]
    assert apply_replacements("a.b and next and axb", rules) == "next and last and axb"


def test_unicode_and_empty_rules():
    assert apply_replacements("CAFÉ, hello", [{"heard": "café", "replacement": "Café Luna"}]) == "Café Luna, hello"
    assert apply_replacements("Leave this alone.", []) == "Leave this alone."


def test_config_validates_and_persists_rules(tmp_path):
    rules = normalize_rules([{}, None, {"heard": "", "replacement": "yes"},
                             {"heard": " max ", "replacement": "Maxim"},
                             {"heard": "MAX", "replacement": "duplicate"}])
    assert rules == [{"heard": "max", "replacement": "Maxim"}]
    path = tmp_path / "settings.json"
    AppConfig(replacements=rules, use_corrections=False).save(path)
    loaded = AppConfig.load(path)
    assert loaded.replacements == rules
    assert not loaded.use_corrections
    path.write_text(json.dumps({"replacements": "malformed"}))
    assert AppConfig.load(path).replacements == []


def test_publication_keeps_raw_text_and_pastes_only_new_dictation(tmp_path, monkeypatch):
    app = object.__new__(module.DictationApp)
    app.config = AppConfig(paste_to_active_app=True, replacements=[{"heard": "mara max", "replacement": "Maramax"}])
    app.history_store = HistoryStore(base_dir=tmp_path)
    app._state_lock = threading.Lock()
    app._force_copy_after_transcription = False
    app._set_current_text_on_main = lambda *_args: None
    app._refresh_history_on_main = lambda: None
    copied, pending = [], []
    app._copy_text_with_feedback = lambda text, **kwargs: copied.append(text) or True
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *args: pending.append(args))
    assert app._publish_transcript("mara max", "microphone", "Dictation", True, 1) == "Maramax"
    assert copied == ["Maramax"]
    assert len(pending) == 1
    assert pending[0][1:] == (1, "Maramax")
    entry = app.history_store.list_entries()[0]
    assert entry.text == "Maramax" and entry.raw_text == "mara max"
    assert "Before word replacements" in HistoryStore(base_dir=tmp_path).render()
    pending.clear()
    app._publish_transcript("mara max", "recovery", "Retry", True, 2)
    assert not pending  # Retry must not paste into a stale destination.
    assert app._publish_transcript("mara max", "file", "File", True, 3) == "mara max"
    app.config.use_corrections = False
    assert app._publish_transcript("mara max", "microphone", "Raw", True, 4) == "mara max"
