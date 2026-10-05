"""How a transcript is written down: fillers and clock times, as pure text rules."""

import pytest

from parakeet_dictation.written_form import with_clock_times, without_fillers, written


@pytest.mark.parametrize("heard, wanted", [
    ("Um, I remember that.", "I remember that."),            # The capital goes to the next word.
    ("Uh, constitutions is number one.", "Constitutions is number one."),
    ("To see him. Um, I remember.", "To see him. I remember."),
    ("Room C. Um collaboration room.", "Room C. Collaboration room."),
    ("I was, um, thinking about it.", "I was thinking about it."),
    ("Things like that um and then.", "Things like that and then."),
    ("Kind of uh pipeline.", "Kind of pipeline."),
    ("He said um.", "He said."),
    ("Um.", ""),                                               # Nothing but a filler is nothing said.
    ("Um, uh.", ""),
    ("We need eggs, um, milk, and bread.", "We need eggs, milk, and bread."),   # A list keeps its comma.
    ("Invite Anna, uh, Ben and Carla.", "Invite Anna, Ben and Carla."),
    ("It was 5, um, 6 people.", "It was 5, 6 people."),                         # So do two numbers.
    ("I think, um.", "I think."),                                               # No comma left dangling.
    ("Okay, um. So we start.", "Okay. So we start."),
    ("I want, um... no.", "I want... no."),
    ("Um? What?", "What?"),                                                     # Nor punctuation opening a sentence.
    ("Um... so I think.", "So I think."),
    ("Um, iPhone sales are up.", "iPhone sales are up."),                       # A word with capitals keeps them.
    ("I was, um, thinking, and then I left.", "I was thinking, and then I left."),  # An aside, not a list.
    ("We, uh, went to the store and bought milk.", "We went to the store and bought milk."),
    ("Let me think, um, about it, uh.", "Let me think about it."),
    ("Pick red, um, blue, or green.", "Pick red, blue, or green."),
    ("Um! Wow.", "Wow."),
    ("Um: so.", "So."),
    ("Um. Um. Okay.", "Okay."),
    ("Um... uh... so.", "So."),
    ("Okay. Um! Wow.", "Okay. Wow."),
    ("That is all. Um.", "That is all."),                                      # Trailing off at the end.
    ("Is that right? Um.", "Is that right?"),
    ("Bye. Uh...", "Bye."),
    ("Okay, um, let us start.", "Okay, let us start."),                        # An opening word keeps its comma.
    ("I think, um, uh.", "I think."),                                          # Several in a row, as one.
    ("We, um, uh, went home.", "We went home."),
    ("Is it, uh, um? Yes.", "Is it? Yes."),
    ("Pick A, um, B, or C.", "Pick A, B, or C."),                              # A one-letter item is an item.
    ("It costs $5, um, $6.", "It costs $5, $6."),                              # Numbers with signs are numbers.
    ("Between 5%, um, 10%.", "Between 5%, 10%."),
    ("Hey John, uh, quick question.", "Hey John, quick question."),
])
def test_fillers_are_removed_and_the_sentence_still_reads(heard, wanted):
    assert without_fillers(heard) == wanted


@pytest.mark.parametrize("kept", [
    "It was uh-huh, fine.",           # An answer, not a hesitation.
    "Uh oh, that is bad.",
    "Uh huh, yes.",
    "The UM campus.",                 # A name.
    "Umbrella and uhlan.",            # Words that begin like one.
    "Hmm, that's odd.",               # Meant.
    "That that is the point.",        # Repeated words are often meant: they are left to the speaker.
])
def test_words_that_only_look_like_fillers_are_kept(kept):
    assert without_fillers(kept) == kept


@pytest.mark.parametrize("heard, wanted", [
    ("Meet at 8.45 p.m. today.", "Meet at 8:45 p.m. today."),
    ("You'll see at 619 p.m. today.", "You'll see at 6:19 p.m. today."),
    ("Up until 1.10 p.m. But", "Up until 1:10 p.m. But"),
    ("From 12.30 pm to 1.10 PM.", "From 12:30 pm to 1:10 PM."),
    ("Dinner like 8.30, 8.45 p.m. or", "Dinner like 8:30, 8:45 p.m. or"),   # Listed before a time of day.
    ("From 9.30 to 10.15 a.m.", "From 9:30 to 10:15 a.m."),
    ("About 7.15 o'clock.", "About 7:15 o'clock."),
    ("1230 a.m.", "12:30 a.m."),
])
def test_clock_times_are_written_with_a_colon(heard, wanted):
    assert with_clock_times(heard) == wanted


@pytest.mark.parametrize("kept", [
    "Version 1.10 is out.",           # No time of day: a version, a price, a decimal.
    "It costs $8.45 p.m.?",
    "3.5 p.m.",                       # Not minutes.
    "13.30 p.m.",                     # Not an hour on a 12-hour clock.
    "2023 p.m.",
    "v1.10.20 p.m.",
    "We hired 100 PM candidates.",    # Without a dot, only Parakeet's own "p.m." marks a time.
    "Between 1.5 and 2.30 pm",        # Only the time changes: "2:30 pm".
])
def test_numbers_that_are_not_clock_times_are_left_alone(kept):
    assert with_clock_times(kept).replace("2:30 pm", "2.30 pm") == kept


def test_a_long_sentence_that_is_not_a_list_is_tidied_at_once():
    import time
    sentence = "We need, um, " + ", and milk" * 60 + ", so let's go."
    started = time.perf_counter()
    without_fillers(sentence)
    assert time.perf_counter() - started < 0.1      # It once took exponential time.


def test_every_time_in_a_list_gets_its_colon():
    assert with_clock_times("Slots at 2.30, 3.30, 4.30 pm.") == "Slots at 2:30, 3:30, 4:30 pm."


def test_a_listed_time_after_a_removed_filler_is_still_a_time():
    assert written("Meet at 8.30, um, 8.45 p.m.") == "Meet at 8:30, 8:45 p.m."


def test_written_applies_both_and_changes_nothing_else():
    assert written("Um, meet at 8.45 p.m., okay?") == "Meet at 8:45 p.m., okay?"
    for plain in ("Parakeet already writes $10, 100% and 0.4% as digits.",
                  "Open the .env file in .NET, then type :wq."):
        assert written(plain) == plain


def test_a_long_run_without_spaces_takes_no_time():
    import time
    started = time.monotonic()
    written("a," * 25000 + " um, done.")
    assert time.monotonic() - started < 0.5
