import datetime as dt
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.invoice import InvoiceStatus
from app.services import invoicing, stripe_integration
from app.templating import templates
from app.utils import looks_like_a_token

router = APIRouter(tags=["invoices"])
logger = logging.getLogger(__name__)


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invoice(token: str, request: Request, db: Session = Depends(get_db)):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Invoice not found")

    invoice = invoicing.get_by_token(db, token)
    # Same policy as documents: a draft hasn't been human-approved to show
    # a client yet, so treat its link as not existing rather than leaking it.
    if invoice is None or invoice.status == InvoiceStatus.draft:
        raise HTTPException(status_code=404, detail="Invoice not found")

    summary = invoicing.get_payment_summary(db, invoice)

    card_payment_url = None
    card_payment_amount = None
    if stripe_integration.is_configured() and not summary["is_fully_paid"]:
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

    return templates.TemplateResponse(
        request,
        "invoice.html",
        {
            "invoice": invoice,
            "booking": invoice.booking,
            "summary": summary,
            "stripe_configured": card_payment_url is not None,
            "card_payment_url": card_payment_url,
            "card_payment_amount": card_payment_amount,
        },
    )
