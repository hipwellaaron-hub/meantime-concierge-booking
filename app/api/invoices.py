import datetime as dt
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from decimal import Decimal

from app.models.invoice import InvoiceStatus, InvoiceType
from app.services import invoicing, policy, stripe_integration
from app.services.booking import VOIDED_STATUSES
from app.services.pdf import render_html_to_pdf
from app.templating import templates, venue_identity
from app.utils import looks_like_a_token

router = APIRouter(tags=["invoices"])
logger = logging.getLogger(__name__)


def _unavailable_response(request: Request, invoice) -> HTMLResponse:
    # Reached once the event is off (see app.services.booking's
    # VOIDED_STATUSES and change_status, which cancels every live invoice
    # on the way) or an invoice was cancelled directly -- either way,
    # nothing on it is payable any more. A real incident, 2026-09-04:
    # Sophie Mavridis still had a working card link after her offer on the
    # Loft was superseded. 410, not 404: this token is not unknown, it is
    # deliberately no longer live.
    #
    # Deliberately NOT reached for a completed booking. Gating this on the
    # wider TERMINAL_STATUSES made an outstanding balance unpayable the
    # moment staff ticked the event off, and re-issuing did not help
    # because the replacement invoice 410'd too -- so the money could only
    # be chased outside the system, while a client who HAD paid lost
    # access to their own receipt (review finding, 2026-09-04).
    return templates.TemplateResponse(
        request, "link_unavailable.html",
        {
            "booking": invoice.booking,
            # THIS VENUE's address, from the booking's own row. It was a
            # module constant -- Hamilton's -- on a public page shown to a
            # client whose booking may belong to the other company, at the
            # one moment they are already confused about why a link died.
            "contact_email": (invoice.booking.venue.contact_email if invoice.booking.venue else None) or "",
            "message": "This invoice is no longer active. Get in touch and we'll help directly.",
        },
        status_code=410,
    )


def _get_viewable_invoice_or_404(db: Session, token: str):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = invoicing.get_by_token(db, token)
    # Same policy as documents: a draft hasn't been human-approved to show
    # a client yet, so treat its link as not existing rather than leaking it.
    # A legacy invoice is a migrated record, never client-facing -- the real
    # invoice is the iVvy PDF, downloaded by staff from the admin.
    if invoice is None or invoice.status == InvoiceStatus.draft or invoice.is_legacy:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _build_invoice_context(db: Session, invoice, *, include_card_payment: bool) -> dict:
    summary = invoicing.get_payment_summary(db, invoice)

    # WHAT IS PAYABLE NOW, as distinct from what the invoice asked for.
    #
    # summary["balance_due"] is invoice.total minus payments against THIS
    # invoice. A deposit paid AFTER a final invoice went out sits against the
    # deposit invoice, so the balance here never learned of it and the page
    # printed the full food total -- twice -- to a client whose deposit had
    # already landed. Paying what they were shown paid the deposit again.
    #
    # invoice.total is left exactly as it is: it is the record of what was
    # asked for. This is the other figure, derived at render so every
    # invoice that already exists gets it, and computed ONCE here because
    # the web view, the PDF and the staff preview all come through this
    # function and must not disagree.
    uncredited_deposit = invoicing.uncredited_deposit(db, invoice)
    payable_now = max(summary["balance_due"] - uncredited_deposit, Decimal("0.00"))

    card_payment_url = None
    card_payment_amount = None
    if include_card_payment and stripe_integration.is_configured_for(invoice.booking.venue) and not summary["is_fully_paid"]:
        # The amount payable now, with nothing added. The 1.8% card
        # surcharge was removed on 2026-09-11 (see policy.py). And it is
        # payable_now, not balance_due: a Payment Link for the stale full
        # balance would collect the deposit a second time, and the
        # overpayment guard cannot catch that because the amount equals
        # invoice.total exactly.
        card_payment_amount = payable_now
        try:
            card_payment_url, link_id, account = stripe_integration.create_payment_link(
                invoice, card_payment_amount
            )
            invoicing.record_payment_link(db, invoice, link_id, account=account)
        except stripe_integration.StripeNotConfigured:
            card_payment_url = None
        except stripe_integration.StripeVenueMismatch:
            # Never a payment link. A link minted in the wrong company's
            # account is signed by that account, passes verification, and
            # records as a successful payment for the other company.
            logger.exception("Refusing a payment link for invoice %s: venue/account mismatch", invoice.id)
            card_payment_url = None
        except stripe.StripeError:
            # A live API problem (network, auth, rate limit) must not take
            # the whole invoice page down -- fall back to "on request"
            # same as if Stripe weren't configured at all.
            logger.exception("Stripe payment link creation failed for invoice %s", invoice.id)
            card_payment_url = None

    # Other invoices for the same booking -- a deposit invoice references
    # its final invoice and vice versa, matching what a client would see
    # in a real accounting system. Drafts are excluded: same "not human-
    # approved to show a client yet" rule as _get_viewable_invoice_or_404
    # applies to this invoice itself.
    other_invoices = [
        inv for inv in invoice.booking.invoices
        if inv.id != invoice.id and inv.status != InvoiceStatus.draft
    ]

    # Which invoice the uncredited deposit sits on, so the page can name it
    # rather than print a subtraction with no explanation. Deposit invoices
    # only, and only ones with money against them -- a cancelled deposit
    # invoice holding a part payment still counts, for the reason
    # get_deposit_paid gives.
    deposit_references = [
        inv.invoice_reference
        for inv in invoice.booking.invoices
        if inv.type == InvoiceType.deposit
        and inv.id != invoice.id
        and invoicing.get_total_paid(db, inv.id) > 0
    ] if uncredited_deposit > 0 else []

    return {
        "invoice": invoice,
        "booking": invoice.booking,
        "summary": summary,
        "uncredited_deposit": uncredited_deposit,
        "payable_now": payable_now,
        "deposit_references": deposit_references,
        "gst_component": invoicing.gst_component(invoice.total),
        "line_items": invoicing.line_item_breakdown(invoice.line_items),
        "other_invoices": other_invoices,
        "stripe_configured": card_payment_url is not None,
        "card_payment_url": card_payment_url,
        "card_payment_amount": card_payment_amount,
        # Live, not frozen: an unpaid invoice must point at the account
        # that is current now. Both the screen and the PDF come through
        # here, so they cannot disagree.
        **venue_identity(invoice.booking.venue),
    }


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invoice(token: str, request: Request, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    if invoice.status == InvoiceStatus.cancelled or invoice.booking.status in VOIDED_STATUSES:
        return _unavailable_response(request, invoice)
    invoice = invoicing.record_view(db, invoice)
    context = _build_invoice_context(db, invoice, include_card_payment=True)
    return templates.TemplateResponse(request, "invoice.html", context)


@router.get("/i/{token}/pdf")
def download_invoice_pdf(token: str, request: Request, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    if invoice.status == InvoiceStatus.cancelled or invoice.booking.status in VOIDED_STATUSES:
        return _unavailable_response(request, invoice)
    # The PDF carries the same working card link as the web invoice -- a
    # client sent the PDF can pay by card straight from it, rather than being
    # told to "contact us" for something the web version already offers. This
    # does mean a Stripe payment link is minted per download, matching the
    # web view's per-view behaviour.
    context = _build_invoice_context(db, invoice, include_card_payment=True)
    html = templates.get_template("invoice.html").render(**context)
    pdf_bytes = render_html_to_pdf(html)
    invoice_label = "Deposit" if invoice.type.value == "deposit" else "Final"
    filename = f"{invoice.booking.reference_code}-{invoice_label}-Invoice.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
