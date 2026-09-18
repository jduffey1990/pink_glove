"""
Serves the built single-page app from the same origin as the API (ADR-027).

Vite writes `index.html` and a folder of content-hashed assets. Whitenoise
serves the assets straight from `UI_DIST_DIR` (see `WHITENOISE_ROOT` in
settings); this view answers everything else the router might own --
`/schedule`, `/jobs/<id>`, a bookmarked invoice -- with that one `index.html`,
and the client-side router takes it from there.

Two things are deliberate:

- The file is read once, at first request, and held. It is a few kilobytes and
  it never changes while the process lives: a new build is a new image.
- `Cache-Control: no-cache` on the document. The assets it names are immutable
  and cached for a year; the document is what points at the current set, so a
  browser must revalidate it or it keeps loading last week's build after a
  deploy. `no-cache` still allows a conditional request (ETag), so it is
  cheap.

Which paths reach here is decided in `urls.py`, not here: the API, the admin,
the health probes, static and media are matched first, so an unknown API path
is a JSON 404 and never a 200 with a page in it.
"""

from __future__ import annotations

import functools
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.views.decorators.http import require_http_methods

_MISSING = (
    "The frontend build is not present on this server. Set UI_DIST_DIR to a "
    "directory containing Vite's output, or build the image from the repository "
    "root so it is baked in. In development, use the Vite dev server instead."
)


def dist_dir() -> Path | None:
    """`UI_DIST_DIR` as a path, or None when unset -- the SPA is then not served."""
    raw = getattr(settings, "UI_DIST_DIR", "")
    return Path(raw) if raw else None


@functools.lru_cache(maxsize=1)
def _read_index(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


# HEAD too: uptime monitors and `curl -I` ask that way.
@require_http_methods(["GET", "HEAD"])
def spa_index(request: HttpRequest) -> HttpResponse:
    root = dist_dir()
    body = _read_index(str(root / "index.html")) if root else None
    if body is None:
        return HttpResponseNotFound(_MISSING, content_type="text/plain")

    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response
