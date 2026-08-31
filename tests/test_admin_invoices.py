import datetime as dt
from decimal import Decimal

from app.models.invoice import InvoiceStatus, InvoiceType
from app.services import invoicing
from app.services.booking import create_booking


def _booking(db, space, name="Invoice List Test"):
    # A real contact with a valid email: invoicing.mark_sent refuses
    # otherwise, since "sent" would be claiming a client has a link.
    from app.models import Contact

    contact = Contact(name="Invoice Test Contact", email=f"invoice.{name.replace(' ', '.').lower()}@example.com")
    db.add(contact)
    db.flush()
    return create_booking(
        db, space_id=space.id, contact_id=contact.id, event_date=dt.date(2027, 3, 6),
        start_time=dt.time(18, 0), end_time=dt.time(23, 0), event_name=name,
        event_type="birthday", adult_count=50, child_count=0, notes=None, actor="test",
    )


def _invoice(db, booking, *, sent=False, amount="500.00"):
    invoice = invoicing.create_invoice(
        db, booking, InvoiceType.deposit,
        [{"description": "Deposit", "quantity": 1, "unit_price": amount}],
        dt.date.today(), actor="test",
    )
    if sent:
        invoicing.mark_sent(db, invoice, actor="test")
    return invoice


def test_invoices_list_shows_invoices(admin_client, db, loft):
    booking = _booking(db, loft, name="Listed Event")
    invoice = _invoice(db, booking, sent=True)

    page = admin_client.get("/admin/invoices")
    assert page.status_code == 200
    assert f"#{invoice.invoice_number}" in page.text
    assert "Listed Event" in page.text


def test_invoices_list_filters_by_status(admin_client, db, loft):
    booking = _booking(db, loft)
    draft = _invoice(db, booking, amount="100.00")
    sent = _invoice(db, _booking(db, loft, name="Second"), sent=True, amount="200.00")

    page = admin_client.get("/admin/invoices?status=sent")
    assert f"#{sent.invoice_number}" in page.text
    assert f"#{draft.invoice_number}" not in page.text


def test_invoices_list_rejects_an_unknown_status(admin_client):
    assert admin_client.get("/admin/invoices?status=nonsense").status_code == 422


def test_empty_status_means_no_filter(admin_client, db, loft):
    draft = _invoice(db, _booking(db, loft), amount="100.00")
    page = admin_client.get("/admin/invoices?status=")
    assert page.status_code == 200
    assert f"#{draft.invoice_number}" in page.text


def test_unpaid_tile_count_matches_the_list_it_links_to(admin_client, db, loft):
    """The dashboard tile links to /admin/invoices?status=sent, so the
    count on the tile and the rows on that page must be the same set --
    otherwise clicking a '3' that shows 5 rows quietly erodes trust in
    every other number on the dashboard."""
    for i in range(3):
        _invoice(db, _booking(db, loft, name=f"Unpaid {i}"), sent=True)
    _invoice(db, _booking(db, loft, name="Still draft"))  # must not be counted

    dashboard = admin_client.get("/admin/")
    assert 'href="/admin/invoices?status=sent"' in dashboard.text

    from app.models import Venue

    venue = db.query(Venue).filter_by(slug="hamilton").one()
    listed = invoicing.search_invoices(db, venue.id, status=InvoiceStatus.sent)
    assert len(listed) == 3


def test_open_enquiries_tile_links_to_the_filtered_bookings_list(admin_client):
    dashboard = admin_client.get("/admin/")
    assert 'href="/admin/bookings?status=enquiry"' in dashboard.text
    assert admin_client.get("/admin/bookings?status=enquiry").status_code == 200


def test_search_invoices_is_scoped_to_the_venue(db, loft):
    from app.models import Space, Venue

    other_venue = Venue(name="Other", slug="other-venue")
    db.add(other_venue)
    db.flush()
    other_space = Space(
        venue_id=other_venue.id, name="Other Room", capacity=50,
        min_food_spend=Decimal("100.00"), standard_min_adults=0,
    )
    db.add(other_space)
    db.flush()

    _invoice(db, _booking(db, loft, name="Ours"), sent=True)
    _invoice(db, _booking(db, other_space, name="Theirs"), sent=True)

    hamilton = db.query(Venue).filter_by(slug="hamilton").one()
    ours = invoicing.search_invoices(db, hamilton.id)
    assert all(i.booking.space.venue_id == hamilton.id for i in ours)
    assert "Theirs" not in {i.booking.event_name for i in ours}


def test_invoices_list_shows_client_name_and_event_date(admin_client, db, loft):
    booking = _booking(db, loft, name="Dated Event")
    _invoice(db, booking, sent=True)

    page = admin_client.get("/admin/invoices")
    assert page.status_code == 200
    assert "Invoice Test Contact" in page.text  # client name column
    assert "06-03-2027" in page.text            # event date, day-first
