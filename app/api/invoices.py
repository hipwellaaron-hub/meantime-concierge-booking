import datetime as dt
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.invoice import InvoiceStatus
from app.services import invoicing, policy, stripe_integration
from app.services.booking import TERMINAL_STATUSES
from app.services.pdf import render_html_to_pdf
from app.templating import templates
from app.utils import looks_like_a_token

router = APIRouter(tags=["invoices"])
logger = logging.getLogger(__name__)


def _unavailable_response(request: Request, invoice) -> HTMLResponse:
    # Reached once a booking has moved to a terminal status (see
    # app.services.booking.change_status, which cancels every live
    # invoice on the way) or an invoice was cancelled directly -- either
    # way, nothing on it is payable any more. A real incident, 2026-09-04:
    # Sophie Mavridis still had a working card link after her offer on the
    # Loft was superseded. 410, not 404: this token is not unknown, it is
    # deliberately no longer live.
    return templates.TemplateResponse(
        request, "link_unavailable.html",
        {
            "booking": invoice.booking,
            "contact_email": policy.VENUE_CONTACT_EMAIL,
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

    card_payment_url = None
    card_payment_amount = None
    if include_card_payment and stripe_integration.is_configured() and not summary["is_fully_paid"]:
        card_payment_amount = invoicing.calculate_card_payment_amount(summary["balance_due"], dt.date.today())
        try:
            card_payment_url, link_id = stripe_integration.create_payment_link(invoice, card_payment_amount)
            invoicing.record_payment_link(db, invoice, link_id)
        except stripe_integration.StripeNotConfigured:
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

    # Only name the surcharge when one is actually built into the card figure
    # -- a card payment past the legislated surcharge sunset (policy.py) costs
    # the same as the balance, so there is nothing to disclose.
    card_surcharge_pct = None
    if card_payment_amount is not None and card_payment_amount > summary["balance_due"]:
        card_surcharge_pct = f"{policy.CARD_SURCHARGE_RATE * 100:.1f}"

    return {
        "invoice": invoice,
        "booking": invoice.booking,
        "summary": summary,
        "gst_component": invoicing.gst_component(invoice.total),
        "line_items": invoicing.line_item_breakdown(invoice.line_items),
        "other_invoices": other_invoices,
        "stripe_configured": card_payment_url is not None,
        "card_payment_url": card_payment_url,
        "card_payment_amount": card_payment_amount,
        "card_surcharge_pct": card_surcharge_pct,
    }


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invoice(token: str, request: Request, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    if invoice.status == InvoiceStatus.cancelled or invoice.booking.status in TERMINAL_STATUSES:
        return _unavailable_response(request, invoice)
    invoice = invoicing.record_view(db, invoice)
    context = _build_invoice_context(db, invoice, include_card_payment=True)
    return templates.TemplateResponse(request, "invoice.html", context)


@router.get("/i/{token}/pdf")
def download_invoice_pdf(token: str, request: Request, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    if invoice.status == InvoiceStatus.cancelled or invoice.booking.status in TERMINAL_STATUSES:
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
