"""
The Connect webhook, end to end with signed bytes.

The request side is tested through the view with real signatures, the
processing side through `webhook.process` with the task's own arguments.
`stripe` is never reached: the signature check runs against a secret the
tests chose, and `billing.connect` is patched where it would call out.
"""

import datetime as dt
from unittest import mock

import pytest
from django.utils import timezone

from billing import connect, webhook
from billing.enums import PaymentMethod, StripeEventStatus
from billing.models import Payment, StripeEvent
from billing.services import balance_cents, overpaid_cents, payment_state
from billing.tests.factories import IssuedInvoiceFactory, PaymentFactory, StripeEventFactory
from billing.tests.stripe_fixtures import ACCOUNT_ID, event, signed
from scheduling.tests.factories import OrganizationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("stripe_on")]

URL = "/api/billing/stripe/webhook/"


@pytest.fixture
def enqueue():
    with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
        yield delay


@pytest.fixture
def no_fee():
    with mock.patch.object(connect, "fee_cents_for", return_value=None) as fee:
        yield fee


def post(client, body: bytes, header: str):
    return client.post(
        URL, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=header
    )


def session(invoice, *, intent="pi_1", amount=None, paid=True, invoice_id=None):
    return {
        "id": "cs_test_1",
        "object": "checkout.session",
        "payment_status": "paid" if paid else "unpaid",
        "payment_intent": intent,
        "amount_total": invoice.total_cents if amount is None else amount,
        "client_reference_id": str(invoice.pk),
        "metadata": {
            "organization_id": str(invoice.organization_id),
            "invoice_id": str(invoice_id or invoice.pk),
        },
    }


def ledgered(invoice, obj, type_="checkout.session.completed", **kwargs):
    """A RECEIVED row for `invoice`'s organization, as `receive` would write."""
    payload = event(type_, obj, account=invoice.organization.stripe_account_id, **kwargs)
    return StripeEventFactory(
        event_id=payload["id"],
        account=payload["account"],
        type=type_,
        organization=invoice.organization,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestTheEndpoint:
    def test_a_bad_signature_is_a_400_and_no_row(self, client, enqueue):
        body, _ = signed(event("account.updated", {"id": ACCOUNT_ID}))

        response = post(client, body, "t=1,v1=nonsense")

        assert response.status_code == 400
        assert not StripeEvent.objects.exists()
        enqueue.assert_not_called()

    def test_the_wrong_secret_is_a_400(self, client, enqueue):
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}), secret="whsec_other")

        assert post(client, body, header).status_code == 400
        assert not StripeEvent.objects.exists()

    def test_a_session_is_not_needed_and_csrf_does_not_apply(self, client, enqueue, connected):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}))

        assert post(strict, body, header).status_code == 200

    def test_is_a_503_when_stripe_is_not_enabled(self, client, settings, enqueue):
        settings.STRIPE_ENABLED = False
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}))

        assert post(client, body, header).status_code == 503
        assert not StripeEvent.objects.exists()

    def test_is_a_503_without_a_webhook_secret(self, client, settings, enqueue):
        settings.STRIPE_CONNECT_WEBHOOK_SECRET = ""
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}))

        assert post(client, body, header).status_code == 503

    def test_only_post(self, client):
        assert client.get(URL).status_code == 405

    def test_a_signed_body_that_is_not_an_event_is_a_400_and_no_row(self, client, enqueue):
        """Not a 500: Stripe would retry that for days, and cannot fix the body."""
        for broken in ({"id": "evt_x"}, {"type": "account.updated"}, {"id": 5, "type": "x"}):
            body, header = signed(broken)

            assert post(client, body, header).status_code == 400

        assert not StripeEvent.objects.exists()
        enqueue.assert_not_called()

    def test_a_platform_event_is_ledgered_ignored_with_no_organization(
        self, client, enqueue, connected
    ):
        """No `account`: Phase 4c's endpoint, not this one. Kept, not applied."""
        body, header = signed(event("invoice.paid", {"id": "in_1"}, account=None))

        assert post(client, body, header).status_code == 200
        row = StripeEvent.objects.get()
        assert row.account == ""
        assert row.status == StripeEventStatus.IGNORED
        assert row.organization is None
        enqueue.assert_not_called()

    def test_a_handled_event_for_a_known_account_is_ledgered_and_enqueued(
        self, client, enqueue, connected
    ):
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}, event_id="evt_ok"))

        response = post(client, body, header)

        assert response.status_code == 200
        row = StripeEvent.objects.get(event_id="evt_ok")
        assert row.status == StripeEventStatus.RECEIVED
        assert row.organization == connected
        assert row.account == ACCOUNT_ID
        assert row.payload["type"] == "account.updated"
        enqueue.assert_called_once_with(str(connected.pk), str(row.pk))

    def test_an_unknown_account_is_ledgered_ignored_and_dropped(self, client, enqueue):
        body, header = signed(
            event("account.updated", {"id": "acct_nobody"}, account="acct_nobody")
        )

        response = post(client, body, header)

        assert response.status_code == 200
        row = StripeEvent.objects.get()
        assert row.status == StripeEventStatus.IGNORED
        assert row.organization is None
        assert "acct_nobody" in row.error
        enqueue.assert_not_called()

    def test_an_unhandled_type_is_ledgered_ignored(self, client, enqueue, connected):
        body, header = signed(event("payout.paid", {"id": "po_1"}))

        assert post(client, body, header).status_code == 200
        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED
        enqueue.assert_not_called()

    def test_a_retry_of_a_processed_event_is_a_200_and_a_no_op(self, client, enqueue, connected):
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}, event_id="evt_twice"))
        assert post(client, body, header).status_code == 200
        StripeEvent.objects.filter(event_id="evt_twice").update(status=StripeEventStatus.PROCESSED)

        assert post(client, body, header).status_code == 200

        assert StripeEvent.objects.filter(event_id="evt_twice").count() == 1
        assert enqueue.call_count == 1

    @pytest.mark.parametrize("stuck", [StripeEventStatus.RECEIVED, StripeEventStatus.FAILED])
    def test_a_resend_of_a_stuck_event_re_queues_it(self, client, enqueue, connected, stuck):
        """
        The first staging payment was lost to a worker killed mid-task, and
        Stripe's Resend was refused as a duplicate. Resend is the replay
        button; a row that never finished is run again.
        """
        body, header = signed(event("account.updated", {"id": ACCOUNT_ID}, event_id="evt_stuck"))
        assert post(client, body, header).status_code == 200
        row = StripeEvent.objects.get(event_id="evt_stuck")
        row.status = stuck
        row.save()

        assert post(client, body, header).status_code == 200

        assert StripeEvent.objects.filter(event_id="evt_stuck").count() == 1
        assert enqueue.call_count == 2
        enqueue.assert_called_with(str(connected.pk), str(row.pk))

    def test_a_resend_of_an_ignored_event_stays_ignored(self, client, enqueue):
        body, header = signed(
            event("account.updated", {"id": "acct_nobody"}, account="acct_nobody")
        )

        assert post(client, body, header).status_code == 200
        assert post(client, body, header).status_code == 200

        enqueue.assert_not_called()
        assert StripeEvent.objects.get().status == StripeEventStatus.IGNORED

    def test_an_event_names_the_organization_by_account_not_by_metadata(
        self, client, enqueue, connected
    ):
        """The task is handed the account's organization, whatever the payload claims."""
        rival = OrganizationFactory(stripe_account_id="acct_rival", stripe_charges_enabled=True)
        body, header = signed(
            event(
                "account.updated",
                {"id": ACCOUNT_ID, "metadata": {"organization_id": str(rival.pk)}},
            )
        )

        post(client, body, header)

        row = StripeEvent.objects.get()
        assert row.organization == connected
        enqueue.assert_called_once_with(str(connected.pk), str(row.pk))


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


class TestProcess:
    def test_an_event_from_another_organization_is_not_found(self, connected):
        row = StripeEventFactory(organization=connected)
        other = OrganizationFactory()

        assert webhook.process(str(other.pk), str(row.pk)) == StripeEventStatus.IGNORED
        row.refresh_from_db()
        assert row.status == StripeEventStatus.RECEIVED

    def test_a_processed_event_is_left_alone(self, connected):
        row = StripeEventFactory(organization=connected, status=StripeEventStatus.PROCESSED)

        with mock.patch.object(webhook, "HANDLERS", {"account.updated": mock.Mock()}) as handlers:
            webhook.process(str(connected.pk), str(row.pk))
            handlers["account.updated"].assert_not_called()

    def test_a_handler_failure_marks_failed_rolls_back_and_re_raises(self, connected):
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice, invoice_id="00000000-0000-0000-0000-000000000000"))

        with pytest.raises(webhook.HandlerError):
            webhook.process(str(connected.pk), str(row.pk))

        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
        assert "HandlerError" in row.error
        assert row.processed_at is not None
        assert not Payment.objects.exists()

    def test_a_failed_event_can_be_run_again(self, connected, no_fee):
        """Replay: fix the cause, run it again, and it goes through."""
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))
        row.status = StripeEventStatus.FAILED
        row.save()

        assert webhook.process(str(connected.pk), str(row.pk)) == StripeEventStatus.PROCESSED
        assert Payment.objects.count() == 1

    def test_the_task_retries_three_times_with_backoff(self):
        from billing.tasks import process_stripe_event

        assert process_stripe_event.autoretry_for == (Exception,)
        assert process_stripe_event.retry_kwargs == {"max_retries": 3}
        assert process_stripe_event.retry_backoff is True
        # Survives a worker killed under it (the first staging payment did not).
        assert process_stripe_event.acks_late is True
        assert process_stripe_event.reject_on_worker_lost is True

    def test_the_task_itself_runs_the_event(self, connected, no_fee):
        """Through Celery's own call path, not `process` directly."""
        from billing.tasks import process_stripe_event

        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))

        result = process_stripe_event.apply(args=[str(connected.pk), str(row.pk)])

        assert result.successful()
        assert result.get() == StripeEventStatus.PROCESSED
        assert Payment.objects.count() == 1

    def test_a_failure_after_the_payment_is_written_rolls_it_back(self, connected):
        """
        The handler and the PROCESSED mark share a transaction. The fee read
        comes after the payment row, so a failure there is the case that
        proves the rollback: no payment survives, the row is FAILED.
        """
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))

        with (
            mock.patch.object(connect, "fee_cents_for", side_effect=RuntimeError("stripe down")),
            pytest.raises(RuntimeError),
        ):
            webhook.process(str(connected.pk), str(row.pk))

        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
        assert "RuntimeError" in row.error
        assert not Payment.objects.exists()


class TestCheckoutSessionCompleted:
    def test_writes_one_card_payment_with_the_intent_as_reference(self, connected):
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=20000)
        row = ledgered(invoice, session(invoice, intent="pi_abc"))

        with mock.patch.object(connect, "fee_cents_for", return_value=610) as fee:
            assert webhook.process(str(connected.pk), str(row.pk)) == StripeEventStatus.PROCESSED

        payment = Payment.objects.get()
        assert payment.invoice == invoice
        assert payment.organization == connected
        assert payment.method == PaymentMethod.CARD
        assert payment.amount_cents == 20000
        assert payment.provider == "stripe"
        assert payment.provider_reference == "pi_abc"
        assert payment.reference == "cs_test_1"
        assert payment.recorded_by is None
        assert payment.fee_cents == 610
        assert payment.received_on == connected.today()
        fee.assert_called_once_with("pi_abc", organization=connected)
        assert payment_state(invoice) == "paid"

    def test_a_replay_writes_nothing(self, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected)
        first = ledgered(invoice, session(invoice, intent="pi_same"), event_id="evt_a")
        second = ledgered(invoice, session(invoice, intent="pi_same"), event_id="evt_b")

        webhook.process(str(connected.pk), str(first.pk))
        webhook.process(str(connected.pk), str(second.pk))

        assert Payment.objects.count() == 1

    def test_the_fee_is_left_null_until_stripe_has_it(self, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))

        webhook.process(str(connected.pk), str(row.pk))

        assert Payment.objects.get().fee_cents is None

    def test_an_unpaid_session_writes_nothing(self, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice, paid=False))

        assert webhook.process(str(connected.pk), str(row.pk)) == StripeEventStatus.PROCESSED
        assert not Payment.objects.exists()

    def test_metadata_naming_another_tenants_invoice_fails_and_pays_nothing(
        self, connected, no_fee
    ):
        """The organization comes from the account; the invoice is looked up within it."""
        rival = OrganizationFactory(stripe_account_id="acct_rival", stripe_charges_enabled=True)
        rivals_invoice = IssuedInvoiceFactory(organization=rival, total_cents=50000)
        row = ledgered(
            IssuedInvoiceFactory(organization=connected),
            session(rivals_invoice, invoice_id=rivals_invoice.pk),
        )

        with pytest.raises(webhook.HandlerError):
            webhook.process(str(connected.pk), str(row.pk))

        assert not Payment.objects.exists()
        assert balance_cents(rivals_invoice) == 50000

    def test_an_overpayment_is_recorded_as_it_happened(self, connected, no_fee):
        """A check recorded after the session was created makes the card an overpayment."""
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=20000)
        PaymentFactory(organization=connected, invoice=invoice, amount_cents=5000)
        row = ledgered(invoice, session(invoice, amount=20000))

        webhook.process(str(connected.pk), str(row.pk))

        assert Payment.objects.count() == 2
        assert balance_cents(invoice) == 0
        assert overpaid_cents(invoice) == 5000


class TestChargeRefunded:
    @pytest.fixture
    def paid(self, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=20000)
        row = ledgered(invoice, session(invoice, intent="pi_r"), event_id="evt_paid")
        webhook.process(str(connected.pk), str(row.pk))
        return invoice

    def charge(self, *, amount=20000, refunded):
        return {
            "id": "ch_1",
            "object": "charge",
            "payment_intent": "pi_r",
            "amount": amount,
            "amount_refunded": refunded,
            "refunded": refunded == amount,
        }

    def test_a_full_refund_voids_the_payment(self, connected, paid):
        row = ledgered(paid, self.charge(refunded=20000), "charge.refunded", event_id="evt_rf")

        webhook.process(str(connected.pk), str(row.pk))

        payment = Payment.objects.get()
        assert payment.is_void
        assert "Refunded in Stripe: $200.00 of $200.00" == payment.void_reason
        assert balance_cents(paid) == 20000

    def test_a_partial_refund_voids_and_writes_the_net(self, connected, paid):
        row = ledgered(paid, self.charge(refunded=5000), "charge.refunded", event_id="evt_rf")

        webhook.process(str(connected.pk), str(row.pk))

        original = Payment.objects.get(provider_reference="pi_r")
        net = Payment.objects.get(provider_reference="pi_r:refunded-5000")
        assert original.is_void
        assert not net.is_void
        assert net.amount_cents == 15000
        assert net.invoice == paid
        assert net.reference == "ch_1"
        assert balance_cents(paid) == 5000

    def test_a_replayed_partial_refund_changes_nothing(self, connected, paid):
        first = ledgered(paid, self.charge(refunded=5000), "charge.refunded", event_id="evt_1")
        second = ledgered(paid, self.charge(refunded=5000), "charge.refunded", event_id="evt_2")

        webhook.process(str(connected.pk), str(first.pk))
        webhook.process(str(connected.pk), str(second.pk))

        assert Payment.objects.count() == 2
        assert not Payment.objects.get(provider_reference="pi_r:refunded-5000").is_void
        assert balance_cents(paid) == 5000

    def test_a_second_partial_refund_replaces_the_net(self, connected, paid):
        first = ledgered(paid, self.charge(refunded=5000), "charge.refunded", event_id="evt_1")
        second = ledgered(paid, self.charge(refunded=12000), "charge.refunded", event_id="evt_2")

        webhook.process(str(connected.pk), str(first.pk))
        webhook.process(str(connected.pk), str(second.pk))

        assert Payment.objects.get(provider_reference="pi_r:refunded-5000").is_void
        assert Payment.objects.get(provider_reference="pi_r:refunded-12000").amount_cents == 8000
        assert balance_cents(paid) == 12000

    def test_a_refund_of_a_charge_we_never_saw_is_a_no_op_that_says_so(self, connected, caplog):
        """
        Nothing to void is not an error, but it is said aloud: the other way
        a refund finds no payment is a checkout event lost before it was
        applied, and an operator watching a refund change nothing needs the
        log to say why (staging, 2026-09-23).
        """
        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, self.charge(refunded=20000), "charge.refunded")

        with caplog.at_level("WARNING", logger="billing.webhook"):
            status = webhook.process(str(connected.pk), str(row.pk))

        assert status == StripeEventStatus.PROCESSED
        assert not Payment.objects.exists()
        assert any(
            "no live payment for PaymentIntent pi_r" in r.getMessage()
            and "nothing to void" in r.getMessage()
            for r in caplog.records
        )

    def test_refunds_arriving_out_of_order_never_put_money_back(self, connected, paid):
        """
        Stripe does not promise order. Two partial refunds, the later one
        delivered first: the ledger must end at the larger cumulative refund
        and stay there when the earlier, smaller one arrives.
        """
        later = ledgered(paid, self.charge(refunded=12000), "charge.refunded", event_id="e2")
        earlier = ledgered(paid, self.charge(refunded=5000), "charge.refunded", event_id="e1")

        webhook.process(str(connected.pk), str(later.pk))
        assert balance_cents(paid) == 12000

        webhook.process(str(connected.pk), str(earlier.pk))

        assert balance_cents(paid) == 12000
        assert not Payment.objects.filter(provider_reference="pi_r:refunded-5000").exists()
        assert not Payment.objects.get(provider_reference="pi_r:refunded-12000").is_void

    def test_a_refund_on_another_tenants_account_touches_nothing_here(
        self, connected, paid, no_fee
    ):
        """Org B's account reports a refund of a PaymentIntent that paid org A's invoice."""
        rival = OrganizationFactory(stripe_account_id="acct_rival", stripe_charges_enabled=True)
        row = StripeEventFactory(
            organization=rival,
            account="acct_rival",
            type="charge.refunded",
            payload=event("charge.refunded", self.charge(refunded=20000), account="acct_rival"),
        )

        webhook.process(str(rival.pk), str(row.pk))

        assert not Payment.objects.get(provider_reference="pi_r").is_void
        assert balance_cents(paid) == 0


class TestDisputes:
    @pytest.fixture
    def paid(self, connected, no_fee):
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=20000)
        row = ledgered(invoice, session(invoice, intent="pi_d"), event_id="evt_paid")
        webhook.process(str(connected.pk), str(row.pk))
        return invoice

    def dispute(self, status):
        return {"id": "dp_1", "object": "dispute", "payment_intent": "pi_d", "status": status}

    def test_created_annotates_and_leaves_the_balance(self, connected, paid):
        row = ledgered(paid, self.dispute("needs_response"), "charge.dispute.created")

        webhook.process(str(connected.pk), str(row.pk))

        payment = Payment.objects.get()
        assert payment.disputed_at is not None
        assert payment.dispute_status == "needs_response"
        assert not payment.is_void
        assert balance_cents(paid) == 0

    def test_won_keeps_the_money(self, connected, paid):
        opened = ledgered(
            paid, self.dispute("needs_response"), "charge.dispute.created", event_id="e1"
        )
        closed = ledgered(paid, self.dispute("won"), "charge.dispute.closed", event_id="e2")

        webhook.process(str(connected.pk), str(opened.pk))
        webhook.process(str(connected.pk), str(closed.pk))

        payment = Payment.objects.get()
        assert payment.dispute_status == "won"
        assert not payment.is_void

    def test_lost_voids_the_payment(self, connected, paid):
        row = ledgered(paid, self.dispute("lost"), "charge.dispute.closed")

        webhook.process(str(connected.pk), str(row.pk))

        payment = Payment.objects.get()
        assert payment.is_void
        assert payment.void_reason == "Dispute lost: dp_1"
        assert balance_cents(paid) == 20000

    def test_a_dispute_on_another_tenants_account_touches_nothing_here(self, connected, paid):
        rival = OrganizationFactory(stripe_account_id="acct_rival", stripe_charges_enabled=True)
        row = StripeEventFactory(
            organization=rival,
            account="acct_rival",
            type="charge.dispute.closed",
            payload=event("charge.dispute.closed", self.dispute("lost"), account="acct_rival"),
        )

        webhook.process(str(rival.pk), str(row.pk))

        payment = Payment.objects.get()
        assert not payment.is_void
        assert payment.dispute_status == ""
        assert balance_cents(paid) == 0


class TestAccountUpdated:
    def test_re_reads_the_account_rather_than_trusting_the_event(self, connected):
        row = StripeEventFactory(
            organization=connected,
            type="account.updated",
            payload=event("account.updated", {"id": ACCOUNT_ID, "charges_enabled": False}),
        )

        with mock.patch.object(connect, "refresh_account") as refresh:
            webhook.process(str(connected.pk), str(row.pk))

        refresh.assert_called_once_with(connected)
        connected.refresh_from_db()
        assert connected.stripe_charges_enabled is True  # the event's False was not copied


@pytest.mark.django_db
class TestSweep:
    """
    The ledger is the queue of record. A row delivered and ledgered but
    never applied is re-queued from beat; one that re-queuing does not help
    is handed to a person.
    """

    def _received(self, connected, *, age):
        row = StripeEventFactory(
            organization=connected,
            account=connected.stripe_account_id,
            type="account.updated",
            status=StripeEventStatus.RECEIVED,
        )
        StripeEvent.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
        row.refresh_from_db()
        return row

    def test_a_stale_received_row_is_requeued_with_its_own_tenant(self, connected):
        row = self._received(connected, age=dt.timedelta(minutes=11))

        with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
            assert webhook.sweep() == {"requeued": 1, "abandoned": 0}

        delay.assert_called_once_with(str(connected.pk), str(row.pk))
        row.refresh_from_db()
        assert row.status == StripeEventStatus.RECEIVED

    def test_a_fresh_received_row_is_still_in_flight(self, connected):
        self._received(connected, age=dt.timedelta(minutes=2))

        with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
            assert webhook.sweep() == {"requeued": 0, "abandoned": 0}

        delay.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [StripeEventStatus.PROCESSED, StripeEventStatus.FAILED, StripeEventStatus.IGNORED],
    )
    def test_settled_rows_are_left_alone(self, connected, status):
        row = self._received(connected, age=dt.timedelta(days=2))
        StripeEvent.objects.filter(pk=row.pk).update(status=status)

        with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
            assert webhook.sweep() == {"requeued": 0, "abandoned": 0}

        delay.assert_not_called()
        row.refresh_from_db()
        assert row.status == status

    def test_a_row_requeuing_has_not_helped_is_handed_to_a_person(self, connected):
        row = self._received(connected, age=dt.timedelta(hours=1, minutes=1))

        with mock.patch("billing.tasks.process_stripe_event.delay") as delay:
            assert webhook.sweep() == {"requeued": 0, "abandoned": 1}

        delay.assert_not_called()
        row.refresh_from_db()
        assert row.status == StripeEventStatus.FAILED
        assert row.error == webhook.ABANDONED
        assert row.processed_at is not None

    def test_the_sweep_actually_applies_the_event(self, connected, no_fee):
        """Through the task and Celery's eager path: the row ends PROCESSED."""
        from billing.tasks import sweep_stripe_events

        invoice = IssuedInvoiceFactory(organization=connected)
        row = ledgered(invoice, session(invoice))
        StripeEvent.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=30)
        )

        assert sweep_stripe_events.apply().get() == {"requeued": 1, "abandoned": 0}

        row.refresh_from_db()
        assert row.status == StripeEventStatus.PROCESSED
        assert Payment.objects.count() == 1

    def test_beat_runs_it_every_five_minutes(self, settings):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-stripe-events"]

        assert entry["task"] == "billing.sweep_stripe_events"
        assert str(entry["schedule"]) == "<crontab: */5 * * * * (m/h/dM/MY/d)>"
