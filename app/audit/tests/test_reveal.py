import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import ACCESS_WARNING, AccessReveal
from customers.models import Customer, ServiceLocation
from users.enums import Role


@pytest.fixture
def location(db, organization):
    customer = Customer.objects.create(organization=organization, first_name="Dana", last_name="Wu")
    return ServiceLocation.objects.create(
        organization=organization,
        customer=customer,
        label="Home",
        line1="1 Elm St",
        city="Denver",
        state="CO",
        postal_code="80202",
        gate_code="4821#",
        alarm_code="9930",
    )


def reveal_url(location):
    return reverse("customers:location-reveal-access", args=[location.id])


@pytest.mark.django_db
class TestCodesAreNotExposedNormally:
    def test_location_detail_does_not_return_codes(self, authed_client, location):
        body = authed_client.get(reverse("customers:location-detail", args=[location.id])).json()

        assert "gate_code" not in body
        assert "alarm_code" not in body
        assert "key_location" not in body

    def test_detail_reports_that_codes_exist_without_revealing_them(self, authed_client, location):
        body = authed_client.get(reverse("customers:location-detail", args=[location.id])).json()

        assert body["has_access_codes"] is True

    def test_codes_remain_writable(self, authed_client, location):
        response = authed_client.patch(
            reverse("customers:location-detail", args=[location.id]),
            {"gate_code": "1111"},
            format="json",
        )

        assert response.status_code == 200
        assert "gate_code" not in response.json()
        location.refresh_from_db()
        assert location.gate_code == "1111"

    def test_customer_list_does_not_leak_codes(self, authed_client, location):
        body = authed_client.get(reverse("customers:customer-list")).json()
        nested = body["results"][0]["locations"][0]

        assert "gate_code" not in nested


@pytest.mark.django_db
class TestReveal:
    def test_acknowledged_reveal_returns_the_codes(self, authed_client, location):
        response = authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert response.json()["gate_code"] == "4821#"
        assert response.json()["alarm_code"] == "9930"

    def test_reveal_without_acknowledgement_is_refused_and_returns_the_warning(
        self, authed_client, location
    ):
        response = authed_client.post(reveal_url(location), {"acknowledged": False}, format="json")

        assert response.status_code == 400
        assert response.json()["detail"] == ACCESS_WARNING
        assert not AccessReveal.objects.exists()

    def test_acknowledgement_is_required_server_side(self, authed_client, location):
        """The warning is part of the contract, not a dialog the UI could drop."""
        response = authed_client.post(reveal_url(location), {}, format="json")

        assert response.status_code == 400
        assert not AccessReveal.objects.exists()

    def test_a_row_is_written_with_who_what_and_where(self, authed_client, location, owner):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        reveal = AccessReveal.objects.get()
        assert reveal.user == owner
        assert reveal.location == location
        assert reveal.organization == location.organization
        assert set(reveal.fields_revealed) == {"gate_code", "alarm_code"}
        assert reveal.acknowledged is True
        assert reveal.ip_address is not None

    def test_only_populated_fields_are_listed(self, authed_client, location):
        location.alarm_code = ""
        location.save()

        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert AccessReveal.objects.get().fields_revealed == ["gate_code"]

    def test_reveals_are_not_flagged_at_request_time(self, authed_client, location):
        """
        Flagging is an end-of-day judgement (Phase 3). Deciding at request time
        would bake in a verdict from a schedule that may still change.
        """
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        reveal = AccessReveal.objects.get()
        assert reveal.is_flagged is False
        assert reveal.evaluated_at is None

    def test_a_cleaner_with_no_assignment_is_refused(
        self, api_client, organization, location, make_member
    ):
        """
        Changed in Phase 3a.6 (ADR-017). Before jobs existed, any staff member
        could reveal and the end-of-day pass sorted it out afterwards. A
        cleaner has no legitimate reason to be in a location's record without
        an assignment, and a flag cannot undo the exposure -- so this is
        refused at request time and no row is written.
        """
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 403
        assert not AccessReveal.objects.exists()

    def test_a_customer_cannot_reveal(self, api_client, organization, location, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert (
            api_client.post(reveal_url(location), {"acknowledged": True}, format="json").status_code
            == 403
        )

    def test_cannot_reveal_another_organizations_location(
        self, api_client, other_organization, location, make_member
    ):
        api_client.force_login(make_member(other_organization, role=Role.OWNER))

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 404
        assert not AccessReveal.objects.exists()


@pytest.mark.django_db
class TestAppendOnly:
    def test_rows_refuse_deletion(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()

        with pytest.raises(NotImplementedError):
            reveal.delete()

        with pytest.raises(NotImplementedError):
            reveal.hard_delete()

    def test_there_is_no_update_or_destroy_route(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()
        url = reverse("audit:access-reveal-detail", args=[reveal.id])

        assert authed_client.patch(url, {"is_flagged": False}, format="json").status_code == 405
        assert authed_client.delete(url).status_code == 405


@pytest.mark.django_db
class TestAuditTrailAccess:
    def test_an_owner_can_read_the_trail(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        body = authed_client.get(reverse("audit:access-reveal-list")).json()

        assert body["count"] == 1
        assert body["results"][0]["user_email"]
        assert body["results"][0]["customer_name"] == "Dana Wu"

    def test_a_cleaner_cannot_read_the_trail(self, api_client, organization, location, make_member):
        """The people being recorded should not be able to curate the record."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        assert api_client.get(reverse("audit:access-reveal-list")).status_code == 403

    def test_a_dispatcher_cannot_read_the_trail(
        self, api_client, organization, location, make_member
    ):
        api_client.force_login(make_member(organization, role=Role.DISPATCHER))

        assert api_client.get(reverse("audit:access-reveal-list")).status_code == 403

    def test_the_trail_is_scoped_to_the_organization(
        self, api_client, authed_client, organization, other_organization, location, make_member
    ):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        api_client.force_login(make_member(other_organization, role=Role.OWNER))
        body = api_client.get(reverse("audit:access-reveal-list")).json()

        assert body["count"] == 0

    def test_reviewing_a_reveal_records_who_closed_it(self, authed_client, location, owner):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()

        response = authed_client.post(
            reverse("audit:access-reveal-review", args=[reveal.id]),
            {"review_note": "Job ran late, confirmed with dispatcher."},
            format="json",
        )

        assert response.status_code == 200
        reveal.refresh_from_db()
        assert reveal.reviewed_by == owner
        assert reveal.reviewed_at is not None
        assert "ran late" in reveal.review_note

    def _review(self, client, reveal, note="Asked; fine."):
        return client.post(
            reverse("audit:access-reveal-review", args=[reveal.id]),
            {"review_note": note},
            format="json",
        )

    def test_an_admin_cannot_review_their_own_reveal(
        self, api_client, organization, location, make_member
    ):
        admin = make_member(organization, role=Role.ADMIN)
        api_client.force_login(admin)
        api_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()

        response = self._review(api_client, reveal)

        reveal.refresh_from_db()
        assert response.status_code == 403
        assert reveal.reviewed_at is None

    def test_an_admin_can_review_someone_elses(
        self, api_client, authed_client, organization, location, make_member
    ):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()
        admin = make_member(organization, role=Role.ADMIN)
        api_client.force_login(admin)

        assert self._review(api_client, reveal).status_code == 200
        reveal.refresh_from_db()
        assert reveal.reviewed_by == admin

    def test_a_review_is_final(self, api_client, authed_client, organization, location, owner):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()
        self._review(authed_client, reveal, note="First word.")

        response = self._review(authed_client, reveal, note="Rewritten.")

        reveal.refresh_from_db()
        assert response.status_code == 409
        assert reveal.review_note == "First word."

    def test_a_dispatcher_cannot_review(
        self, api_client, authed_client, organization, location, make_member
    ):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        api_client.force_login(make_member(organization, role=Role.DISPATCHER))

        assert self._review(api_client, AccessReveal.objects.get()).status_code == 403

    def test_another_organizations_reveal_cannot_be_reviewed(
        self, api_client, authed_client, location, rival_owner
    ):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        api_client.force_login(rival_owner)

        assert self._review(api_client, AccessReveal.objects.get()).status_code == 404


@pytest.mark.django_db
class TestRevealsBindToAJob:
    """
    Phase 3a.6 (ADR-016, ADR-017): a reveal records *which visit* it was for,
    so the end-of-day pass has a window to judge it against rather than only a
    person and a time.
    """

    @pytest.fixture
    def cleaner(self, organization, make_member):
        return make_member(organization, role=Role.CLEANER, email="cleaner@example.com")

    @pytest.fixture
    def dispatcher(self, organization, make_member):
        return make_member(organization, role=Role.DISPATCHER, email="dispatcher@example.com")

    def _job_at(self, organization, location, *, start):
        from scheduling.tests.factories import JobFactory

        return JobFactory(
            organization=organization,
            customer=location.customer,
            location=location,
            scheduled_start=start,
            scheduled_end=start + dt.timedelta(hours=2),
        )

    def _assign(self, organization, job, user):
        from scheduling.models import JobAssignment

        return JobAssignment.objects.create(organization=organization, job=job, user=user)

    def test_a_cleaner_on_a_job_today_gets_the_codes_and_the_binding(
        self, api_client, organization, location, cleaner
    ):
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        self._assign(organization, job, cleaner)
        api_client.force_login(cleaner)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert response.json()["job"] == str(job.id)
        assert AccessReveal.objects.get().job_id == job.id

    def test_a_cleaner_assigned_three_days_out_is_refused(
        self, api_client, organization, location, cleaner
    ):
        """The 24-hour band is coarse, but it is not unbounded."""
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(days=3))
        self._assign(organization, job, cleaner)
        api_client.force_login(cleaner)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 403
        assert not AccessReveal.objects.exists()

    def test_a_cleaner_on_a_deleted_job_is_refused(
        self, api_client, organization, location, cleaner
    ):
        """A deleted visit justifies nothing -- and would log a reveal bound to no job."""
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        self._assign(organization, job, cleaner)
        job.delete()
        api_client.force_login(cleaner)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 403
        assert not AccessReveal.objects.exists()

    def test_a_cleaner_taken_off_the_job_is_refused(
        self, api_client, organization, location, cleaner
    ):
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        self._assign(organization, job, cleaner).delete()
        api_client.force_login(cleaner)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 403

    def test_a_cleaner_assigned_to_a_cancelled_job_is_refused(
        self, api_client, organization, location, cleaner
    ):
        from scheduling.enums import JobStatus

        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        job.status = JobStatus.CANCELLED
        job.save()
        self._assign(organization, job, cleaner)
        api_client.force_login(cleaner)

        assert (
            api_client.post(reveal_url(location), {"acknowledged": True}, format="json").status_code
            == 403
        )

    def test_the_nearest_job_wins_when_several_qualify(
        self, api_client, organization, location, cleaner
    ):
        """A morning and an evening visit at one address is an ordinary day."""
        soon = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        later = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=8))
        self._assign(organization, soon, cleaner)
        self._assign(organization, later, cleaner)
        api_client.force_login(cleaner)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.json()["job"] == str(soon.id)

    def test_a_dispatcher_with_no_job_reveals_with_a_null_binding(
        self, api_client, organization, location, dispatcher
    ):
        api_client.force_login(dispatcher)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert response.json()["job"] is None
        assert AccessReveal.objects.get().job_id is None

    def test_a_dispatcher_can_name_the_job(self, api_client, organization, location, dispatcher):
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        api_client.force_login(dispatcher)

        response = api_client.post(
            reveal_url(location), {"acknowledged": True, "job": str(job.id)}, format="json"
        )

        assert response.status_code == 200
        assert AccessReveal.objects.get().job_id == job.id

    def test_a_dispatcher_naming_another_organizations_job_is_a_400(
        self, api_client, organization, other_organization, location, dispatcher
    ):
        from scheduling.tests.factories import JobFactory

        rival_job = JobFactory(organization=other_organization)
        api_client.force_login(dispatcher)

        response = api_client.post(
            reveal_url(location),
            {"acknowledged": True, "job": str(rival_job.id)},
            format="json",
        )

        assert response.status_code == 400
        assert not AccessReveal.objects.exists()

    def test_a_dispatcher_naming_a_job_at_another_location_is_a_400(
        self, api_client, organization, location, dispatcher
    ):
        from scheduling.tests.factories import JobFactory

        elsewhere = JobFactory(organization=organization)
        api_client.force_login(dispatcher)

        response = api_client.post(
            reveal_url(location),
            {"acknowledged": True, "job": str(elsewhere.id)},
            format="json",
        )

        assert response.status_code == 400

    def test_a_dispatcher_with_exactly_one_open_job_binds_to_it(
        self, api_client, organization, location, dispatcher
    ):
        job = self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        api_client.force_login(dispatcher)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.json()["job"] == str(job.id)

    def test_a_dispatcher_with_two_open_jobs_binds_to_neither(
        self, api_client, organization, location, dispatcher
    ):
        """Guessing between them would put the wrong visit on an audit row."""
        self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=1))
        self._job_at(organization, location, start=timezone.now() + dt.timedelta(hours=8))
        api_client.force_login(dispatcher)

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert response.json()["job"] is None


@pytest.mark.django_db
class TestTheEvaluatorJudgesBoundReveals:
    """The two halves joined up: a bound reveal gets a window to be judged against."""

    @pytest.fixture
    def cleaner(self, organization, make_member):
        return make_member(organization, role=Role.CLEANER, email="cleaner@example.com")

    def _yesterday_job_and_reveal(self, organization, location, cleaner, *, reveal_hour):
        import datetime as dt
        from zoneinfo import ZoneInfo

        from scheduling.models import JobAssignment
        from scheduling.tests.factories import JobFactory

        tz = ZoneInfo(organization.timezone)
        day = timezone.now().astimezone(tz).date() - dt.timedelta(days=1)
        while day.isoweekday() > 5:
            day -= dt.timedelta(days=1)

        start = dt.datetime(day.year, day.month, day.day, 9, 0, tzinfo=tz)
        job = JobFactory(
            organization=organization,
            customer=location.customer,
            location=location,
            scheduled_start=start,
            scheduled_end=start + dt.timedelta(hours=2),
        )
        JobAssignment.objects.create(organization=organization, job=job, user=cleaner)

        reveal = AccessReveal.objects.create(
            organization=organization,
            user=cleaner,
            location=location,
            job=job,
            fields_revealed=["gate_code"],
            acknowledged=True,
        )
        moment = dt.datetime(day.year, day.month, day.day, reveal_hour, 0, tzinfo=tz)
        AccessReveal.objects.filter(pk=reveal.pk).update(created_at=moment)
        return reveal

    def test_a_reveal_inside_the_window_is_cleared(self, organization, location, cleaner):
        from audit.tasks import evaluate_access_reveals

        reveal = self._yesterday_job_and_reveal(organization, location, cleaner, reveal_hour=9)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is not None
        assert not reveal.is_flagged

    def test_a_reveal_hours_outside_the_window_is_flagged(self, organization, location, cleaner):
        from audit.tasks import OUTSIDE_JOB_WINDOW, evaluate_access_reveals

        reveal = self._yesterday_job_and_reveal(organization, location, cleaner, reveal_hour=5)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.is_flagged
        assert reveal.flag_reason == OUTSIDE_JOB_WINDOW


@pytest.mark.django_db
class TestAccessWarningEndpoint:
    """
    The warning is served on its own so the frontend never hardcodes it.

    It used to be scraped out of the 400 an unacknowledged reveal returns.
    Same single source of truth, but a deliberate 400 on a healthy path is
    indistinguishable from a real failure in a console or an error tracker.
    """

    def warning_url(self, location):
        return reverse("customers:location-access-warning", args=[location.id])

    def test_it_serves_the_backends_own_copy(self, authed_client, location):
        response = authed_client.get(self.warning_url(location))

        assert response.status_code == 200
        assert response.json()["warning"] == ACCESS_WARNING

    def test_asking_for_it_logs_nothing(self, authed_client, location):
        """No reveal happened, so there is nothing to record."""
        authed_client.get(self.warning_url(location))

        assert not AccessReveal.objects.exists()

    def test_a_cleaner_with_no_assignment_cannot_read_it(
        self, api_client, organization, location, make_member
    ):
        """Gated exactly as the reveal is; it is the same door."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        assert api_client.get(self.warning_url(location)).status_code == 403

    def test_another_organizations_location_is_a_404(
        self, api_client, other_organization, location, make_member
    ):
        api_client.force_login(make_member(other_organization, role=Role.OWNER))

        assert api_client.get(self.warning_url(location)).status_code == 404

    def test_a_customer_cannot_read_it(self, api_client, organization, location, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert api_client.get(self.warning_url(location)).status_code == 403

    def test_it_matches_what_the_unacknowledged_reveal_returns(self, authed_client, location):
        """The two paths must not drift; that is the whole point."""
        served = authed_client.get(self.warning_url(location)).json()["warning"]
        refused = authed_client.post(
            reveal_url(location), {"acknowledged": False}, format="json"
        ).json()["detail"]

        assert served == refused
