from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document import DocumentStatus, DocumentType
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.services import documents as documents_service
from app.services import policy
from app.services.booking import VOIDED_STATUSES
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
    """False once the event behind this document is off (see
    app.services.booking.VOIDED_STATUSES and change_status, which cancels
    the booking's live invoices on the same move -- this is the
    document-side half) or a newer version has superseded this one. A
    real incident, 2026-09-04: Sophie Mavridis still had a working sign
    link after her offer was superseded by Chanai Duncombe confirming the
    same room and night.

    VOIDED_STATUSES, not TERMINAL_STATUSES: a client whose event has
    simply HAPPENED keeps access to the agreement they signed. Gating on
    the wider tuple meant a signed contract 410'd the moment staff ticked
    the booking completed, so anyone wanting to re-read the cancellation
    terms they had agreed to was told the link no longer existed."""
    return document.booking.status not in VOIDED_STATUSES and document.is_current


BEING_UPDATED_MESSAGE = (
    "This document is being updated. Please check back shortly -- you'll be sent a new link "
    "once it's ready."
)
NO_LONGER_ACTIVE_MESSAGE = "This link is no longer active. Get in touch and we'll help directly."


def _being_updated(db: Session, document) -> bool:
    """Whether the replacement for this link is still a DRAFT.

    Between a staff member pressing Revise and pressing Send, the client
    holds a link to a superseded version and there is no new one to give
    them yet. Telling them "this link is no longer active" is true and
    useless: nothing has gone wrong and they do not need to ring anybody.
    Most of these windows are minutes; the ones that are not are exactly
    the ones where a client should be told to wait rather than to chase
    (Aaron's ruling, 2026-09-08).

    A version superseded by another SENT one is the other case -- they were
    given a newer link -- and keeps the ordinary message.

    So is a VOIDED booking. _is_live fails for two different reasons, and
    only one of them is "a replacement is on its way": a client whose event
    has been cancelled was being told to check back shortly for a link that
    is never coming, which is a worse sentence than the plain one. Proved
    over HTTP before this line existed -- revise a sent Event Order, cancel
    the booking, and the dead link promised a new one.
    """
    if document.booking.status in VOIDED_STATUSES:
        return False
    current = documents_service.get_current(db, document.booking_id, document.type)
    return current is not None and current.status == DocumentStatus.draft


def _unavailable_response(request: Request, document, *, being_updated: bool = False) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "link_unavailable.html",
        {
            "booking": document.booking,
            "contact_email": policy.VENUE_CONTACT_EMAIL,
            "message": BEING_UPDATED_MESSAGE if being_updated else NO_LONGER_ACTIVE_MESSAGE,
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
        return _unavailable_response(request, document, being_updated=_being_updated(db, document))

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
        return _unavailable_response(request, document, being_updated=_being_updated(db, document))

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
    accept_lock: str | None = Form(None),
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
        # The same card the GET shows, not raw JSON. This is the one refusal
        # a real client hits: staff press Revise while the approval page is
        # open, is_current clears, and the click lands here. Before this the
        # POST answered {"detail": "This offer is no longer available..."}
        # while a refresh of the same link said "being updated -- you'll be
        # sent a new link". One route, one answer.
        return _unavailable_response(request, document, being_updated=_being_updated(db, document))

    signer_name = signer_name.strip()
    if not signer_name:
        raise HTTPException(status_code=422, detail="Name is required to sign")
    if document.type == DocumentType.beo and accept_lock != "yes":
        # Approving an Event Order is three things at once, and the third
        # is the one that matters: their name, the event date as printed,
        # and that approval LOCKS this version (Aaron's ruling,
        # 2026-09-10). The checkbox is required in the HTML; this is the
        # server saying so too, because a form field can be posted without
        # the page.
        raise HTTPException(
            status_code=422,
            detail="To approve, please tick the box confirming the details and the date are correct "
            "and that approval locks this Event Order.",
        )

    try:
        documents_service.sign(db, document, signer_name=signer_name, signer_ip=_client_ip(request))
    except ValueError as exc:
        db.refresh(document)
        if document.status == DocumentStatus.signed:
            # A double-clicked button, or a retry after a slow response: the
            # first click already did it. Show them the signed page rather
            # than {"detail": "cannot sign a document with status signed"}.
            return RedirectResponse(url=f"/d/{token}", status_code=303)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RedirectResponse(url=f"/d/{token}", status_code=303)
