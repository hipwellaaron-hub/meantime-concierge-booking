"""HTML-to-PDF rendering for the public document/invoice views. Renders the
exact same template used for the HTML view, so the PDF can never drift
from what a client sees on the page -- there is no second source of truth
for layout or content here.

xhtml2pdf (pure Python, reportlab-based) rather than a library needing
native system packages (e.g. WeasyPrint's Pango/Cairo/GDK) -- Railway's
Railpack Python build has no straightforward way to install those, and a
PDF download failing at runtime because a shared library is missing would
be a much worse failure mode than xhtml2pdf's more limited CSS support.
"""

import io
import os

from xhtml2pdf import pisa

STATIC_URL_PREFIX = "/static/"
STATIC_ROOT = os.path.join("app", "static")


class PDFRenderError(Exception):
    pass


def _link_callback(uri: str, rel) -> str:
    """Resolves the /static/ URLs in the template to real files on disk.
    Without this xhtml2pdf resolves them relative to the process's working
    directory and silently drops the image -- there is no HTTP fetch here,
    unlike a browser rendering the same markup."""
    if uri.startswith(STATIC_URL_PREFIX):
        return os.path.abspath(os.path.join(STATIC_ROOT, uri[len(STATIC_URL_PREFIX):]))
    return uri


def render_html_to_pdf(html: str) -> bytes:
    buffer = io.BytesIO()
    result = pisa.CreatePDF(io.StringIO(html), dest=buffer, link_callback=_link_callback)
    if result.err:
        raise PDFRenderError(f"xhtml2pdf reported {result.err} error(s) rendering this document")
    return buffer.getvalue()
