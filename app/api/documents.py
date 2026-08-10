from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document import DocumentStatus
from app.rate_limit import InMemoryRateLimiter, client_ip, rate_limit_dependency
from app.services import documents as documents_service
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


def _client_ip(request: Request) -> str:
    # Railway (and most PaaS) sits behind a proxy, so request.client.host
    # would just be the proxy's address -- prefer the first hop of
    # X-Forwarded-For, which is the actual client. Truncated defensively:
    # a forged/garbage header should never be able to overflow the
    # signer_ip column and crash a real signing attempt.
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
    if document is None or document.status == DocumentStatus.draft:
        raise HTTPException(status_code=404, detail="Document not found")

    document = documents_service.record_view(db, document)

    return templates.TemplateResponse(
        request, "document.html", {"document": document, "booking": document.booking}
    )


@router.get("/d/{token}/pdf")
def download_document_pdf(token: str, db: Session = Depends(get_db)):
    if not looks_like_a_token(token):
        raise HTTPException(status_code=404, detail="Document not found")

    document = documents_service.get_by_token(db, token)
    if document is None or document.status == DocumentStatus.draft:
        raise HTTPException(status_code=404, detail="Document not found")

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
    if document is None or document.status == DocumentStatus.draft:
        raise HTTPException(status_code=404, detail="Document not found")

    signer_name = signer_name.strip()
    if not signer_name:
        raise HTTPException(status_code=422, detail="Name is required to sign")

    try:
        documents_service.sign(db, document, signer_name=signer_name, signer_ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RedirectResponse(url=f"/d/{token}", status_code=303)
