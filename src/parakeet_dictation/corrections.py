"""Explicit, local word replacements; no model, inference, or chained rewrites."""

from __future__ import annotations

import re
import unicodedata

MAX_RULES = 100
MAX_HEARD_CHARS = 200
MAX_REPLACEMENT_CHARS = 2000
# A replacement longer than this is a snippet to insert, not a word to listen for.
MAX_VOCABULARY_TERM_CHARS = 40


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
        # One space between words, so "open ai" and "open  ai" are one rule.
        heard = " ".join(unicodedata.normalize("NFC", heard).split())
        replacement = replacement.strip()
        if (not heard or not replacement or len(heard) > MAX_HEARD_CHARS
                or len(replacement) > MAX_REPLACEMENT_CHARS):
            continue
        if heard.casefold() in seen:
            continue
        seen.add(heard.casefold())
        rules.append({"heard": heard, "replacement": replacement})
        if len(rules) == MAX_RULES:
            break
    return rules


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
