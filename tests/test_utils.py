from app.utils import is_valid_email, looks_like_a_token, truncate


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


def test_is_valid_email_accepts_real_address():
    assert is_valid_email("aaron@meantime.com.au") is True


def test_is_valid_email_rejects_none():
    assert is_valid_email(None) is False


def test_is_valid_email_rejects_empty_string():
    assert is_valid_email("") is False


def test_is_valid_email_rejects_missing_at_sign():
    assert is_valid_email("not-an-email") is False


def test_is_valid_email_rejects_no_domain():
    assert is_valid_email("someone@") is False
