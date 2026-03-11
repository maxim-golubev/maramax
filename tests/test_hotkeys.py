import pytest

from parakeet_dictation.hotkeys import _four_char_code


def test_four_char_code_requires_exact_length():
    with pytest.raises(ValueError, match="exactly 4 characters"):
        _four_char_code("MM")


def test_four_char_code_encodes_ascii_signature():
    assert _four_char_code("MRMX") == int.from_bytes(b"MRMX", "big")
