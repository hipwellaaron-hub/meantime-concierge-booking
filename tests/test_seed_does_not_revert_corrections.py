"""app.seed runs on EVERY deploy (preDeploy), so what it writes is what
survives a deploy -- and what it overwrites is lost without a trace.

The client-facing renders now read the venue COLUMNS, not the policy.py
constants, so a bank account or an ABN corrected in the database is a live
answer sitting on every invoice. A plain assignment in the seed would revert
it on the next deploy, and the only thing in the log would be "Seeded
Hamilton venue and spaces" (review, 2026-09-12).
"""
from app.seed import seed


def test_a_corrected_bank_account_survives_the_next_deploy(db, hamilton):
    """The one that costs money. Somebody fixes the BSB in the database
    because a client paid into the wrong account; the next deploy must not
    put the wrong one back."""
    hamilton.bank_bsb = "123456"
    hamilton.bank_account_number = "99887766"
    hamilton.abn = "11 111 111 111"
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.bank_bsb == "123456", "the deploy reverted a corrected BSB"
    assert hamilton.bank_account_number == "99887766"
    assert hamilton.abn == "11 111 111 111"


def test_a_corrected_contact_survives_the_next_deploy(db, hamilton):
    """Same rule for everything a client reads, not just the money fields."""
    hamilton.contact_email = "someone.else@meantime.com.au"
    hamilton.contact_name = "Someone Else"
    hamilton.phone = "0400 000 000"
    hamilton.trading_name = "Meantime Hamilton Bar"
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.contact_email == "someone.else@meantime.com.au"
    assert hamilton.contact_name == "Someone Else"
    assert hamilton.phone == "0400 000 000"
    assert hamilton.trading_name == "Meantime Hamilton Bar"


def test_an_empty_field_is_still_filled(db, hamilton):
    """The other half. Leaving corrections alone must not mean a venue that
    predates the identity columns stays blank -- a blank ABN prints nothing
    at all on an invoice."""
    hamilton.abn = None
    hamilton.bank_bsb = None
    hamilton.contact_email = None
    hamilton.reference_prefix = None
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.abn, "a blank ABN was left blank"
    assert hamilton.bank_bsb
    assert hamilton.contact_email
    assert hamilton.reference_prefix == "HAM"


def test_an_empty_string_counts_as_unfilled(db, hamilton):
    """"" is not somebody's answer -- it is a field nobody finished. It gets
    the default, the same as NULL."""
    hamilton.phone = ""
    db.flush()

    seed(db)

    db.refresh(hamilton)
    assert hamilton.phone, "an empty string was treated as a deliberate answer"
