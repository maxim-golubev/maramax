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
    app._cancel_event = threading.Event()
    app._set_current_text_on_main = lambda *_args: None
    app._refresh_history_on_main = lambda: None
    copied, pending = [], []
    app._copy_text_with_feedback = lambda text, **kwargs: copied.append(text) or True
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *args: pending.append(args))
    assert app._publish_transcript("mara max", module.Source.MICROPHONE, "Dictation", 1) == ("Maramax", True)
    assert copied == ["Maramax"]
    assert len(pending) == 1
    assert pending[0][1:] == (1, "Maramax")
    entry = app.history_store.list_entries()[0]
    assert entry.text == "Maramax" and entry.raw_text == "mara max"
    assert "Before word replacements" in HistoryStore(base_dir=tmp_path).render()
    pending.clear()
    app._publish_transcript("mara max", module.Source.RECOVERY, "Retry", 2)
    assert not pending  # Retry must not paste into a stale destination.
    assert app._publish_transcript("mara max", module.Source.FILE, "File", 3)[0] == "mara max"
    app.config.use_corrections = False
    assert app._publish_transcript("mara max", module.Source.MICROPHONE, "Raw", 4)[0] == "mara max"
    # The copy setting is honoured on every path; nothing forces a copy.
    app.config.paste_to_active_app = app.config.auto_copy_to_clipboard = False
    copied.clear()
    statuses = []
    app._push_status = lambda message, revert_after=0: statuses.append(message)
    app._publish_transcript("words", module.Source.MICROPHONE, "Quiet", 5)
    assert copied == [] and statuses == ["Transcript ready"]


def test_vocabulary_hint_lists_wanted_spellings_once():
    from parakeet_dictation.corrections import vocabulary_hint

    rules = [{"heard": "mara max", "replacement": "Maramax"}, {"heard": "meramax", "replacement": "maramax"},
             {"heard": "sig", "replacement": "Kind regards,\nMaxim"}, {"heard": "kairos", "replacement": "Cairos"}]
    assert vocabulary_hint(rules) == "Vocabulary: Maramax, Cairos."
    assert vocabulary_hint([]) is None


def test_rules_match_however_the_recognizer_spaced_or_composed_the_words():
    def rule(heard, replacement):
        return [{"heard": heard, "replacement": replacement}]

    assert apply_replacements("wait... what", rule("...", "…")) == "wait… what"
    assert apply_replacements("open  ai and open\u00a0ai", rule("open ai", "OpenAI")) == "OpenAI and OpenAI"
    assert apply_replacements("cafe\u0301 cafe", rule("cafe", "Coffee")) == "caf\u00e9 Coffee"


def test_vocabulary_hint_never_carries_snippets_or_addresses():
    from parakeet_dictation.corrections import vocabulary_hint

    rules = [{"heard": "addr", "replacement": "maxim@example.com"}, {"heard": "list", "replacement": "one, two"},
             {"heard": "dots", "replacement": "..."}, {"heard": "see", "replacement": "C++"},
             {"heard": "phone", "replacement": "555 123 4567"}, {"heard": "site", "replacement": "example.com/login"}]
    assert vocabulary_hint(rules) == "Vocabulary: C++."


def test_rules_that_differ_only_in_spacing_are_one_rule():
    rules = normalize_rules([{"heard": "open ai", "replacement": "OpenAI"}, {"heard": "open  ai", "replacement": "other"}])
    assert rules == [{"heard": "open ai", "replacement": "OpenAI"}]


def test_adding_a_rule_never_replaces_another():
    from parakeet_dictation.corrections import RuleRefused, edited_rules

    rules = [{"heard": "Kairos", "replacement": "Cairos"}, {"heard": "Lira", "replacement": "Lyra"}]
    # The rule that was lost in 0.7.0: a new rule typed after another was shown replaced it.
    assert edited_rules(rules, "Meramax", "Maramax", at=None) == [*rules, {"heard": "Meramax", "replacement": "Maramax"}]
    refused = edited_rules(rules, " lira ", "Other", at=None)
    assert refused == RuleRefused("“Lira” is already in the list, replaced with “Lyra”.")
    assert edited_rules(rules, "open  ai", "  OpenAI ", at=None)[-1] == {"heard": "open ai", "replacement": "OpenAI"}


def test_changing_a_rule_changes_only_that_rule():
    from parakeet_dictation.corrections import RuleRefused, edited_rules

    rules = [{"heard": "Kairos", "replacement": "Cairos"}, {"heard": "Lira", "replacement": "Lyra"}]
    assert edited_rules(rules, "LIRA", "Lyra", at=1) == [rules[0], {"heard": "LIRA", "replacement": "Lyra"}]
    assert isinstance(edited_rules(rules, "kairos", "Lyra", at=1), RuleRefused)   # Words the other rule covers.
    long = edited_rules([{"heard": "sig", "replacement": "Kind regards,\nMaxim " + "x" * 80}], "SIG", "y", at=None)
    assert isinstance(long, RuleRefused) and long.reason.endswith("…”.") and "\n" not in long.reason


def test_a_rule_is_refused_with_the_reason():
    from parakeet_dictation.corrections import MAX_HEARD_CHARS, MAX_RULES, RuleRefused, edited_rules

    assert edited_rules([], "  ", "x", at=None) == RuleRefused("Enter the words the transcript says.")
    assert edited_rules([], "x", " \n", at=None) == RuleRefused("Enter what to write instead.")
    assert "up to 200" in edited_rules([], "x" * (MAX_HEARD_CHARS + 1), "y", at=None).reason
    assert "2,000" in edited_rules([], "x", "y" * 2001, at=None).reason
    full = [{"heard": f"w{n}", "replacement": "x"} for n in range(MAX_RULES)]
    assert "Remove one" in edited_rules(full, "new", "x", at=None).reason
    assert edited_rules(full, "new", "x", at=0)[0] == {"heard": "new", "replacement": "x"}   # A change still fits.


def test_whatever_the_editor_saves_survives_loading():
    from parakeet_dictation.corrections import edited_rules, find_rule

    rules = edited_rules([], "café  ok", "Café OK", at=None)
    assert normalize_rules(rules) == rules
    assert find_rule(rules, "CAFÉ OK") == 0 and find_rule(rules, "cafe") is None


def test_while_pasting_every_transcript_is_copied_as_settings_shows(tmp_path, monkeypatch):
    app = object.__new__(module.DictationApp)
    app.config = AppConfig(paste_to_active_app=True, auto_copy_to_clipboard=False)
    app.history_store = HistoryStore(base_dir=tmp_path)
    app._cancel_event = threading.Event()
    app._set_current_text_on_main = lambda *_args: None
    app._refresh_history_on_main = lambda: None
    copied = []
    app._copy_text_with_feedback = lambda text, **kwargs: copied.append(text) or True
    monkeypatch.setattr(module.AppHelper, "callAfter", lambda *args: None)
    app._publish_transcript("from a file", module.Source.FILE, "File", 1)       # Never pasted, still copied.
    app._cancel_event.set()
    app._publish_transcript("cancelled late", module.Source.MICROPHONE, "Dictation", 2)
    assert copied == ["from a file", "cancelled late"]
