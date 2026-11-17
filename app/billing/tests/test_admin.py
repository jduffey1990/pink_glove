"""
Replaying a FAILED Stripe event from the admin.

Until this existed the replay was `process_stripe_event.delay(...)` typed
into a shell on the server, which in production means the owner in a console
in the middle of an incident. The action hands the same two ids to the same
task; everything it can get wrong is here.

The admin itself admits only a session that has cleared the second factor
(`app/tests/test_admin_site.py`); the last class checks the action did not
open a way round that.
"""

from unittest import mock

import pytest
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.urls import reverse
from kombu.exceptions import OperationalError

from app.admin import TWO_FACTOR_VERIFIED_SESSION_KEY
from billing import subscription
from billing.enums import StripeEventStatus
from billing.models import Payment, StripeEvent
from billing.tasks import process_stripe_event
from billing.tests.factories import IssuedInvoiceFactory, StripeEventFactory
from billing.tests.stripe_fixtures import ACCOUNT_ID, CUSTOMER_ID, subscription_checkout_session
from billing.tests.test_platform_webhook import ledgered as platform_ledgered
from billing.tests.test_webhook import ledgered, session
from scheduling.tests.factories import OrganizationFactory, UserFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("stripe_on")]

CHANGELIST = reverse("admin:billing_stripeevent_changelist")


def cleared(client, user):
    """Signed in, with the mark `two_factor.views.VerifyView` leaves."""
    client.force_login(user)
    session_ = client.session
    session_[TWO_FACTOR_VERIFIED_SESSION_KEY] = True
    session_.save()
    return client


@pytest.fixture
def admin_client(client, superuser):
    return cleared(client, superuser)


def replay(client, *rows):
    return client.post(
        CHANGELIST,
        {"action": "replay_failed", "_selected_action": [str(row.pk) for row in rows], "index": 0},
    )


def said(response) -> list[tuple[str, str]]:
    return [(message.level_tag, message.message) for message in get_messages(response.wsgi_request)]


def failed(organization, **kwargs):
    return StripeEventFactory(
        organization=organization,
        account=organization.stripe_account_id,
        status=StripeEventStatus.FAILED,
        error="HandlerError: something a person has since fixed",
        **kwargs,
    )


class TestReplay:
    def test_a_failed_event_is_handed_back_to_the_task_as_two_ids(
        self, admin_client, connected, enqueue
    ):
        row = failed(connected)

        response = replay(admin_client, row)

        assert response.status_code == 302
        # Invariant 3: the tenant goes explicitly, and the payload stays in
        # the ledger rather than travelling through the broker.
        enqueue.assert_called_once_with(str(connected.pk), str(row.pk))
        ((level, text),) = said(response)
        assert level == "success"
        assert "Re-queued 1 event" in text

    def test_the_row_stays_failed_until_a_worker_has_applied_it(
        self, admin_client, connected, enqueue
    ):
        """A replay that is lost in turn must leave the queue telling the truth."""
        row = failed(connected)

        replay(admin_client, row)

        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
        assert row.error == "HandlerError: something a person has since fixed"

    def test_the_replay_really_applies_the_event(self, admin_client, connected, no_fee):
        """No mock in the way: the suite runs tasks inline."""
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))
        StripeEvent.objects.filter(pk=row.pk).update(status=StripeEventStatus.FAILED, error="was")

        replay(admin_client, row)

        row.refresh_from_db()
        assert row.status == StripeEventStatus.PROCESSED
        assert row.error == ""
        assert Payment.objects.filter(invoice=invoice).count() == 1

    def test_each_event_goes_with_its_own_tenant(self, admin_client, connected, enqueue):
        other = OrganizationFactory(stripe_account_id="acct_1OtherConnectedAcct")
        ours, theirs = failed(connected), failed(other)

        replay(admin_client, ours, theirs)

        assert sorted(call.args for call in enqueue.call_args_list) == sorted(
            [(str(connected.pk), str(ours.pk)), (str(other.pk), str(theirs.pk))]
        )

    def test_the_replay_is_recorded_against_the_person_who_ran_it(
        self, admin_client, superuser, connected, enqueue
    ):
        row = failed(connected)

        replay(admin_client, row)

        entry = LogEntry.objects.get()
        assert entry.user == superuser
        assert entry.object_id == str(row.pk)
        assert entry.action_flag == CHANGE
        assert entry.change_message == "Re-queued for processing."


class TestOnlyFailedRows:
    @pytest.mark.parametrize(
        "status",
        [StripeEventStatus.RECEIVED, StripeEventStatus.PROCESSED, StripeEventStatus.IGNORED],
    )
    def test_any_other_status_is_refused_and_says_so(
        self, admin_client, connected, enqueue, status
    ):
        row = StripeEventFactory(organization=connected, account=ACCOUNT_ID, status=status)

        response = replay(admin_client, row)

        enqueue.assert_not_called()
        ((level, text),) = said(response)
        assert level == "warning"
        assert "1 event(s) not re-queued" in text
        assert str(status) in text
        assert not LogEntry.objects.exists()

    def test_a_failed_event_nobody_owns_has_no_tenant_to_run_under(self, admin_client, enqueue):
        row = StripeEventFactory(organization=None, status=StripeEventStatus.FAILED)

        response = replay(admin_client, row)

        enqueue.assert_not_called()
        assert [level for level, _ in said(response)] == ["warning"]

    def test_a_mixed_selection_replays_the_failed_and_refuses_the_rest(
        self, admin_client, connected, enqueue
    ):
        broken = failed(connected)
        done = StripeEventFactory(
            organization=connected, account=ACCOUNT_ID, status=StripeEventStatus.PROCESSED
        )

        response = replay(admin_client, broken, done)

        enqueue.assert_called_once_with(str(connected.pk), str(broken.pk))
        assert [level for level, _ in said(response)] == ["success", "warning"]


@pytest.mark.usefixtures("platform_billing_on")
class TestPlatformEvents:
    """
    The platform's own events (Phase 4c, ADR-028) are ledgered in the same
    table. Such a row has no account, but it does have an organization: the
    one whose Stripe customer the event is about, resolved before anything
    is queued. So a FAILED one is replayed like any other, with that
    organization's id (invariant 3), and `process` picks the platform
    handlers from the row's empty account. The one type both tables share is
    used here, so a replay sent down the Connect path would show.
    """

    @pytest.fixture
    def subscriber(self):
        return OrganizationFactory(stripe_customer_id=CUSTOMER_ID)

    @pytest.fixture
    def broken(self, subscriber):
        row = platform_ledgered(
            subscriber, "checkout.session.completed", subscription_checkout_session()
        )
        StripeEvent.objects.filter(pk=row.pk).update(
            status=StripeEventStatus.FAILED, error="ServiceUnavailableError: Stripe was down"
        )
        return row

    def test_a_failed_platform_event_is_queued_with_the_tenant_it_was_resolved_to(
        self, admin_client, subscriber, broken, enqueue
    ):
        assert broken.account == ""

        response = replay(admin_client, broken)

        enqueue.assert_called_once_with(str(subscriber.pk), str(broken.pk))
        assert [level for level, _ in said(response)] == ["success"]

    def test_and_it_runs_the_platform_handler_not_the_connect_one(
        self, admin_client, subscriber, broken
    ):
        """No mock on the queue: the suite runs the task inline."""
        with mock.patch.object(subscription, "refresh_subscription") as refresh:
            replay(admin_client, broken)

        refresh.assert_called_once_with(subscriber)
        # The Connect handler for this type records a payment on an invoice.
        assert not Payment.objects.exists()
        broken.refresh_from_db()
        assert broken.status == StripeEventStatus.PROCESSED
        assert broken.error == ""

    @pytest.mark.parametrize("status", [StripeEventStatus.IGNORED, StripeEventStatus.FAILED])
    def test_a_platform_event_no_tenant_holds_is_refused_and_says_so(
        self, admin_client, enqueue, status
    ):
        """
        "No organization holds customer ..." is ledgered with none. There is
        no organization id to hand the task, so nothing is queued, whatever
        the row's status says.
        """
        row = StripeEventFactory(account="", organization=None, type="invoice.paid", status=status)

        response = replay(admin_client, row)

        enqueue.assert_not_called()
        ((level, text),) = said(response)
        assert level == "warning"
        assert "that an organization owns" in text


class TestRunningItTwice:
    def test_two_runs_before_a_worker_gets_to_it_still_write_one_payment(
        self, admin_client, connected, enqueue, no_fee
    ):
        """A double click, or two people in the same incident."""
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))
        StripeEvent.objects.filter(pk=row.pk).update(status=StripeEventStatus.FAILED)

        replay(admin_client, row)
        replay(admin_client, row)

        assert enqueue.call_count == 2
        for call in enqueue.call_args_list:
            assert process_stripe_event.apply(args=call.args).successful()

        row.refresh_from_db()
        assert row.status == StripeEventStatus.PROCESSED
        assert Payment.objects.filter(invoice=invoice).count() == 1

    def test_a_second_run_after_it_went_through_is_refused(self, admin_client, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))
        StripeEvent.objects.filter(pk=row.pk).update(status=StripeEventStatus.FAILED)
        replay(admin_client, row)
        # The list page shows the first run's message, which clears it.
        admin_client.get(CHANGELIST)

        response = replay(admin_client, row)

        assert [level for level, _ in said(response)] == ["warning"]
        assert Payment.objects.filter(invoice=invoice).count() == 1


class TestWhenTheBrokerIsDown:
    def test_it_says_so_on_the_page_instead_of_a_500(self, admin_client, connected, enqueue):
        enqueue.side_effect = OperationalError("Error 111 connecting to redis:6379.")
        row = failed(connected)

        response = replay(admin_client, row)

        assert response.status_code == 302
        ((level, text),) = said(response)
        assert level == "error"
        assert "could not be queued" in text
        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
        # Nothing happened, so nothing is recorded as having happened.
        assert not LogEntry.objects.exists()


class TestWhoMayReplay:
    def test_a_session_that_skipped_the_second_factor_cannot(
        self, client, superuser, connected, enqueue
    ):
        """The admin's own door (`app/admin.py`), still shut for the action."""
        client.force_login(superuser)

        response = replay(client, failed(connected))

        assert response.status_code == 302
        assert reverse("admin:login") in response["Location"]
        enqueue.assert_not_called()

    def test_anonymous_cannot(self, client, connected, enqueue):
        response = replay(client, failed(connected))

        assert response.status_code == 302
        enqueue.assert_not_called()

    def test_staff_who_may_only_look_are_not_offered_it(self, client, connected, enqueue):
        """Viewing the ledger is not permission to move money in it."""
        viewer = UserFactory(is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_stripeevent"))
        cleared(client, viewer)

        assert client.get(CHANGELIST).status_code == 200
        assert b"replay_failed" not in client.get(CHANGELIST).content

        replay(client, failed(connected))

        enqueue.assert_not_called()

    def test_a_superuser_is_offered_exactly_one_action(self, admin_client, connected):
        """And not `delete_selected`: the ledger is still read-only."""
        failed(connected)

        content = admin_client.get(CHANGELIST).content

        assert b'value="replay_failed"' in content
        assert b"delete_selected" not in content

    def test_the_ledger_is_still_not_editable_from_the_admin(self, admin_client, connected):
        row = failed(connected)
        change = reverse("admin:billing_stripeevent_change", args=[row.pk])

        assert admin_client.post(change, {"status": StripeEventStatus.PROCESSED}).status_code == 403
        assert (
            admin_client.post(
                reverse("admin:billing_stripeevent_delete", args=[row.pk]), {"post": "yes"}
            ).status_code
            == 403
        )
        assert admin_client.get(reverse("admin:billing_stripeevent_add")).status_code == 403
        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
