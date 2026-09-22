"""
Stripe events for tests, signed the way Stripe signs them.

Tests never reach Stripe. The webhook is fed bytes and a `Stripe-Signature`
header made here, so `stripe.Webhook.construct_event` runs for real with a
secret the test chose; the `stripe` client is patched at the
`billing.connect` boundary for everything else.

Payload shapes follow Stripe's documented objects, trimmed to the fields the
handlers read. Trimming is deliberate: a handler that needs a field these
fixtures lack fails a test before it fails on a real event.
"""

import hashlib
import hmac
import json
import time

from django.conf import settings

#: The signing secret tests hand to the webhook. Not a real one.
WEBHOOK_SECRET = "whsec_test_not_a_real_secret"

#: A connected account id in the shape Stripe uses.
ACCOUNT_ID = "acct_1TestConnectedAcct"


def signature_header(body: bytes, secret: str = WEBHOOK_SECRET, *, timestamp: int | None = None):
    """
    The `Stripe-Signature` header for `body`: `t=<unix>,v1=<hmac-sha256>`
    over `"{t}.{body}"`, which is exactly what the SDK verifies.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def signed(event: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    """The (body, header) pair to POST to the webhook."""
    body = json.dumps(event).encode()
    return body, signature_header(body, secret)


def event(
    type_: str,
    obj: dict,
    *,
    event_id: str = "evt_test_1",
    account: str | None = ACCOUNT_ID,
    created: int | None = None,
) -> dict:
    """
    One Stripe event envelope. `account` is what makes it a Connect event;
    pass `None` for a platform event (Phase 4c).
    """
    envelope = {
        "id": event_id,
        "object": "event",
        "api_version": settings.STRIPE_API_VERSION,
        "created": int(time.time()) if created is None else created,
        "livemode": False,
        "type": type_,
        "data": {"object": obj},
    }
    if account is not None:
        envelope["account"] = account
    return envelope
