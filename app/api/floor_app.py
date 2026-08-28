"""Serves the Meantime Floor PWA shell: one self-contained HTML page (no
build step, same approach as the wizard), a web-app manifest so it
installs to a home screen, and a minimal service worker. All actual data
goes through the token-gated /api/staff routes -- this router serves only
static shell, so none of it needs auth.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.templating import templates

router = APIRouter(tags=["floor-app"])

# The venue-dark palette the wizard established.
_MANIFEST = """{
  "name": "Meantime Floor",
  "short_name": "Floor",
  "description": "Meantime Hamilton functions: what's on, and who's paid.",
  "start_url": "/floor",
  "scope": "/floor",
  "display": "standalone",
  "background_color": "#141210",
  "theme_color": "#141210",
  "icons": [
    {
      "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'%3E%3Crect width='512' height='512' rx='96' fill='%23141210'/%3E%3Ctext x='256' y='330' font-family='Georgia,serif' font-size='240' fill='%23c9a96e' text-anchor='middle'%3EM%3C/text%3E%3C/svg%3E",
      "sizes": "512x512",
      "type": "image/svg+xml",
      "purpose": "any"
    }
  ]
}"""

# Cache-first for the shell so the app opens instantly; network-only for
# the API -- the brief promises refresh-on-open, not offline data.
_SERVICE_WORKER = """
const SHELL_CACHE = "floor-shell-v1";
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(["/floor"])));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // network only, always fresh
  if (url.pathname === "/floor") {
    e.respondWith(
      fetch(e.request).then((resp) => {
        const copy = resp.clone();
        caches.open(SHELL_CACHE).then((c) => c.put(e.request, copy));
        return resp;
      }).catch(() => caches.match(e.request))
    );
  }
});
"""


@router.get("/floor", response_class=HTMLResponse)
def floor_app(request: Request):
    return templates.TemplateResponse(request, "floor/floor.html", {})


@router.get("/floor/manifest.json")
def floor_manifest():
    return Response(content=_MANIFEST, media_type="application/manifest+json")


@router.get("/floor/sw.js")
def floor_service_worker():
    return Response(
        content=_SERVICE_WORKER,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/floor"},
    )
