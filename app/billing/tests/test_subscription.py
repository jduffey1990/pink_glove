"""
`billing.subscription`, with Stripe patched out at the client.

What is tested is what this module decides: when a Customer is created and
when not, what the Checkout Session is asked for, when the portal opens,
and what `refresh_subscription` copies from a Subscription -- in
particular which end of the period counts as paid for. The SDK's own
behaviour is not.
"""

import datetime as dt
from types import SimpleNamespace
from unittest import mock

import pytest

from app.exceptions import ConflictError, ServiceUnavailableError
from billing import connect, subscription
from billing.tests.stripe_fixtures import (
    CUSTOMER_ID,
    PRICE_ID,
    SUBSCRIPTION_ID,
    stripe_object,
    subscription_object,
)
from scheduling.tests.factories import OrganizationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("platform_billing_on")]

NOW = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def ts(when: dt.datetime) -> int:
    return int(when.timestamp())


@pytest.fixture
def api():
    """A fake StripeClient whose calls the tests inspect."""
    fake = mock.MagicMock(name="StripeClient")
    fake.v1.customers.create.return_value = SimpleNamespace(id=CUSTOMER_ID)
    fake.v1.checkout.sessions.create.return_value = SimpleNamespace(
        url="https://checkout.stripe.com/c/subscribe/x"
    )
    fake.v1.billing_portal.sessions.create.return_value = SimpleNamespace(
        url="https://billing.stripe.com/p/session/x"
    )
    fake.v1.subscriptions.retrieve.return_value = stripe_object(
        subscription_object(period_start=ts(NOW - 10 * DAY), period_end=ts(NOW + 20 * DAY))
    )
    with mock.patch.object(connect, "client", return_value=fake):
        yield fake


@pytest.fixture
def subscriber(db):
    """An organization whose trial is long over and which holds a subscription."""
    return OrganizationFactory(
        trial_ends_at=NOW - 40 * DAY,
        stripe_customer_id=CUSTOMER_ID,
        stripe_subscription_id=SUBSCRIPTION_ID,
        subscription_status="active",
        subscription_paid_through=NOW - 10 * DAY,
    )


class TestDisabled:
    def test_every_entry_point_is_a_503_without_a_price(self, settings, api):
        settings.PLATFORM_BILLING_ENABLED = False
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)

        with pytest.raises(ServiceUnavailableError, match="not configured"):
            subscription.start_checkout(organization)
        with pytest.raises(ServiceUnavailableError, match="not configured"):
            subscription.portal_url(organization)
        api.v1.customers.create.assert_not_called()

    def test_a_refresh_with_stripe_off_touches_nothing(self, settings, api, subscriber):
        settings.STRIPE_ENABLED = False

        subscription.refresh_subscription(subscriber, now=NOW)

        api.v1.subscriptions.retrieve.assert_not_called()


class TestStartCheckout:
    def test_creates_the_customer_once_and_a_session_every_time(self, api):
        organization = OrganizationFactory(name="Sparkle Clean", email="owner@sparkle.test")

        first = subscription.start_checkout(organization)
        second = subscription.start_checkout(organization)

        assert first == second == "https://checkout.stripe.com/c/subscribe/x"
        assert api.v1.customers.create.call_count == 1
        assert api.v1.checkout.sessions.create.call_count == 2
        organization.refresh_from_db()
        assert organization.stripe_customer_id == CUSTOMER_ID

    def test_the_customer_is_the_organization(self, api):
        organization = OrganizationFactory(name="Sparkle Clean", email="owner@sparkle.test")

        subscription.start_checkout(organization)

        params = api.v1.customers.create.call_args.args[0]
        assert params["name"] == "Sparkle Clean"
        assert params["email"] == "owner@sparkle.test"
        assert params["metadata"] == {"organization_id": str(organization.pk)}

    def test_the_session_subscribes_the_customer_to_the_configured_price(self, api):
        organization = OrganizationFactory()

        subscription.start_checkout(organization)

        params = api.v1.checkout.sessions.create.call_args.args[0]
        assert params["mode"] == "subscription"
        assert params["customer"] == CUSTOMER_ID
        assert params["line_items"] == [{"price": PRICE_ID, "quantity": 1}]
        # Never on a connected account: no options at all.
        assert "options" not in api.v1.checkout.sessions.create.call_args.kwargs

    def test_the_organization_rides_on_the_session_and_the_subscription(self, api):
        """The Subscription is what later events name, and it does not inherit."""
        organization = OrganizationFactory()

        subscription.start_checkout(organization)

        params = api.v1.checkout.sessions.create.call_args.args[0]
        metadata = {"organization_id": str(organization.pk)}
        assert params["client_reference_id"] == str(organization.pk)
        assert params["metadata"] == metadata
        assert params["subscription_data"] == {"metadata": metadata}

    def test_it_returns_to_the_settings_page(self, api):
        subscription.start_checkout(OrganizationFactory())

        params = api.v1.checkout.sessions.create.call_args.args[0]
        settings_page = "https://app.example.test/billing/settings"
        assert params["success_url"] == f"{settings_page}?subscription=return"
        assert params["cancel_url"] == f"{settings_page}?subscription=cancelled"

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
    def test_a_live_subscription_is_not_subscribed_again(self, api, subscriber, status):
        subscriber.subscription_status = status
        subscriber.save()

        with pytest.raises(ConflictError, match="already has a subscription"):
            subscription.start_checkout(subscriber)

        api.v1.checkout.sessions.create.assert_not_called()

    def test_a_cancelled_customer_subscribes_again_without_a_second_customer(self, api, subscriber):
        subscriber.subscription_status = "canceled"
        subscriber.save()

        subscription.start_checkout(subscriber)

        api.v1.customers.create.assert_not_called()
        assert api.v1.checkout.sessions.create.call_args.args[0]["customer"] == CUSTOMER_ID

    def test_an_organization_switched_off_by_hand_is_refused(self, api):
        """Billing would never turn it back on, so the money would buy nothing."""
        organization = OrganizationFactory(is_active=False)

        with pytest.raises(ConflictError, match="not for billing"):
            subscription.start_checkout(organization)

        api.v1.customers.create.assert_not_called()

    def test_one_billing_switched_off_may_subscribe(self, api):
        organization = OrganizationFactory(is_active=False, deactivated_for_billing_at=NOW)

        subscription.start_checkout(organization)

        api.v1.checkout.sessions.create.assert_called_once()

    def test_a_complimentary_organization_is_refused(self, api):
        with pytest.raises(ConflictError, match="complimentary"):
            subscription.start_checkout(OrganizationFactory(billing_exempt=True))

        api.v1.customers.create.assert_not_called()

    def test_a_stripe_refusal_is_a_503_and_writes_nothing(self, api):
        import stripe

        api.v1.customers.create.side_effect = stripe.InvalidRequestError("No such price.", None)
        organization = OrganizationFactory()

        with pytest.raises(ServiceUnavailableError, match="No such price"):
            subscription.start_checkout(organization)

        organization.refresh_from_db()
        assert organization.stripe_customer_id == ""


class TestPortal:
    def test_opens_the_portal_for_the_customer_and_returns_to_settings(self, api, subscriber):
        url = subscription.portal_url(subscriber)

        assert url == "https://billing.stripe.com/p/session/x"
        params = api.v1.billing_portal.sessions.create.call_args.args[0]
        assert params == {
            "customer": CUSTOMER_ID,
            "return_url": "https://app.example.test/billing/settings",
        }

    def test_there_is_nothing_to_manage_before_subscribing(self, api):
        with pytest.raises(ConflictError, match="Subscribe first"):
            subscription.portal_url(OrganizationFactory())

        api.v1.billing_portal.sessions.create.assert_not_called()


class TestRefreshSubscription:
    def retrieve(self, api, **kwargs):
        kwargs.setdefault("period_start", ts(NOW - 10 * DAY))
        kwargs.setdefault("period_end", ts(NOW + 20 * DAY))
        api.v1.subscriptions.retrieve.return_value = stripe_object(subscription_object(**kwargs))

    def test_a_no_op_without_a_subscription_id(self, api):
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)

        subscription.refresh_subscription(organization, now=NOW)

        api.v1.subscriptions.retrieve.assert_not_called()

    def test_reads_the_subscription_with_its_latest_invoice(self, api, subscriber):
        subscription.refresh_subscription(subscriber, now=NOW)

        api.v1.subscriptions.retrieve.assert_called_once_with(
            SUBSCRIPTION_ID, {"expand": ["latest_invoice"]}
        )

    def test_a_paid_period_is_covered_to_its_end(self, api, subscriber):
        self.retrieve(api, status="active", latest_invoice_status="paid")

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_status == "active"
        assert subscriber.subscription_paid_through == NOW + 20 * DAY
        assert subscriber.subscription_cancel_at_period_end is False

    def test_a_renewal_stripe_is_still_collecting_is_covered_only_to_its_start(
        self, api, subscriber
    ):
        """The period has advanced; the money has not. No free month."""
        self.retrieve(api, status="past_due", latest_invoice_status="open")

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_status == "past_due"
        assert subscriber.subscription_paid_through == NOW - 10 * DAY

    def test_a_second_unpaid_cycle_does_not_move_the_date_forward(self, api, subscriber):
        """Stripe advances the period monthly even unpaid; the last paid end does not move."""
        subscriber.subscription_paid_through = NOW - 40 * DAY
        subscriber.save()
        self.retrieve(
            api,
            status="past_due",
            latest_invoice_status="open",
            period_start=ts(NOW - 10 * DAY),
            period_end=ts(NOW + 20 * DAY),
        )

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_paid_through == NOW - 40 * DAY

    def test_a_paid_read_still_moves_it_forward(self, api, subscriber):
        subscriber.subscription_paid_through = NOW - 40 * DAY
        subscriber.save()
        self.retrieve(api, status="active", latest_invoice_status="paid")

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_paid_through == NOW + 20 * DAY

    def test_a_subscription_of_another_customer_is_not_mirrored(self, api, subscriber):
        """Routing makes this impossible; the check makes it harmless."""
        self.retrieve(api, status="active", customer="cus_somebody_else")
        subscriber.subscription_status = "canceled"
        subscriber.save()

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_status == "canceled"

    def test_a_stripe_trial_is_covered_to_its_end(self, api, subscriber):
        self.retrieve(api, status="trialing", latest_invoice_status=None)

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_paid_through == NOW + 20 * DAY

    def test_an_unexpanded_invoice_is_not_taken_as_paid(self, api, subscriber):
        values = subscription_object(period_start=ts(NOW - 10 * DAY), period_end=ts(NOW + 20 * DAY))
        values["latest_invoice"] = "in_test_1"
        api.v1.subscriptions.retrieve.return_value = stripe_object(values)

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_paid_through == NOW - 10 * DAY

    def test_cancel_at_period_end_is_copied(self, api, subscriber):
        self.retrieve(api, cancel_at_period_end=True)

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_cancel_at_period_end is True

    def test_a_subscription_with_no_items_has_no_paid_period(self, api, subscriber):
        values = subscription_object(period_start=0, period_end=0)
        values["items"]["data"] = []
        api.v1.subscriptions.retrieve.return_value = stripe_object(values)

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.subscription_paid_through is None

    def test_a_payment_coming_good_reactivates_in_the_same_call(self, api, subscriber):
        subscriber.is_active = False
        subscriber.deactivated_for_billing_at = NOW - DAY
        subscriber.save()
        self.retrieve(api, status="active", latest_invoice_status="paid")

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.is_active is True
        assert subscriber.deactivated_for_billing_at is None

    def test_a_lapse_past_the_grace_deactivates_in_the_same_call(self, api, subscriber):
        self.retrieve(
            api,
            status="canceled",
            period_start=ts(NOW - 60 * DAY),
            period_end=ts(NOW - 30 * DAY),
        )

        subscription.refresh_subscription(subscriber, now=NOW)

        subscriber.refresh_from_db()
        assert subscriber.is_active is False
        assert subscriber.deactivated_for_billing_at == NOW

    def test_a_stripe_refusal_is_a_503(self, api, subscriber):
        import stripe

        api.v1.subscriptions.retrieve.side_effect = stripe.InvalidRequestError(
            "No such subscription.", None
        )

        with pytest.raises(ServiceUnavailableError, match="read the subscription"):
            subscription.refresh_subscription(subscriber, now=NOW)
