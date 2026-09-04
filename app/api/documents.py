from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document import DocumentStatus
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.services import documents as documents_service
from app.services import policy
from app.services.booking import TERMINAL_STATUSES
from app.services.pdf import render_html_to_pdf
from app.templating import templates
from app.utils import looks_like_a_token, truncate

router = APIRouter(tags=["documents"])

# Signing is a low-frequency, deliberate action -- this just blocks
# scripted brute-forcing of the sign endpoint, not real client use
# (including a genuine double-click retry, which this comfortably allows).
_sign_rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=300)

SIGNER_IP_MAX_LENGTH = 45  # matches Document.signer_ip column width
BOOKING_EVENT_ACTOR_MAX_LENGTH = 255


def _is_live(document) -> bool:
    """False once the booking behind this document has moved to a
    terminal status (see app.services.booking.change_status, which
    cancels the booking's live invoices on the same move -- this is the
    document-side half) or a newer version has superseded this one. A
    real incident, 2026-09-04: Sophie Mavridis still had a working sign
    link after her offer was superseded by Chanai Duncombe confirming the
    same room and night."""
    return document.booking.status not in TERMINAL_STATUSES and document.is_current


def _unavailable_response(request: Request, document) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "link_unavailable.html",
        {
            "booking": document.booking,
            "contact_email": policy.VENUE_CONTACT_EMAIL,
            "message": "This link is no longer active. Get in touch and we'll help directly.",
        },
        status_code=410,
    )


def _client_ip(request: Request) -> str:
    # Delegates to the shared client_ip (app/rate_limit.py), which derives
    # the real client from the trusted end of X-Forwarded-For rather than
    # the spoofable leftmost hop -- so signer_ip records the actual signer,
    # not a value the signer chose. Truncated defensively: a forged/garbage
    # header must never overflow the signer_ip column and crash a real
    # signing attempt.
    return truncate(client_ip(request), SIGNER_IP_MAX_LENGTH)


@router.get("/d/{token}", response_class=HTMLResponse)
def view_document(token: str, request: Request, db: Session = Depends(get_db)):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Document not found")

    document = documents_service.get_by_token(db, token)
    # A draft is not yet human-approved for client eyes -- treat its link
    # as if it doesn't exist yet, same as an unknown token, rather than
    # leaking draft content to anyone who happens to have (or guesses at)
    # a link that was never actually sent.
    if document is None or document.status == DocumentStatus.draft or document.is_legacy:
        # Legacy documents carry placeholder content (the real record is the
        # uploaded PDF) and must NEVER render to a client -- treat the public
        # link as if it doesn't exist. Staff download the stored PDF from the
        # admin instead.
        raise HTTPException(status_code=404, detail="Document not found")
    if not _is_live(document):
        return _unavailable_response(request, document)

    document = documents_service.record_view(db, document)

    return templates.TemplateResponse(
        request, "document.html", {"document": document, "booking": document.booking}
    )


@router.get("/d/{token}/pdf")
def download_document_pdf(token: str, request: Request, db: Session = Depends(get_db)):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Document not found")

    document = documents_service.get_by_token(db, token)
    if document is None or document.status == DocumentStatus.draft or document.is_legacy:
        # Legacy documents carry placeholder content (the real record is the
        # uploaded PDF) and must NEVER render to a client -- treat the public
        # link as if it doesn't exist. Staff download the stored PDF from the
        # admin instead.
        raise HTTPException(status_code=404, detail="Document not found")
    if not _is_live(document):
        return _unavailable_response(request, document)

    html = templates.get_template("document.html").render(document=document, booking=document.booking, is_pdf=True)
    pdf_bytes = render_html_to_pdf(html)
    doc_label = "Agreement" if document.type.value == "agreement" else "BEO"
    filename = f"{document.booking.reference_code}-{doc_label}-v{document.version}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/d/{token}/sign", dependencies=[Depends(rate_limit_dependency(_sign_rate_limiter))])
def sign_document(
    token: str,
    request: Request,
    signer_name: str = Form(..., max_length=255),
    db: Session = Depends(get_db),
):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Document not found")

    document = documents_service.get_by_token(db, token)
    if document is None or document.status == DocumentStatus.draft or document.is_legacy:
        # Legacy documents carry placeholder content (the real record is the
        # uploaded PDF) and must NEVER render to a client -- treat the public
        # link as if it doesn't exist. Staff download the stored PDF from the
        # admin instead.
        raise HTTPException(status_code=404, detail="Document not found")
    if not _is_live(document):
        raise HTTPException(status_code=410, detail="This offer is no longer available. Please get in touch.")

    signer_name = signer_name.strip()
    if not signer_name:
        raise HTTPException(status_code=422, detail="Name is required to sign")

    try:
        documents_service.sign(db, document, signer_name=signer_name, signer_ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RedirectResponse(url=f"/d/{token}", status_code=303)
