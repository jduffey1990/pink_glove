"""
The platform webhook (Phase 4c), end to end with signed bytes, and the
daily task behind it.

The same view as the Connect endpoint with the platform secret and the
customer resolver; what is tested here is everything that differs -- the
secret, the resolution, the dispatch by row, the handlers' two steps -- and
that the two endpoints are mirror images of each other. `stripe` is never
reached: the signature runs against a secret the tests chose, and
`billing.connect.client` is patched where it would call out.
"""

import datetime as dt
from unittest import mock

import pytest
from django.utils import timezone

from app.exceptions import ServiceUnavailableError
from billing import connect, subscription, webhook
from billing.enums import StripeEventStatus
from billing.models import Payment, StripeEvent
from billing.tests.factories import StripeEventFactory
from billing.tests.stripe_fixtures import (
    ACCOUNT_ID,
    CUSTOMER_ID,
    PLATFORM_WEBHOOK_SECRET,
    SUBSCRIPTION_ID,
    WEBHOOK_SECRET,
    event,
    invoice_object,
    platform_event,
    signed,
    stripe_object,
    subscription_checkout_session,
    subscription_object,
)
from scheduling.tests.factories import OrganizationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("platform_billing_on")]

URL = "/api/billing/stripe/platform-webhook/"
CONNECT_URL = "/api/billing/stripe/webhook/"

NOW = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def ts(when: dt.datetime) -> int:
    return int(when.timestamp())


@pytest.fixture
def enqueue():
    with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
        yield delay


@pytest.fixture
def subscriber(db):
    """An organization whose trial is over, holding the fixture customer and subscription."""
    return OrganizationFactory(
        trial_ends_at=timezone.now() - 40 * DAY,
        stripe_customer_id=CUSTOMER_ID,
        stripe_subscription_id=SUBSCRIPTION_ID,
        subscription_status="active",
        subscription_paid_through=timezone.now() + 20 * DAY,
    )


@pytest.fixture
def api():
    """A fake StripeClient handing back a paid, live subscription."""
    fake = mock.MagicMock(name="StripeClient")
    now = timezone.now()
    fake.v1.subscriptions.retrieve.return_value = stripe_object(
        subscription_object(period_start=ts(now - 10 * DAY), period_end=ts(now + 20 * DAY))
    )
    with mock.patch.object(connect, "client", return_value=fake):
        yield fake


def post(client, body: bytes, header: str, url=URL):
    return client.post(
        url, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=header
    )


def ledgered(organization, type_, obj, **kwargs):
    """A RECEIVED platform row, as `receive` would write it: no account."""
    payload = platform_event(type_, obj, **kwargs)
    return StripeEventFactory(
        event_id=payload["id"],
        account="",
        type=type_,
        organization=organization,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestTheEndpoint:
    def test_the_connect_secret_is_refused_here(self, client, enqueue, subscriber):
        body, header = signed(platform_event("invoice.paid", invoice_object()), WEBHOOK_SECRET)

        assert post(client, body, header).status_code == 400
        assert not StripeEvent.objects.exists()

    def test_and_the_platform_secret_is_refused_there(self, client, enqueue, subscriber):
        """Mirror images: each endpoint verifies with its own secret only."""
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}), PLATFORM_WEBHOOK_SECRET)

        assert post(client, body, header, url=CONNECT_URL).status_code == 400
        assert not StripeEvent.objects.exists()

    def test_is_a_503_without_a_platform_secret(self, client, settings, enqueue):
        settings.STRIPE_PLATFORM_WEBHOOK_SECRET = ""
        body, header = signed(
            platform_event("invoice.paid", invoice_object()), PLATFORM_WEBHOOK_SECRET
        )

        assert post(client, body, header).status_code == 503
        assert not StripeEvent.objects.exists()

    def test_a_handled_event_for_a_known_customer_is_ledgered_and_enqueued(
        self, client, enqueue, subscriber
    ):
        body, header = signed(
            platform_event("invoice.paid", invoice_object(), event_id="evt_p_ok"),
            PLATFORM_WEBHOOK_SECRET,
        )

        assert post(client, body, header).status_code == 200

        row = StripeEvent.objects.get(event_id="evt_p_ok")
        assert row.status == StripeEventStatus.RECEIVED
        assert row.organization == subscriber
        assert row.account == ""
        enqueue.assert_called_once_with(str(subscriber.pk), str(row.pk))

    def test_a_connect_event_here_is_ledgered_ignored(self, client, enqueue, subscriber):
        """Whatever its object says about a customer, an `account` makes it the other endpoint's."""
        obj = {"id": "ch_1", "customer": CUSTOMER_ID}
        body, header = signed(event("charge.refunded", obj), PLATFORM_WEBHOOK_SECRET)

        assert post(client, body, header).status_code == 200

        row = StripeEvent.objects.get()
        assert row.status == StripeEventStatus.IGNORED
        assert row.organization is None
        assert row.account == ACCOUNT_ID
        assert "connected-account" in row.error
        enqueue.assert_not_called()

    def test_an_event_naming_no_customer_is_ledgered_ignored(self, client, enqueue, subscriber):
        body, header = signed(
            platform_event("invoice.paid", {"id": "in_x", "object": "invoice"}),
            PLATFORM_WEBHOOK_SECRET,
        )

        assert post(client, body, header).status_code == 200
        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED
        enqueue.assert_not_called()

    def test_a_customer_nobody_holds_is_ledgered_ignored_and_dropped(self, client, enqueue):
        body, header = signed(
            platform_event("invoice.paid", invoice_object(customer="cus_nobody")),
            PLATFORM_WEBHOOK_SECRET,
        )

        assert post(client, body, header).status_code == 200

        row = StripeEvent.objects.get()
        assert row.status == StripeEventStatus.IGNORED
        assert row.organization is None
        assert "cus_nobody" in row.error
        enqueue.assert_not_called()

    def test_an_unhandled_type_is_ledgered_ignored(self, client, enqueue, subscriber):
        body, header = signed(
            platform_event("customer.updated", {"id": CUSTOMER_ID, "customer": CUSTOMER_ID}),
            PLATFORM_WEBHOOK_SECRET,
        )

        assert post(client, body, header).status_code == 200
        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED
        enqueue.assert_not_called()

    def test_a_replay_is_one_row(self, client, enqueue, subscriber):
        body, header = signed(
            platform_event("invoice.paid", invoice_object(), event_id="evt_p_twice"),
            PLATFORM_WEBHOOK_SECRET,
        )
        assert post(client, body, header).status_code == 200
        StripeEvent.objects.filter(event_id="evt_p_twice").update(
            status=StripeEventStatus.PROCESSED
        )

        assert post(client, body, header).status_code == 200

        assert StripeEvent.objects.filter(event_id="evt_p_twice").count() == 1
        assert enqueue.call_count == 1

    def test_the_organization_comes_from_the_customer_not_the_metadata(
        self, client, enqueue, subscriber
    ):
        rival = OrganizationFactory(stripe_customer_id="cus_rival")
        obj = subscription_checkout_session(organization_id=str(rival.pk))
        body, header = signed(
            platform_event("checkout.session.completed", obj), PLATFORM_WEBHOOK_SECRET
        )

        post(client, body, header)

        row = StripeEvent.objects.get()
        assert row.organization == subscriber
        enqueue.assert_called_once_with(str(subscriber.pk), str(row.pk))

    def test_the_right_endpoint_takes_over_an_event_the_wrong_one_ledgered(
        self, client, enqueue, subscriber
    ):
        """
        A platform endpoint misconfigured to also hear connected accounts
        would ledger a Connect event first, unowned. The Connect delivery of
        the same event must not then be dropped as a replay: it owns it.
        """
        payload = event("account.updated", {"id": ACCOUNT_ID}, event_id="evt_misrouted")
        OrganizationFactory(stripe_account_id=ACCOUNT_ID, stripe_charges_enabled=True)
        body, header = signed(payload, PLATFORM_WEBHOOK_SECRET)
        assert post(client, body, header).status_code == 200
        row = StripeEvent.objects.get(event_id="evt_misrouted")
        assert row.status == StripeEventStatus.IGNORED
        enqueue.assert_not_called()

        body, header = signed(payload, WEBHOOK_SECRET)
        assert post(client, body, header, url=CONNECT_URL).status_code == 200

        row.refresh_from_db()
        assert StripeEvent.objects.filter(event_id="evt_misrouted").count() == 1
        assert row.status == StripeEventStatus.RECEIVED
        assert row.organization.stripe_account_id == ACCOUNT_ID
        assert row.error == ""
        enqueue.assert_called_once_with(str(row.organization_id), str(row.pk))

    def test_and_the_other_way_round(self, client, enqueue, subscriber):
        payload = platform_event("invoice.paid", invoice_object(), event_id="evt_misrouted_2")
        body, header = signed(payload, WEBHOOK_SECRET)
        assert post(client, body, header, url=CONNECT_URL).status_code == 200
        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED

        body, header = signed(payload, PLATFORM_WEBHOOK_SECRET)
        assert post(client, body, header).status_code == 200

        row = StripeEvent.objects.get()
        assert row.status == StripeEventStatus.RECEIVED
        assert row.organization == subscriber
        enqueue.assert_called_once_with(str(subscriber.pk), str(row.pk))

    def test_an_unowned_event_resent_to_the_same_endpoint_stays_ignored(self, client, enqueue):
        body, header = signed(
            platform_event("invoice.paid", invoice_object(customer="cus_nobody")),
            PLATFORM_WEBHOOK_SECRET,
        )

        assert post(client, body, header).status_code == 200
        assert post(client, body, header).status_code == 200

        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED
        enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


class TestDispatchByRow:
    def test_a_platform_checkout_runs_the_platform_handler_not_the_connect_one(
        self, api, subscriber
    ):
        """Same type in both tables; the row's empty account picks the table."""
        row = ledgered(subscriber, "checkout.session.completed", subscription_checkout_session())

        with mock.patch.object(subscription, "refresh_subscription") as refresh:
            assert webhook.process(str(subscriber.pk), str(row.pk)) == StripeEventStatus.PROCESSED

        refresh.assert_called_once_with(subscriber)
        assert not Payment.objects.exists()

    def test_a_connect_type_on_a_platform_row_is_ignored(self, api, subscriber):
        row = ledgered(subscriber, "charge.refunded", {"id": "ch_1", "customer": CUSTOMER_ID})

        assert webhook.process(str(subscriber.pk), str(row.pk)) == StripeEventStatus.IGNORED
        row.refresh_from_db()
        assert row.error == webhook.NO_HANDLER


class TestPlatformHandlers:
    @pytest.mark.parametrize(
        "type_",
        [
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ],
    )
    def test_a_subscription_event_re_reads_the_subscription(self, api, subscriber, type_):
        obj = subscription_object(period_start=0, period_end=0, status="canceled")
        row = ledgered(subscriber, type_, obj)

        webhook.process(str(subscriber.pk), str(row.pk))

        # Re-read, never copied: the event said canceled, Stripe says active.
        api.v1.subscriptions.retrieve.assert_called_once_with(
            SUBSCRIPTION_ID, {"expand": ["latest_invoice"]}
        )
        subscriber.refresh_from_db()
        assert subscriber.subscription_status == "active"

    @pytest.mark.parametrize("type_", ["invoice.paid", "invoice.payment_failed"])
    def test_an_invoice_event_re_reads_the_subscription(self, api, subscriber, type_):
        row = ledgered(subscriber, type_, invoice_object())

        webhook.process(str(subscriber.pk), str(row.pk))

        api.v1.subscriptions.retrieve.assert_called_once()

    def test_checkout_adopts_the_subscription_an_organization_does_not_have_yet(self, api):
        """The checkout event races the subscription events; whichever lands first names it."""
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)
        row = ledgered(organization, "checkout.session.completed", subscription_checkout_session())

        webhook.process(str(organization.pk), str(row.pk))

        organization.refresh_from_db()
        assert organization.stripe_subscription_id == SUBSCRIPTION_ID
        assert organization.subscription_status == "active"

    def test_a_subscription_event_adopts_it_too(self, api):
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)
        obj = subscription_object(period_start=0, period_end=0)
        row = ledgered(organization, "customer.subscription.created", obj)

        webhook.process(str(organization.pk), str(row.pk))

        organization.refresh_from_db()
        assert organization.stripe_subscription_id == SUBSCRIPTION_ID

    def test_an_invoice_event_adopts_it_from_the_invoices_parent(self, api):
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)
        row = ledgered(organization, "invoice.paid", invoice_object())

        webhook.process(str(organization.pk), str(row.pk))

        organization.refresh_from_db()
        assert organization.stripe_subscription_id == SUBSCRIPTION_ID

    def test_a_new_subscription_supersedes_a_dead_one(self, api, subscriber):
        subscriber.subscription_status = "canceled"
        subscriber.save()
        obj = subscription_object(period_start=0, period_end=0, subscription_id="sub_new")
        row = ledgered(subscriber, "customer.subscription.created", obj)

        webhook.process(str(subscriber.pk), str(row.pk))

        subscriber.refresh_from_db()
        assert subscriber.stripe_subscription_id == "sub_new"
        api.v1.subscriptions.retrieve.assert_called_once_with(
            "sub_new", {"expand": ["latest_invoice"]}
        )

    def test_a_stale_event_about_another_subscription_cannot_steal_a_live_one(
        self, api, subscriber
    ):
        obj = subscription_object(period_start=0, period_end=0, subscription_id="sub_old")
        row = ledgered(subscriber, "customer.subscription.updated", obj)

        webhook.process(str(subscriber.pk), str(row.pk))

        subscriber.refresh_from_db()
        assert subscriber.stripe_subscription_id == SUBSCRIPTION_ID

    def test_a_payment_mode_session_names_no_subscription(self, api):
        organization = OrganizationFactory(stripe_customer_id=CUSTOMER_ID)
        obj = {**subscription_checkout_session(), "mode": "payment"}
        row = ledgered(organization, "checkout.session.completed", obj)

        webhook.process(str(organization.pk), str(row.pk))

        organization.refresh_from_db()
        assert organization.stripe_subscription_id == ""
        api.v1.subscriptions.retrieve.assert_not_called()

    def test_a_payment_coming_good_reactivates_within_the_handler(self, api, subscriber):
        subscriber.is_active = False
        subscriber.deactivated_for_billing_at = timezone.now() - DAY
        subscriber.save()
        row = ledgered(subscriber, "invoice.paid", invoice_object())

        webhook.process(str(subscriber.pk), str(row.pk))

        subscriber.refresh_from_db()
        assert subscriber.is_active is True
        assert subscriber.deactivated_for_billing_at is None

    def test_an_organization_switched_off_by_hand_stays_off(self, api, subscriber):
        subscriber.is_active = False
        subscriber.save()
        row = ledgered(subscriber, "invoice.paid", invoice_object())

        webhook.process(str(subscriber.pk), str(row.pk))

        subscriber.refresh_from_db()
        assert subscriber.is_active is False

    def test_another_tenants_event_never_touches_this_one(self, api, subscriber):
        """The row is looked up within the organization the task was handed."""
        rival = OrganizationFactory(stripe_customer_id="cus_rival", stripe_subscription_id="sub_r")
        row = ledgered(rival, "invoice.paid", invoice_object(customer="cus_rival"))

        assert webhook.process(str(subscriber.pk), str(row.pk)) == StripeEventStatus.IGNORED

        api.v1.subscriptions.retrieve.assert_not_called()
        row.refresh_from_db()
        assert row.status == StripeEventStatus.RECEIVED

    def test_a_stripe_failure_marks_the_row_failed_for_retry(self, api, subscriber):
        import stripe

        api.v1.subscriptions.retrieve.side_effect = stripe.APIConnectionError("down")
        row = ledgered(subscriber, "invoice.paid", invoice_object())

        with pytest.raises(ServiceUnavailableError):
            webhook.process(str(subscriber.pk), str(row.pk))

        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED


# ---------------------------------------------------------------------------
# The daily task
# ---------------------------------------------------------------------------


class TestEnforcePlatformBilling:
    def test_re_reads_the_subscription_and_applies_coverage(self, api, subscriber):
        from billing.tasks import enforce_platform_billing

        subscriber.is_active = False
        subscriber.deactivated_for_billing_at = timezone.now() - DAY
        subscriber.save()

        result = enforce_platform_billing.apply(args=[str(subscriber.pk)])

        assert result.get() == {"refreshed": True, "changed": True}
        subscriber.refresh_from_db()
        assert subscriber.is_active is True

    def test_an_organization_with_no_subscription_is_judged_on_its_trial(self, api):
        from billing.tasks import enforce_platform_billing

        organization = OrganizationFactory(trial_ends_at=timezone.now() - 15 * DAY)

        result = enforce_platform_billing.apply(args=[str(organization.pk)])

        assert result.get() == {"refreshed": False, "changed": True}
        api.v1.subscriptions.retrieve.assert_not_called()
        organization.refresh_from_db()
        assert organization.is_active is False
        assert organization.deactivated_for_billing_at is not None

    def test_stripe_being_unreachable_still_applies_what_is_on_file(self, api, subscriber):
        import stripe

        from billing.tasks import enforce_platform_billing

        api.v1.subscriptions.retrieve.side_effect = stripe.APIConnectionError("down")
        subscriber.subscription_status = "canceled"
        subscriber.subscription_paid_through = timezone.now() - 30 * DAY
        subscriber.save()

        result = enforce_platform_billing.apply(args=[str(subscriber.pk)])

        assert result.get() == {"refreshed": False, "changed": True}
        subscriber.refresh_from_db()
        assert subscriber.is_active is False

    def test_nothing_happens_with_billing_not_configured(self, api, settings):
        from billing.tasks import enforce_platform_billing, enforce_platform_billing_all

        settings.PLATFORM_BILLING_ENABLED = False
        organization = OrganizationFactory(trial_ends_at=timezone.now() - 100 * DAY)

        assert enforce_platform_billing_all.apply().get() == 0
        assert enforce_platform_billing.apply(args=[str(organization.pk)]).get() == {
            "refreshed": False,
            "changed": False,
        }
        organization.refresh_from_db()
        assert organization.is_active is True

    def test_the_fan_out_includes_inactive_organizations(self, api):
        from billing.tasks import enforce_platform_billing_all

        active = OrganizationFactory()
        inactive = OrganizationFactory(is_active=False)

        with mock.patch("billing.tasks.enforce_platform_billing.delay") as delay:
            assert enforce_platform_billing_all.apply().get() == 2

        queued = {call.args[0] for call in delay.call_args_list}
        assert queued == {str(active.pk), str(inactive.pk)}

    def test_an_unknown_organization_is_a_no_op(self, api):
        from billing.tasks import enforce_platform_billing

        result = enforce_platform_billing.apply(args=["00000000-0000-0000-0000-000000000000"])

        assert result.get() == {"refreshed": False, "changed": False}

    def test_beat_runs_it_daily_in_the_small_hours(self, settings):
        entry = settings.CELERY_BEAT_SCHEDULE["enforce-platform-billing"]

        assert entry["task"] == "billing.enforce_platform_billing_all"
        assert str(entry["schedule"]) == "<crontab: 0 3 * * * (m/h/dM/MY/d)>"
