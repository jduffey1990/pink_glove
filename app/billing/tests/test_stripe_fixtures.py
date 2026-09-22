"""
The test fixtures sign events the way Stripe does.

If this fails, every webhook test that passes is passing for the wrong
reason -- the view would be accepting a signature Stripe never produces.
"""

import stripe

from billing.tests.stripe_fixtures import ACCOUNT_ID, WEBHOOK_SECRET, event, signed


def test_the_sdk_accepts_a_fixture_signature():
    body, header = signed(event("account.updated", {"id": ACCOUNT_ID, "object": "account"}))

    verified = stripe.Webhook.construct_event(body, header, WEBHOOK_SECRET)

    assert verified.id == "evt_test_1"
    assert verified.type == "account.updated"
    assert verified.account == ACCOUNT_ID


def test_the_sdk_rejects_a_fixture_signed_with_another_secret():
    body, header = signed(event("account.updated", {"id": ACCOUNT_ID}), secret="whsec_other")

    try:
        stripe.Webhook.construct_event(body, header, WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        return
    raise AssertionError("a signature under the wrong secret was accepted")


def test_a_platform_event_has_no_account():
    assert "account" not in event("invoice.paid", {"id": "in_1"}, account=None)
