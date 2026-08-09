from fastapi import FastAPI
from starlette.responses import PlainTextResponse

from app.api.availability import router as availability_router
from app.api.documents import router as documents_router
from app.api.enquiries import router as enquiries_router
from app.api.invoices import router as invoices_router
from app.api.webhooks import router as webhooks_router

# Generous for the JSON/form payloads this app actually receives (an
# enquiry, a signature) -- blocks gross abuse, not real use. Only catches
# requests that declare an honest Content-Length; a client deliberately
# using chunked transfer-encoding to omit it could still stream an
# unbounded body. Acceptable for a single small venue's public forms;
# would need a streaming byte-count guard to be airtight.
MAX_BODY_SIZE = 200_000


class MaxBodySizeMiddleware:
    def __init__(self, app, max_size: int):
        self.app = app
        self.max_size = max_size

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    too_large = int(content_length) > self.max_size
                except ValueError:
                    too_large = False
                if too_large:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app = FastAPI(title="Meantime Concierge")
app.add_middleware(MaxBodySizeMiddleware, max_size=MAX_BODY_SIZE)

app.include_router(availability_router)
app.include_router(documents_router)
app.include_router(invoices_router)
app.include_router(enquiries_router)
app.include_router(webhooks_router)


@app.get("/health")
def health():
    return {"status": "ok"}
