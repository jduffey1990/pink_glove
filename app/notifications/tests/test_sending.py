"""
Sending a reminder: at most once per visit and time, and what it says.

The mail backend under test settings is locmem, so `mail.outbox` is every
email the code tried to send.
"""

import datetime as dt
import logging

import pytest
from django.core import mail
from django.core.mail import EmailMessage

from notifications.models import VisitReminder
from notifications.services import due_reminders, send_due_reminders, send_reminder
from notifications.tests.clock import NOW, TOMORROW_2PM


@pytest.mark.django_db
class TestOnce:
    def test_a_due_visit_gets_one_email_and_a_ledger_row(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)

        assert send_due_reminders(org, NOW) == 1

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [visit.customer.email]
        reminder = VisitReminder.objects.get()
        assert reminder.job == visit
        assert reminder.organization == org
        assert reminder.scheduled_start == visit.scheduled_start
        assert reminder.recipient == visit.customer.email
        assert reminder.sent_at == NOW

    def test_running_twice_sends_once(self, org, make_visit):
        """Beat ticking twice, or the next hour's run: the ledger answers, not the clock."""
        make_visit(org, TOMORROW_2PM)

        assert send_due_reminders(org, NOW) == 1
        assert send_due_reminders(org, NOW) == 0
        assert send_due_reminders(org, NOW + dt.timedelta(hours=1)) == 0

        assert len(mail.outbox) == 1
        assert VisitReminder.objects.count() == 1

    def test_a_run_that_lost_the_race_sends_nothing(self, org, make_visit):
        """
        Two runs select the same visit; the first to insert the ledger row
        wins. Played in order here: the losing run selected before the
        winner's row existed, and only then tries to claim.
        """
        make_visit(org, TOMORROW_2PM)
        selected_by_the_loser = list(due_reminders(org, NOW))

        assert send_due_reminders(org, NOW) == 1
        assert send_reminder(selected_by_the_loser[0], NOW) is False

        assert len(mail.outbox) == 1
        assert VisitReminder.objects.count() == 1

    def test_a_claim_whose_send_never_finished_is_not_retried(self, org, make_visit):
        """
        A worker that died between the claim and the send leaves a row with
        no `sent_at`. The customer may have the email, so it is left alone.
        """
        visit = make_visit(org, TOMORROW_2PM)
        VisitReminder.objects.create(
            organization=org,
            job=visit,
            scheduled_start=visit.scheduled_start,
            recipient=visit.customer.email,
            sequence=1,
        )

        assert send_due_reminders(org, NOW) == 0
        assert mail.outbox == []


@pytest.mark.django_db
class TestFailure:
    @pytest.fixture
    def broken_mail(self, monkeypatch):
        def refuse(self, fail_silently=False):
            raise ConnectionRefusedError("smtp is down")

        monkeypatch.setattr(EmailMessage, "send", refuse)

    def test_a_failed_send_frees_the_visit_for_the_next_run(
        self, org, make_visit, broken_mail, monkeypatch
    ):
        make_visit(org, TOMORROW_2PM)

        assert send_due_reminders(org, NOW) == 0
        assert not VisitReminder.objects.exists()

        monkeypatch.undo()
        assert send_due_reminders(org, NOW + dt.timedelta(hours=1)) == 1
        assert len(mail.outbox) == 1

    def test_the_failure_is_logged_without_the_address(self, org, make_visit, broken_mail, caplog):
        visit = make_visit(org, TOMORROW_2PM)

        with caplog.at_level(logging.ERROR, logger="notifications.services"):
            send_due_reminders(org, NOW)

        assert str(visit.pk) in caplog.text
        assert visit.customer.email not in caplog.text


@pytest.mark.django_db
class TestRescheduled:
    def test_a_visit_moved_after_its_reminder_is_reminded_of_the_new_time(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)
        send_due_reminders(org, NOW)

        visit.scheduled_start += dt.timedelta(hours=2)
        visit.scheduled_end += dt.timedelta(hours=2)
        visit.save()
        assert send_due_reminders(org, NOW + dt.timedelta(hours=1)) == 1

        assert len(mail.outbox) == 2
        assert "4:00 PM" in mail.outbox[1].subject
        assert VisitReminder.objects.count() == 2

    def test_moved_back_to_a_time_already_reminded_it_is_reminded_again(self, org, make_visit):
        """The latest reminder said 4:00 PM; the customer must hear 2:00 PM stands again."""
        visit = make_visit(org, TOMORROW_2PM)
        send_due_reminders(org, NOW)
        move(visit, hours=2)
        send_due_reminders(org, NOW)

        move(visit, hours=-2)

        assert send_due_reminders(org, NOW) == 1
        assert len(mail.outbox) == 3
        assert "2:00 PM" in mail.outbox[2].subject
        assert list(
            VisitReminder.objects.order_by("sequence").values_list("sequence", "scheduled_start")
        ) == [
            (1, TOMORROW_2PM),
            (2, TOMORROW_2PM + dt.timedelta(hours=2)),
            (3, TOMORROW_2PM),
        ]
        assert send_due_reminders(org, NOW) == 0

    def test_moved_twice_in_a_minute_it_sends_twice(self, org, make_visit):
        """The accepted cost (ADR-035): each move is a time worth telling."""
        visit = make_visit(org, TOMORROW_2PM)
        send_due_reminders(org, NOW)
        move(visit, hours=1)
        send_due_reminders(org, NOW)
        move(visit, hours=1)
        send_due_reminders(org, NOW + dt.timedelta(minutes=1))

        assert [m.subject.rsplit(" at ", 1)[1] for m in mail.outbox] == [
            "2:00 PM",
            "3:00 PM",
            "4:00 PM",
        ]

    def test_a_run_that_lost_the_race_for_the_next_reminder_sends_nothing(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)
        send_due_reminders(org, NOW)
        move(visit, hours=1)
        selected_by_the_loser = list(due_reminders(org, NOW))

        assert send_due_reminders(org, NOW) == 1
        assert send_reminder(selected_by_the_loser[0], NOW) is False
        assert len(mail.outbox) == 2


def move(visit, *, hours):
    visit.scheduled_start += dt.timedelta(hours=hours)
    visit.scheduled_end += dt.timedelta(hours=hours)
    visit.save()


@pytest.mark.django_db
class TestWhatItSays:
    @pytest.fixture
    def sent(self, org, make_visit):
        visit = make_visit(
            org,
            TOMORROW_2PM,
            customer__first_name="Dana",
            location__line1="1180 Marion Street",
            location__access_notes="Key under the blue pot.",
        )
        send_due_reminders(org, NOW)
        return visit, mail.outbox[0]

    def test_the_subject_names_the_organization_the_day_and_the_local_time(self, sent):
        _, message = sent

        assert (
            message.subject == "Reminder: Sparkle Clean is coming Thursday 19 November at 2:00 PM"
        )

    def test_the_body_has_the_visit_in_local_time(self, sent):
        visit, message = sent

        assert "Hi Dana," in message.body
        assert visit.service.name in message.body
        assert "Thursday 19 November" in message.body
        assert "2:00 PM" in message.body
        assert "4:00 PM" in message.body
        assert "1180 Marion Street" in message.body
        # 14:00 MST is 21:00 UTC: the server's clock must not leak through.
        assert "9:00 PM" not in message.body

    def test_it_says_how_to_reach_the_organization(self, sent):
        _, message = sent

        assert "303-555-0100" in message.body
        assert "office@sparkle.example" in message.body
        assert message.reply_to == ["office@sparkle.example"]

    def test_it_carries_no_access_notes(self, sent):
        """An email is not the audited reveal (ADR-014)."""
        _, message = sent

        assert "blue pot" not in message.body

    def test_it_is_html(self, sent):
        _, message = sent

        assert message.content_subtype == "html"

    def test_an_organization_with_no_email_sets_no_reply_to(self, org, make_visit):
        org.email = ""
        org.save(update_fields=["email", "updated_at"])
        make_visit(org, TOMORROW_2PM)

        send_due_reminders(org, NOW)

        assert mail.outbox[0].reply_to == []
