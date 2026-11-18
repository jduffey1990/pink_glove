/**
 * A secret token in an emailed link (ADR-036).
 *
 * The pay, invitation and reset links carry their token in the URL fragment,
 * `/pay#token=...`, which a browser never sends to a server: not in the
 * request line the access log writes down, and not in a Referer. The server
 * builds those links in `app/base/links.py`; `TOKEN_KEY` must agree with the
 * key there.
 *
 * Links already in inboxes have the token in the path (`/pay/<token>`). Their
 * routes redirect to the fragment form with `fragmentRedirect`, so the old
 * address is replaced before anything renders and leaves no history entry.
 * The page load that brought it here has already been logged; nothing in the
 * browser can take that back.
 */

import type { RouteRecordRedirectOption } from 'vue-router'

/** The fragment's key. Mirrored by `TOKEN_KEY` in `app/base/links.py`. */
export const TOKEN_KEY = 'token'

/**
 * Shown when the address has no token at all, which the server never saw:
 * a link cut short by a mail client, or the page opened by hand.
 */
export const INCOMPLETE_LINK = 'That link is incomplete. Open it again from the email, or ask for a new one.'

/**
 * The token in a route's hash (`#token=...`), or '' when there is none.
 * vue-router hands `route.hash` over already decoded.
 */
export function tokenFromHash (hash: string): string {
  return new URLSearchParams(hash.replace(/^#/, '')).get(TOKEN_KEY)?.trim() ?? ''
}

/** The hash that carries `token`, for a route location. */
export function tokenHash (token: string): string {
  return `#${TOKEN_KEY}=${token}`
}

/**
 * A route record's `redirect` for the old form, `/<path>/:token`: the same
 * page by name, the token moved to the fragment, the query kept (Stripe's
 * `?paid=1` arrives this way). A redirect is resolved before the URL is
 * written, so the old address replaces the entry it arrived in.
 */
export function fragmentRedirect (name: string): RouteRecordRedirectOption {
  return to => ({
    name,
    query: to.query,
    hash: tokenHash(String(to.params.token ?? '')),
  })
}
