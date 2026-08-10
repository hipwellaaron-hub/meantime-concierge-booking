from app.services.staff_auth import (
    authenticate,
    create_or_update_staff_user,
    deactivate_staff_user,
    get_by_email,
    hash_password,
    verify_password,
)


def test_hash_and_verify_round_trip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_verify_password_rejects_malformed_hash():
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_create_or_update_staff_user_is_idempotent(db):
    first = create_or_update_staff_user(db, email="Staff@Example.com", name="First Name", password="password123")
    second = create_or_update_staff_user(db, email="staff@example.com", name="Second Name", password="newpassword123")

    assert first.id == second.id
    assert second.name == "Second Name"
    assert verify_password("newpassword123", second.password_hash) is True
    assert verify_password("password123", second.password_hash) is False


def test_get_by_email_is_case_insensitive(db):
    created = create_or_update_staff_user(db, email="Staff@Example.com", name="Staff", password="password123")
    assert get_by_email(db, "staff@example.com").id == created.id
    assert get_by_email(db, "STAFF@EXAMPLE.COM").id == created.id


def test_authenticate_success(db):
    create_or_update_staff_user(db, email="staff@example.com", name="Staff", password="password123")
    staff = authenticate(db, "staff@example.com", "password123")
    assert staff is not None
    assert staff.email == "staff@example.com"


def test_authenticate_rejects_wrong_password(db):
    create_or_update_staff_user(db, email="staff@example.com", name="Staff", password="password123")
    assert authenticate(db, "staff@example.com", "wrong-password") is None


def test_authenticate_rejects_unknown_email(db):
    assert authenticate(db, "nobody@example.com", "password123") is None


def test_authenticate_rejects_inactive_account(db):
    create_or_update_staff_user(db, email="staff@example.com", name="Staff", password="password123")
    deactivate_staff_user(db, email="staff@example.com")
    assert authenticate(db, "staff@example.com", "password123") is None
