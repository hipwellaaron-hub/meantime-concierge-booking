def test_logout_without_csrf_token_is_rejected(admin_client):
    # No csrf_token field at all fails FastAPI's own required-Form-field
    # validation (422) before require_csrf's own check ever runs -- still
    # a rejection, just via a different layer than a wrong token (403).
    resp = admin_client.post("/admin/logout", data={}, follow_redirects=False)
    assert resp.status_code == 422


def test_logout_with_wrong_csrf_token_is_rejected(admin_client):
    resp = admin_client.post("/admin/logout", data={"csrf_token": "not-the-real-token"}, follow_redirects=False)
    assert resp.status_code == 403

    # Session must still be alive -- a rejected CSRF attempt must not log
    # the real user out.
    still_in = admin_client.get("/admin/", follow_redirects=False)
    assert still_in.status_code == 200


def test_policy_action_without_csrf_token_is_rejected(admin_client, db, booking):
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake", data={"permitted": "true"}, follow_redirects=False
    )
    assert resp.status_code == 422
    db.refresh(booking)
    assert booking.outside_cake_permitted is False


def test_policy_action_with_wrong_csrf_token_is_rejected(admin_client, db, booking):
    resp = admin_client.post(
        f"/admin/bookings/{booking.id}/policy/outside-cake",
        data={"permitted": "true", "csrf_token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db.refresh(booking)
    assert booking.outside_cake_permitted is False
