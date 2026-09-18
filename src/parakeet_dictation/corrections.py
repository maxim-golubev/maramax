"""Explicit, local word replacements; no model, inference, or chained rewrites."""

from __future__ import annotations

import re

MAX_RULES = 100


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
        heard, replacement = heard.strip(), replacement.strip()
        if not heard or not replacement or len(heard) > 200 or len(replacement) > 2000:
            continue
        if heard.casefold() in seen:
            continue
        seen.add(heard.casefold())
        rules.append({"heard": heard, "replacement": replacement})
        if len(rules) == MAX_RULES:
            break
    return rules


def apply_replacements(text: str, rules: list[dict[str, str]]) -> str:
    rules = normalize_rules(rules)
    if not rules:
        return text
    # Prefer longer phrases, respect word boundaries, and use one pass so an
    # inserted replacement can never trigger a second rule.
    ordered = sorted(rules, key=lambda rule: len(rule["heard"]), reverse=True)
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(f"(?P<r{i}>{re.escape(rule['heard'])})" for i, rule in enumerate(ordered))
        + r")(?!\w)", re.IGNORECASE,
    )
    def replace(match: re.Match[str]) -> str:
        assert match.lastgroup is not None
        return ordered[int(match.lastgroup[1:])]["replacement"]

    return pattern.sub(replace, text)
