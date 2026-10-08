"""How a Parakeet transcript is written down where the model writes it differently from a person: fillers and clock times."""

from __future__ import annotations

import re

# "um" and "uh" as Parakeet writes them; never "uh-huh", "uh oh", or "uh huh",
# and never an all-capitals "UM", which is a name. Parakeet v2 is
# English-only, so these are not words of another language here.
_FILLER = r"(?:[Uu]m+|[Uu]h+(?!\s+(?:[Oo]h|[Hh]uh)\b)|[Uu]hm|[Ee]rm)(?![\w-])"
# One or several in a row: "um, uh" is as common as "um".
_FILLERS = rf"{_FILLER}(?:,?\s+{_FILLER})*"
# Set off by commas inside a sentence. In an aside the commas go with it, "I
# was, um, thinking, and then" reads "I was thinking, and then"; between items
# of a list or two numbers one stays, "eggs, um, milk, and bread" reads "eggs,
# milk, and bread". A list's tail is short items to the end of the sentence.
# The words either side are read whole, so "$5, um, $6" counts as two numbers.
_BETWEEN_COMMAS = re.compile(rf"(?<!\S)(\S*),\s+{_FILLERS},\s+(?=(\S*)([^.?!]*))")
# An item is one or two words, neither of them the connector: read as a word
# of an item too, "and" would give a run of them more readings at each one.
_WORD = r"(?!(?:and|or)\b)\w+"
_ITEM = rf"{_WORD}(?:\s{_WORD})?"
# The connector is possessive: read as an item's first word too, "and" would
# give each item two readings, and a long non-list sentence exponential work.
_LIST_TAIL = re.compile(rf"{_ITEM}(?:,\s*(?:(?:and|or)\s+)?+{_ITEM}|\s(?:and|or)\s{_ITEM})+\s*")
# A comma after an opening word or greeting belongs to it: "Okay, um, let's go"
# reads "Okay, let's go", "Hey John, uh, quick question" keeps its comma too.
_INTRODUCTORY = re.compile(r"(?:^|[.?!]\s+)(?:yeah|yes|no|okay|ok|well|right|sure|so|sorry|actually|anyway|oh|"
                           r"thanks|(?:hi|hey|hello|dear)(?:\s+\w+)?)$", re.IGNORECASE)
# Ending a clause: the comma before it goes too, "I think, um." reads "I think."
_CLOSING = re.compile(rf",\s*{_FILLERS}(?=\s*(?:[.?!;:…]|$))")
# A filler sentence that ends the text goes with its own punctuation: "That is all. Um." reads "That is all."
_TRAILING = re.compile(rf"(?<=[.?!…])(?:\s+{_FILLER}(?:\.\.\.|[,.?!;:…])?)+\s*$")
# Opening a sentence: its capital goes to the next word, "Um, so" reads "So",
# unless that word has capitals of its own ("iPhone"). A quotation opens one
# too: 'He said, "Um, I see."' reads 'He said, "I see."'
_OPENING = re.compile(rf"((?:^|[.?!]\s+)[\"“‘(]?|,\s+[\"“‘])(?:{_FILLER}(?:[,.?!;:…]|\.\.\.)?\s+)+(\w+)")
_ANYWHERE = re.compile(rf"\s*(?<![\w-]){_FILLER}[,]?")
_ALONE = re.compile(rf"^(?:{_FILLER}(?:\.\.\.|[,.?!;:…])?\s*)+$")


def _numeral(word: str) -> bool:
    return any(character.isdigit() for character in word)


def _between_commas(match: re.Match) -> str:
    before, after, rest = match.group(1), match.group(2), match.group(3)
    opening = _INTRODUCTORY.search(match.string[:match.start(1) + len(before)]) is not None
    separates = (_numeral(before) and _numeral(after)) or _LIST_TAIL.fullmatch(after + rest) is not None
    return f"{before}, " if opening or separates else f"{before} "


def _capitalised(word: str) -> str:
    return word if any(letter.isupper() for letter in word) else word[0].upper() + word[1:]


# Clock times written with a dot or no separator, as Parakeet writes them
# ("8.45 p.m.", "619 p.m."), where a person writes "8:45 p.m." The time of day
# that follows decides: a dot alone also writes prices and versions.
_DAY_PART = r"\s*(?:[ap]\.m\.|[AP]\.M\.|[ap]m\b|[AP]M\b|o['’]clock\b)"
_HOUR = r"(1[0-2]|0?[1-9])"
_MINUTES = r"([0-5]\d)"
# A time listed before one that has its time of day ("8.30, 8.45 p.m.", "9.30 or 9.45 p.m.") is one too.
_LISTED = r"(?:,\s*|\s+(?:or|to|and|until|till)\s+|\s*[-–]\s*)(?:1[0-2]|0?[1-9])[.:][0-5]\d"
_DOTTED_TIME = re.compile(rf"(?<![\w.$£€]){_HOUR}\.{_MINUTES}(?=(?:{_LISTED})*{_DAY_PART})")
# Without a dot only before "a.m."/"p.m." as Parakeet writes them: "100 PM" may be product managers.
_RUN_TOGETHER_TIME = re.compile(rf"(?<![\w.$£€]){_HOUR}{_MINUTES}(?=\s*[ap]\.m\.)")


def without_fillers(text: str) -> str:
    """`text` without "um" and "uh". A filler that opened a sentence hands
    its capital to the next word; one set off by commas takes them with it."""
    if _ALONE.match(text):
        return ""
    text = _TRAILING.sub("", text)
    text = _CLOSING.sub("", text)  # Before the commas, so a closing filler does not read as a list item.
    # Before the commas: "Um, okay, um, let's go" then opens with "Okay", whose comma stays.
    text = _OPENING.sub(lambda match: match.group(1) + _capitalised(match.group(2)), text)
    text = _BETWEEN_COMMAS.sub(_between_commas, text)
    text = _ANYWHERE.sub("", text)
    return text.strip()


def with_clock_times(text: str) -> str:
    """`text` with clock times written with a colon: "8.45 p.m." and "619 p.m."
    become "8:45 p.m." and "6:19 p.m."; a number not followed by a time of day
    is left alone."""
    text = _DOTTED_TIME.sub(r"\1:\2", text)
    return _RUN_TOGETHER_TIME.sub(r"\1:\2", text)


def written(text: str) -> str:
    """A transcript as a person would write it down."""
    return with_clock_times(without_fillers(text))
