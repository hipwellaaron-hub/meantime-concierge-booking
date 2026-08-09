from app.utils import looks_like_a_token, truncate


def test_truncate_leaves_short_strings_alone():
    assert truncate("hello", 10) == "hello"


def test_truncate_cuts_long_strings():
    assert truncate("hello world", 5) == "hello"


def test_looks_like_a_token_accepts_real_token_shape():
    assert looks_like_a_token("AbCdEf1234567890_-abcdefghijklmnopqrstuvwxyz") is True


def test_looks_like_a_token_rejects_null_byte():
    assert looks_like_a_token("token\x00.txt") is False


def test_looks_like_a_token_rejects_oversized_input():
    assert looks_like_a_token("a" * 5000) is False


def test_looks_like_a_token_rejects_empty_string():
    assert looks_like_a_token("") is False


def test_looks_like_a_token_rejects_path_traversal_shape():
    assert looks_like_a_token("../../etc/passwd") is False
