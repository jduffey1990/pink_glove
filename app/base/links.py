"""
Links into the frontend that carry a secret token (ADR-036).

The token goes in the URL fragment (`/pay#token=...`), which a browser never
sends to a server. In the path or the query it would be written to the
access log on the page load (the SPA is served from the API's origin,
ADR-027), and it would travel in the Referer header. Every emailed link with a
token in it is built here, so the shape is stated once.

The pages read the fragment in `ui/src/lib/linkToken.ts`; the key below and
`TOKEN_KEY` there must agree.
"""

from urllib.parse import urlencode

from django.conf import settings

#: The fragment's key. Mirrored by `TOKEN_KEY` in `ui/src/lib/linkToken.ts`.
TOKEN_KEY = "token"


def token_link(path: str, token: str, query: dict[str, str] | None = None) -> str:
    """
    `FRONTEND_BASE_URL` + `path` + `?query` + `#token=<token>`.

    The query, if any, comes before the fragment: the shape Stripe Checkout
    was seen to hand back unchanged (ADR-036).
    """
    search = f"?{urlencode(query)}" if query else ""
    fragment = urlencode({TOKEN_KEY: token}, safe=":")
    return f"{settings.FRONTEND_BASE_URL}{path}{search}#{fragment}"
