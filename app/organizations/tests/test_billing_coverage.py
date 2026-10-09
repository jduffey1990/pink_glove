"""
The coverage rule (Phase 4c, ADR-028): `organizations.services`.

Tested by table from the fields, with the clock held still, because every
path -- the serializer, the daily task, the webhook -- reads the same
functions and the question is always "given these dates and this word from
Stripe, what is this organization, and what is it told".
"""

import datetime as dt

import pytest
from django.utils import timezone

from base.permissions import ORGANIZATION_INACTIVE
from organizations import services
from organizations.enums import BillingState, NoticeLevel
from organizations.models import Organization
from users.enums import Role

NOW = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def billing_on(settings):
    settings.PLATFORM_BILLING_ENABLED = True
    settings.PLATFORM_TRIAL_DAYS = 30
    settings.PLATFORM_GRACE_DAYS = 14


@pytest.fixture
def org(db):
    """On the frozen clock: a trial with thirty days to run."""
    return Organization.objects.create(
        name="Coverage Test Clean", timezone="America/Denver", trial_ends_at=NOW + 30 * DAY
    )


def subscribed(org, *, status="active", paid_through, cancel_at_period_end=False, trial_over=True):
    """A subscriber, whose trial is normally long over."""
    if trial_over:
        org.trial_ends_at = NOW - 40 * DAY
    org.stripe_customer_id = "cus_x"
    org.stripe_subscription_id = "sub_x"
    org.subscription_status = status
    org.subscription_paid_through = paid_through
    org.subscription_cancel_at_period_end = cancel_at_period_end
    org.save()
    return org


@pytest.mark.django_db
class TestTheTrialStamp:
    def test_a_new_organization_starts_on_a_trial(self, db):
        org = Organization.objects.create(name="Brand New")

        assert org.trial_ends_at is not None
        assert abs(org.trial_ends_at - (timezone.now() + 30 * DAY)) < dt.timedelta(minutes=1)

    def test_the_length_is_read_from_settings_at_creation(self, settings, db):
        settings.PLATFORM_TRIAL_DAYS = 7

        org = Organization.objects.create(name="Short Trial")

        assert abs(org.trial_ends_at - (timezone.now() + 7 * DAY)) < dt.timedelta(minutes=1)

    def test_a_comped_organization_gets_none(self, db):
        org = Organization.objects.create(name="Comped", billing_exempt=True)

        assert org.trial_ends_at is None

    def test_a_trial_set_by_hand_is_kept(self, db):
        when = NOW + 90 * DAY

        org = Organization.objects.create(name="Long Pilot", trial_ends_at=when)

        assert org.trial_ends_at == when

    def test_saving_again_does_not_move_it(self, org):
        before = org.trial_ends_at
        org.name = "Renamed"
        org.save()

        assert org.trial_ends_at == before


@pytest.mark.django_db
class TestCoveredUntil:
    def test_is_the_trial_end_with_no_subscription(self, org):
        assert services.covered_until(org) == org.trial_ends_at

    def test_is_the_later_of_trial_and_paid_period(self, org):
        subscribed(org, paid_through=org.trial_ends_at + 10 * DAY, trial_over=False)

        assert services.covered_until(org) == org.trial_ends_at + 10 * DAY

    def test_a_trial_longer_than_the_paid_period_wins(self, org):
        subscribed(org, paid_through=org.trial_ends_at - 10 * DAY, trial_over=False)

        assert services.covered_until(org) == org.trial_ends_at

    def test_exempt_is_forever(self, org):
        org.billing_exempt = True

        assert services.covered_until(org) is None
        assert services.grace_ends_at(org) is None

    def test_billing_not_configured_is_forever(self, org, settings):
        settings.PLATFORM_BILLING_ENABLED = False

        assert services.covered_until(org) is None

    def test_neither_a_trial_nor_a_paid_period_has_been_uncovered_since_creation(self, db):
        org = Organization.objects.create(name="Un-comped", billing_exempt=True)
        org.billing_exempt = False

        assert services.covered_until(org) == org.created_at

    def test_the_grace_runs_from_coverage(self, org):
        assert services.grace_ends_at(org) == org.trial_ends_at + 14 * DAY


@pytest.mark.django_db
class TestBillingState:
    def test_trial(self, org):
        org.trial_ends_at = NOW + 10 * DAY

        assert services.billing_state(org, NOW) == BillingState.TRIAL

    def test_active(self, org):
        subscribed(org, paid_through=NOW + 20 * DAY)

        assert services.billing_state(org, NOW) == BillingState.ACTIVE

    def test_stripes_own_trial_counts_as_active(self, org):
        subscribed(org, status="trialing", paid_through=NOW + 20 * DAY)

        assert services.billing_state(org, NOW) == BillingState.ACTIVE

    def test_cancelled_but_paid_through_a_future_date_is_still_active(self, org):
        """What was paid for is theirs until it runs out."""
        org.trial_ends_at = NOW - 40 * DAY
        subscribed(org, status="canceled", paid_through=NOW + 5 * DAY)

        assert services.billing_state(org, NOW) == BillingState.ACTIVE

    def test_past_due_whether_covered_or_not(self, org):
        subscribed(org, status="past_due", paid_through=NOW - DAY)
        assert services.billing_state(org, NOW) == BillingState.PAST_DUE

        org.trial_ends_at = NOW + 10 * DAY
        assert services.billing_state(org, NOW) == BillingState.PAST_DUE

    def test_an_expired_trial_is_lapsed(self, org):
        org.trial_ends_at = NOW - DAY

        assert services.billing_state(org, NOW) == BillingState.LAPSED

    def test_a_subscription_that_ran_out_is_lapsed(self, org):
        org.trial_ends_at = NOW - 40 * DAY
        subscribed(org, status="canceled", paid_through=NOW - DAY)

        assert services.billing_state(org, NOW) == BillingState.LAPSED

    def test_past_the_grace_but_not_yet_deactivated_is_still_lapsed(self, org):
        """The daily task has not run yet; the banner, not the state, says it is late."""
        org.trial_ends_at = NOW - 20 * DAY

        assert services.billing_state(org, NOW) == BillingState.LAPSED

    def test_deactivated_however_it_happened(self, org):
        org.is_active = False
        subscribed(org, paid_through=NOW + 20 * DAY)

        assert services.billing_state(org, NOW) == BillingState.DEACTIVATED

    def test_exempt(self, org):
        org.billing_exempt = True
        org.trial_ends_at = NOW - 100 * DAY

        assert services.billing_state(org, NOW) == BillingState.EXEMPT

    def test_exempt_when_billing_is_not_configured(self, org, settings):
        settings.PLATFORM_BILLING_ENABLED = False
        org.trial_ends_at = NOW - 100 * DAY

        assert services.billing_state(org, NOW) == BillingState.EXEMPT

    def test_the_boundary_is_the_instant(self, org):
        org.trial_ends_at = NOW

        assert services.billing_state(org, NOW) == BillingState.LAPSED
        assert services.billing_state(org, NOW - dt.timedelta(seconds=1)) == BillingState.TRIAL


@pytest.mark.django_db
class TestAvailableActions:
    def test_subscribe_with_no_subscription(self, org):
        assert services.available_actions(org) == ["subscribe"]

    def test_manage_only_with_a_live_subscription(self, org):
        subscribed(org, paid_through=NOW + 20 * DAY)

        assert services.available_actions(org) == ["manage"]

    def test_past_due_is_managed_not_resubscribed(self, org):
        subscribed(org, status="past_due", paid_through=NOW - DAY)

        assert services.available_actions(org) == ["manage"]

    def test_a_cancelled_customer_may_do_both(self, org):
        subscribed(org, status="canceled", paid_through=NOW - DAY)

        assert services.available_actions(org) == ["subscribe", "manage"]

    def test_nothing_when_exempt_or_not_configured(self, org, settings):
        org.billing_exempt = True
        assert services.available_actions(org) == []

        org.billing_exempt = False
        settings.PLATFORM_BILLING_ENABLED = False
        assert services.available_actions(org) == []


@pytest.mark.django_db
class TestBillingNotice:
    """The server's sentence, for the reader it is addressed to (ADR-023)."""

    def test_nothing_early_in_a_trial(self, org):
        org.trial_ends_at = NOW + 8 * DAY

        assert services.billing_notice(org, Role.OWNER, NOW) is None

    def test_the_last_week_of_a_trial_warns_with_the_local_date(self, org):
        # 2026-10-16 15:00 UTC is still the 16th in Denver.
        org.trial_ends_at = NOW + 7 * DAY

        level, text = services.billing_notice(org, Role.OWNER, NOW)

        assert level == NoticeLevel.WARNING
        assert text == (
            "Your free trial ends on October 16, 2026. Subscribe to keep scheduling after that."
        )

    def test_a_date_is_the_organizations_not_utcs(self, org):
        # 03:00 UTC on the 16th is 9pm on the 15th in Denver.
        org.trial_ends_at = dt.datetime(2026, 10, 16, 3, 0, tzinfo=dt.UTC)

        _, text = services.billing_notice(org, Role.OWNER, NOW)

        assert "October 15, 2026" in text

    def test_everyone_but_the_owner_is_told_to_ask(self, org):
        org.trial_ends_at = NOW + 2 * DAY

        _, text = services.billing_notice(org, Role.DISPATCHER, NOW)

        assert text.endswith(
            "Ask the organization owner to subscribe to keep scheduling after that."
        )

    def test_nothing_while_subscribed(self, org):
        subscribed(org, paid_through=NOW + 20 * DAY)

        assert services.billing_notice(org, Role.OWNER, NOW) is None

    def test_a_cancellation_at_period_end_is_information(self, org):
        subscribed(org, paid_through=NOW + 20 * DAY, cancel_at_period_end=True)

        level, text = services.billing_notice(org, Role.CLEANER, NOW)

        assert level == NoticeLevel.INFO
        assert text == "Your subscription ends on October 29, 2026."

    def test_past_due_names_the_grace_end(self, org):
        org.trial_ends_at = NOW - 40 * DAY
        subscribed(org, status="past_due", paid_through=NOW - DAY)

        level, text = services.billing_notice(org, Role.OWNER, NOW)

        assert level == NoticeLevel.WARNING
        assert text == (
            "A payment for your subscription failed. Update your card by October 22, 2026 "
            "to keep scheduling."
        )

    def test_a_lapsed_trial(self, org):
        org.trial_ends_at = NOW - DAY

        level, text = services.billing_notice(org, Role.ADMIN, NOW)

        assert level == NoticeLevel.WARNING
        assert text == (
            "Your free trial ended on October 8, 2026. Ask the organization owner to "
            "subscribe by October 22, 2026 to keep scheduling."
        )

    def test_a_lapsed_subscription(self, org):
        org.trial_ends_at = NOW - 40 * DAY
        subscribed(org, status="canceled", paid_through=NOW - DAY)

        _, text = services.billing_notice(org, Role.OWNER, NOW)

        assert text == (
            "Your subscription ended on October 8, 2026. Subscribe by October 22, 2026 "
            "to keep scheduling."
        )

    def test_deactivated_for_billing_says_why_and_how(self, org):
        org.trial_ends_at = NOW - 20 * DAY
        org.is_active = False
        org.deactivated_for_billing_at = NOW - 6 * DAY

        level, text = services.billing_notice(org, Role.OWNER, NOW)

        assert level == NoticeLevel.ERROR
        assert text == (
            "This organization is read-only because its free trial ended on September 19, "
            "2026 and no subscription was started. Subscribe to restore access."
        )

    def test_deactivated_by_hand_is_the_permission_layers_sentence(self, org):
        """The banner the baseline gate owed, with no call to action billing cannot keep."""
        org.is_active = False

        level, text = services.billing_notice(org, Role.OWNER, NOW)

        assert level == NoticeLevel.ERROR
        assert text == ORGANIZATION_INACTIVE

    def test_nothing_when_exempt(self, org):
        org.billing_exempt = True
        org.is_active = True

        assert services.billing_notice(org, Role.OWNER, NOW) is None

    def test_the_tenants_customers_are_told_nothing(self, org):
        """How the cleaning company pays for its software is not its customers' business."""
        org.trial_ends_at = NOW - DAY
        assert services.billing_notice(org, Role.CUSTOMER, NOW) is None

        org.is_active = False
        assert services.billing_notice(org, Role.CUSTOMER, NOW) is None


@pytest.mark.django_db
class TestBillingSummary:
    """Always a sentence for staff, so the settings card composes none of its own (ADR-023)."""

    def test_is_the_notice_when_there_is_one(self, org):
        org.trial_ends_at = NOW - DAY

        assert (
            services.billing_summary(org, Role.OWNER, NOW)
            == (services.billing_notice(org, Role.OWNER, NOW)[1])
        )

    def test_a_quiet_trial_says_when_it_ends(self, org):
        assert services.billing_summary(org, Role.OWNER, NOW) == (
            "Your free trial runs until November 8, 2026. Subscribe at any time to keep going "
            "after that."
        )
        assert services.billing_summary(org, Role.ADMIN, NOW).endswith(
            "Ask the organization owner to subscribe at any time to keep going after that."
        )

    def test_a_quiet_subscription_says_what_it_is_paid_through(self, org):
        subscribed(org, paid_through=NOW + 20 * DAY)

        assert services.billing_summary(org, Role.OWNER, NOW) == (
            "Paid through October 29, 2026. The card, receipts and cancellation are in your "
            "billing portal."
        )

    def test_exempt(self, org):
        org.billing_exempt = True

        assert services.billing_summary(org, Role.OWNER, NOW) == (
            "This organization uses pink glove at no charge."
        )

    def test_a_customer_gets_nothing(self, org):
        org.trial_ends_at = NOW - DAY

        assert services.billing_summary(org, Role.CUSTOMER, NOW) == ""


@pytest.mark.django_db
class TestTheStripeCustomerConstraint:
    def test_two_live_organizations_cannot_share_a_customer(self, org):
        from django.db import IntegrityError

        org.stripe_customer_id = "cus_shared"
        org.save()

        with pytest.raises(IntegrityError):
            Organization.objects.create(name="Copycat", stripe_customer_id="cus_shared")

    def test_a_deleted_organization_frees_its_customer(self, org):
        org.stripe_customer_id = "cus_freed"
        org.save()
        org.delete()

        assert Organization.objects.create(name="Successor", stripe_customer_id="cus_freed")

    def test_empty_is_not_a_customer(self, org):
        """Every organization starts with "" and that must not collide."""
        assert Organization.objects.create(name="Also empty").stripe_customer_id == ""


@pytest.mark.django_db
class TestTheMigrationBackfill:
    """Nobody who predates 4c wakes up read-only on the deploy that introduced it."""

    def test_existing_rows_get_a_trial_and_exempt_ones_do_not(self, db, settings):
        import importlib

        from django.apps import apps

        settings.PLATFORM_TRIAL_DAYS = 30
        migration = importlib.import_module("organizations.migrations.0006_platform_subscription")
        old = Organization.objects.create(name="Pre-4c", trial_ends_at=NOW)
        Organization.objects.filter(pk=old.pk).update(trial_ends_at=None)
        comped = Organization.objects.create(name="Comped before 4c", billing_exempt=True)

        migration.start_existing_organizations_on_a_trial(apps, None)

        old.refresh_from_db()
        comped.refresh_from_db()
        assert abs(old.trial_ends_at - (timezone.now() + 30 * DAY)) < dt.timedelta(minutes=1)
        assert comped.trial_ends_at is None


@pytest.mark.django_db
class TestApplyCoverage:
    def test_within_the_grace_nothing_happens(self, org):
        org.trial_ends_at = NOW - 13 * DAY
        org.save()

        assert services.apply_coverage(org, NOW) is False
        org.refresh_from_db()
        assert org.is_active is True

    def test_past_the_grace_it_is_deactivated_and_stamped(self, org):
        org.trial_ends_at = NOW - 14 * DAY
        org.save()

        assert services.apply_coverage(org, NOW) is True
        org.refresh_from_db()
        assert org.is_active is False
        assert org.deactivated_for_billing_at == NOW

    def test_covered_again_it_is_reactivated_and_the_stamp_cleared(self, org):
        org.trial_ends_at = NOW - 30 * DAY
        org.is_active = False
        org.deactivated_for_billing_at = NOW - 16 * DAY
        subscribed(org, paid_through=NOW + 29 * DAY)

        assert services.apply_coverage(org, NOW) is True
        org.refresh_from_db()
        assert org.is_active is True
        assert org.deactivated_for_billing_at is None

    def test_an_organization_switched_off_by_hand_stays_off(self, org):
        org.is_active = False
        subscribed(org, paid_through=NOW + 29 * DAY)

        assert services.apply_coverage(org, NOW) is False
        org.refresh_from_db()
        assert org.is_active is False

    def test_still_uncovered_it_stays_off(self, org):
        org.trial_ends_at = NOW - 30 * DAY
        org.is_active = False
        org.deactivated_for_billing_at = NOW - 16 * DAY
        org.save()

        assert services.apply_coverage(org, NOW) is False
        org.refresh_from_db()
        assert org.is_active is False

    def test_exempt_never_lapses_and_comes_back_if_it_was_deactivated_for_billing(self, org):
        org.trial_ends_at = NOW - 100 * DAY
        org.save()
        assert services.apply_coverage(org, NOW) is True  # it lapsed

        org.billing_exempt = True
        assert services.apply_coverage(org, NOW) is True  # comped: back on
        org.refresh_from_db()
        assert org.is_active is True

    def test_switching_billing_off_on_the_server_reactivates_what_billing_took_down(
        self, org, settings
    ):
        org.trial_ends_at = NOW - 100 * DAY
        org.save()
        services.apply_coverage(org, NOW)
        settings.PLATFORM_BILLING_ENABLED = False

        assert services.apply_coverage(org, NOW) is True
        org.refresh_from_db()
        assert org.is_active is True

    def test_only_the_two_fields_are_written(self, org):
        org.trial_ends_at = NOW - 14 * DAY
        org.save()
        Organization.objects.filter(pk=org.pk).update(name="Renamed elsewhere")

        services.apply_coverage(org, NOW)

        org.refresh_from_db()
        assert org.name == "Renamed elsewhere"
        assert org.is_active is False
