"""Explicit, local word replacements; no model, inference, or chained rewrites."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_RULES = 100
MAX_HEARD_CHARS = 200
MAX_REPLACEMENT_CHARS = 2000
# A replacement longer than this is a snippet to insert, not a word to listen for.
MAX_VOCABULARY_TERM_CHARS = 40
# How much of an existing replacement a refusal quotes.
_QUOTED_CHARS = 40


@dataclass(frozen=True)
class RuleRefused:
    """Why a rule cannot be saved, in words for the person who typed it."""
    reason: str


def _cleaned(heard: str, replacement: str) -> tuple[str, str]:
    # One space between words, so "open ai" and "open  ai" are one rule.
    return " ".join(unicodedata.normalize("NFC", heard).split()), replacement.strip()


def _malformed(heard: str, replacement: str) -> str | None:
    """What is wrong with a cleaned rule on its own, or None."""
    if not heard:
        return "Enter the words the transcript says."
    if not replacement:
        return "Enter what to write instead."
    if len(heard) > MAX_HEARD_CHARS:
        return f"What the transcript says can be up to {MAX_HEARD_CHARS} characters."
    if len(replacement) > MAX_REPLACEMENT_CHARS:
        return f"A replacement can be up to {MAX_REPLACEMENT_CHARS:,} characters."
    return None


def rule_key(heard: str) -> str:
    """What makes two rules one rule: the same words heard, however spaced or
    capitalized. Lower case, as re.IGNORECASE compares letters: casefold()
    would also take "STRASSE" for "Straße", which matching never does."""
    return _cleaned(heard, "")[0].lower()


def normalize_rules(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rules = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        heard, replacement = item.get("heard"), item.get("replacement")
        if not isinstance(heard, str) or not isinstance(replacement, str):
            continue
        heard, replacement = _cleaned(heard, replacement)
        if _malformed(heard, replacement) is not None or rule_key(heard) in seen:
            continue
        seen.add(rule_key(heard))
        rules.append({"heard": heard, "replacement": replacement})
        if len(rules) == MAX_RULES:
            break
    return rules


def find_rule(rules: list[dict[str, str]], heard: str) -> int | None:
    """The index of the rule for these heard words, or None."""
    key = rule_key(heard)
    return next((index for index, rule in enumerate(rules) if rule_key(rule["heard"]) == key), None)


def edited_rules(rules: list[dict[str, str]], heard: str, replacement: str,
                 at: int | None) -> list[dict[str, str]] | RuleRefused:
    """`rules` with this rule added (`at` None) or put in place of rules[at];
    or why it cannot be. A rule for words another rule already covers is
    refused, never merged: saving one rule must not change another."""
    heard, replacement = _cleaned(heard, replacement)
    problem = _malformed(heard, replacement)
    if problem is not None:
        return RuleRefused(problem)
    existing = find_rule(rules, heard)
    if existing is not None and existing != at:
        other = rules[existing]
        shown = " ".join(other["replacement"].split())
        if len(shown) > _QUOTED_CHARS:
            shown = shown[: _QUOTED_CHARS - 1] + "…"
        return RuleRefused(f"“{other['heard']}” is already in the list, replaced with “{shown}”.")
    rule = {"heard": heard, "replacement": replacement}
    if at is not None:
        return [rule if index == at else other for index, other in enumerate(rules)]
    if len(rules) >= MAX_RULES:
        return RuleRefused(f"Up to {MAX_RULES} replacements are supported. Remove one to add another.")
    return [*rules, rule]


def _matcher(heard: str) -> str:
    """A regular expression for one heard phrase: literal text, any run of
    whitespace between its words, and a word boundary only on an edge that
    is itself a word character (a rule such as "..." has no such edge)."""
    body = r"\s+".join(re.escape(part) for part in heard.split())
    before = r"(?<!\w)" if re.match(r"\w", heard[0]) else ""
    after = r"(?!\w)" if re.match(r"\w", heard[-1]) else ""
    return before + body + after


def apply_replacements(text: str, rules: list[dict[str, str]]) -> str:
    rules = normalize_rules(rules)
    if not rules:
        return text
    # Prefer longer phrases and use one pass, so an inserted replacement can
    # never trigger a second rule. Composed and decomposed accents compare
    # equal once both sides are in the same normal form.
    ordered = sorted(rules, key=lambda rule: len(rule["heard"]), reverse=True)
    pattern = re.compile(
        "|".join(f"(?P<r{i}>{_matcher(rule['heard'])})" for i, rule in enumerate(ordered)),
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        assert match.lastgroup is not None
        return ordered[int(match.lastgroup[1:])]["replacement"]

    return pattern.sub(replace, unicodedata.normalize("NFC", text))


def vocabulary_hint(rules: list[dict[str, str]], limit: int = 50) -> str | None:
    """The spellings the user asked for, phrased for a recognizer that accepts
    context. Only plain names and terms qualify: the recognizer may repeat
    its context, so snippets, addresses, and lists must never be in it."""
    terms: list[str] = []
    for rule in normalize_rules(rules):
        term = rule["replacement"]
        if (len(term) <= MAX_VOCABULARY_TERM_CHARS
                and re.fullmatch(r"[\w][\w .'’+#&-]*", term)
                and re.search(r"[^\W\d_]", term)  # A name has a letter in it; a phone number does not.
                and term.casefold() not in {t.casefold() for t in terms}):
            terms.append(term)
    return f"Vocabulary: {', '.join(terms[:limit])}." if terms else None
