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


# --- client-facing link tokens -----------------------------------------------


def test_access_tokens_are_short_enough_for_an_email_link():
    from app.utils import generate_access_token

    token = generate_access_token()
    assert len(token) == 22, "a longer token makes client links look like spam"
    assert looks_like_a_token(token)


def test_access_tokens_are_unguessable_and_unique():
    from app.utils import ACCESS_TOKEN_BYTES, generate_access_token

    # 128 bits. Well beyond brute force, and these links can sign a contract.
    assert ACCESS_TOKEN_BYTES * 8 >= 128
    assert len({generate_access_token() for _ in range(1000)}) == 1000


def test_tokens_issued_before_the_length_change_still_validate():
    """Real links already in clients' inboxes are 43 characters. They are
    looked up by exact match, so they must keep resolving -- shortening
    new tokens must never invalidate an agreement someone already holds."""
    assert looks_like_a_token("gHdI-CyR8grz8J9k586BJzTu6kigyoFS1RJDUiBJRv4")
    assert looks_like_a_token("fUxH5m9BxwuQaXnKStwCdxF6RWsLlOOnoXd9gJLG6N8")


def test_every_client_facing_model_uses_the_shared_token_generator():
    """Document, Invoice and WizardSession each had their own copy of the
    generator; a length change to one and not the others would be silent."""
    from app.models.document import generate_access_token as document_token
    from app.models.invoice import generate_access_token as invoice_token
    from app.models.wizard_session import generate_access_token as wizard_token

    assert {len(document_token()), len(invoice_token()), len(wizard_token())} == {22}
