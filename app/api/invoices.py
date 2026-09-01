import datetime as dt
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.invoice import InvoiceStatus
from app.services import invoicing, stripe_integration
from app.services.pdf import render_html_to_pdf
from app.templating import templates
from app.utils import looks_like_a_token

router = APIRouter(tags=["invoices"])
logger = logging.getLogger(__name__)


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
            card_payment_url = stripe_integration.create_payment_link(invoice, card_payment_amount)
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
    }


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invoice(token: str, request: Request, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    invoice = invoicing.record_view(db, invoice)
    context = _build_invoice_context(db, invoice, include_card_payment=True)
    return templates.TemplateResponse(request, "invoice.html", context)


@router.get("/i/{token}/pdf")
def download_invoice_pdf(token: str, db: Session = Depends(get_db)):
    invoice = _get_viewable_invoice_or_404(db, token)
    # No live Stripe call for a static download -- a PDF isn't the primary
    # payment flow, and generating a fresh payment link on every download
    # would be a wasted API call. The PDF falls back to "card payment on
    # request", same as when Stripe isn't configured at all.
    context = _build_invoice_context(db, invoice, include_card_payment=False)
    html = templates.get_template("invoice.html").render(**context)
    pdf_bytes = render_html_to_pdf(html)
    invoice_label = "Deposit" if invoice.type.value == "deposit" else "Final"
    filename = f"{invoice.booking.reference_code}-{invoice_label}-Invoice.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
