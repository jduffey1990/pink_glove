"""
The timesheet report.

The questions worth a test are the ones a payroll figure gets wrong quietly:
which day an entry that crosses midnight belongs to, what a 25-hour day does
to the edge of a period, whether someone still clocked in is counted, and
whether another tenant's hours can leak into the total. ADR-030 decides each;
these pin them.

The organization under test is in Denver, which is never UTC and changes its
offset twice a year. 2027's changes are 14 March (a 23-hour day) and
7 November (a 25-hour one).
"""

import csv
import datetime as dt
import io

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from scheduling.models import JobAssignment, TimeEntry
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    OrganizationFactory,
    TimeEntryFactory,
    UserFactory,
)
from users.enums import Role

URL = reverse("scheduling:report-timesheet")
CSV_URL = reverse("scheduling:report-timesheet-csv")


def utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def period(date_from: str, date_to: str | None = None) -> dict:
    return {"date_from": date_from, "date_to": date_to or date_from}


def worked(organization, user, clock_in, *, hours=2.0, still_open=False, job=None, customer=None):
    """One time entry: `hours` long from `clock_in`, or open."""
    if job is None:
        extra = {"customer": customer} if customer is not None else {}
        job = JobFactory(organization=organization, **extra)
    return TimeEntryFactory(
        organization=organization,
        job=job,
        user=user,
        clock_in=clock_in,
        clock_out=None if still_open else clock_in + dt.timedelta(hours=hours),
    )


def read_csv(response) -> list[list[str]]:
    """Parsed rows. `utf-8-sig` because the file opens with a BOM for Excel."""
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.fixture
def admin(organization, make_member):
    return make_member(organization, role=Role.ADMIN, email="admin-here@example.com")


@pytest.fixture
def owner_client(owner, make_client):
    return make_client(owner)


# --- who may read it -------------------------------------------------------


@pytest.mark.django_db
class TestPermissions:
    @pytest.mark.parametrize("url", [URL, CSV_URL])
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_owner_and_admin_may_read_it(self, url, role, organization, make_member, make_client):
        client = make_client(make_member(organization, role=role, email=f"{role}@example.com"))

        assert client.get(url, period("2027-06-14")).status_code == 200

    @pytest.mark.parametrize("url", [URL, CSV_URL])
    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_everyone_else_is_refused(self, url, role, organization, make_member, make_client):
        client = make_client(make_member(organization, role=role, email=f"{role}@example.com"))

        assert client.get(url, period("2027-06-14")).status_code == 403

    @pytest.mark.parametrize("url", [URL, CSV_URL])
    def test_nobody_signed_in_is_refused(self, url, api_client):
        assert api_client.get(url, period("2027-06-14")).status_code == 403

    def test_a_refused_csv_is_json_not_a_file(self, organization, make_member, make_client):
        """The refusal must not arrive looking like a download."""
        client = make_client(make_member(organization, role=Role.DISPATCHER))

        response = client.get(CSV_URL, period("2027-06-14"))

        assert response.status_code == 403
        assert response["Content-Type"] == "application/json"
        assert "Content-Disposition" not in response


# --- tenant isolation ------------------------------------------------------


@pytest.mark.django_db
class TestTenantIsolation:
    """
    The two organizations work different hours at the same instants, so a
    report read from the wrong tenant is a wrong number rather than a subtly
    plausible one.
    """

    @pytest.fixture
    def both_worked(self, organization, other_organization):
        ours = UserFactory(first_name="Ana", last_name="Ours")
        theirs = UserFactory(first_name="Theo", last_name="Theirs")
        # 18:00Z is midday in Denver and early afternoon in New York: the
        # 14th in both.
        worked(organization, ours, utc(2027, 6, 14, 18), hours=2)
        worked(other_organization, theirs, utc(2027, 6, 14, 18), hours=5)
        return ours, theirs

    def test_a_report_holds_only_its_own_organization(
        self, both_worked, owner_client, rival_owner, make_client
    ):
        ours, theirs = both_worked

        mine = owner_client.get(URL, period("2027-06-14")).json()
        rivals = make_client(rival_owner).get(URL, period("2027-06-14")).json()

        assert mine["total_seconds"] == 2 * 3600
        assert [person["user"] for person in mine["staff"]] == [str(ours.pk)]
        assert rivals["total_seconds"] == 5 * 3600
        assert [person["user"] for person in rivals["staff"]] == [str(theirs.pk)]

    def test_the_csv_holds_only_its_own_organization(self, both_worked, owner_client):
        rows = read_csv(owner_client.get(CSV_URL, period("2027-06-14")))

        assert [row[0] for row in rows[1:]] == ["Ana Ours"]

    def test_someone_working_for_both_is_reported_once_each(
        self, organization, other_organization, owner_client
    ):
        """One person, two tenants: each sees only the hours worked for it."""
        moonlighter = UserFactory()
        worked(organization, moonlighter, utc(2027, 6, 14, 18), hours=1)
        worked(other_organization, moonlighter, utc(2027, 6, 14, 20), hours=7)

        report = owner_client.get(URL, period("2027-06-14")).json()

        assert report["total_seconds"] == 3600
        assert report["entry_count"] == 1


# --- which day owns an entry ----------------------------------------------


@pytest.mark.django_db
class TestWhichDayOwnsAnEntry:
    def test_an_evening_entry_is_filed_under_its_local_day(self, organization, owner_client):
        # 05:30Z on the 15th is 23:30 on the 14th in Denver.
        worked(organization, UserFactory(), utc(2027, 6, 15, 5, 30), hours=1)

        on_the_14th = owner_client.get(URL, period("2027-06-14")).json()
        on_the_15th = owner_client.get(URL, period("2027-06-15")).json()

        assert on_the_14th["entry_count"] == 1
        assert on_the_14th["staff"][0]["entries"][0]["local_date"] == "2027-06-14"
        assert on_the_15th["entry_count"] == 0

    def test_an_entry_that_crosses_midnight_is_whole_on_the_day_it_began(
        self, organization, owner_client
    ):
        """22:00 to 02:00 is four hours on the first day and none on the second."""
        # 04:00Z on the 15th is 22:00 on the 14th in Denver.
        worked(organization, UserFactory(), utc(2027, 6, 15, 4), hours=4)

        first = owner_client.get(URL, period("2027-06-14")).json()
        second = owner_client.get(URL, period("2027-06-15")).json()

        assert first["total_seconds"] == 4 * 3600
        assert second["total_seconds"] == 0
        assert second["staff"] == []

    def test_adjacent_periods_add_up_to_the_whole(self, organization, owner_client):
        """
        The reason nothing is split: an entry crossing the boundary between
        two pay periods is paid once, in the period it began in.
        """
        cleaner = UserFactory()
        worked(organization, cleaner, utc(2027, 6, 21, 4), hours=4)  # Sun 20th 22:00, into Mon
        worked(organization, cleaner, utc(2027, 6, 21, 15), hours=3)  # Mon 21st 09:00

        week_one = owner_client.get(URL, period("2027-06-14", "2027-06-20")).json()
        week_two = owner_client.get(URL, period("2027-06-21", "2027-06-27")).json()
        both = owner_client.get(URL, period("2027-06-14", "2027-06-27")).json()

        assert week_one["total_seconds"] == 4 * 3600
        assert week_two["total_seconds"] == 3 * 3600
        assert both["total_seconds"] == week_one["total_seconds"] + week_two["total_seconds"]

    def test_both_ends_of_the_period_are_inclusive(self, organization, owner_client):
        cleaner = UserFactory()
        # Denver is UTC-6 in June, so its day runs 06:00Z to 06:00Z.
        worked(organization, cleaner, utc(2027, 6, 10, 6, 0), hours=1)  # 00:00 on the 10th
        worked(organization, cleaner, utc(2027, 6, 13, 5, 59), hours=1)  # 23:59 on the 12th
        worked(organization, cleaner, utc(2027, 6, 10, 5, 59), hours=1)  # 23:59 on the 9th
        worked(organization, cleaner, utc(2027, 6, 13, 6, 0), hours=1)  # 00:00 on the 13th

        report = owner_client.get(URL, period("2027-06-10", "2027-06-12")).json()

        assert report["entry_count"] == 2
        assert [e["local_date"] for e in report["staff"][0]["entries"]] == [
            "2027-06-10",
            "2027-06-12",
        ]


@pytest.mark.django_db
class TestDaylightSaving:
    """
    Invariant 8. A day's bounds come from `Organization.local_day_bounds`,
    which builds both midnights in the zone. Taking the offset at the start
    of the day and adding 24 hours is the bug these would catch: it is an
    hour out at one end, twice a year.
    """

    def test_the_last_hour_of_a_25_hour_day_still_belongs_to_it(self, organization, owner_client):
        # 7 November 2027: clocks go back at 02:00 MDT. The day starts at
        # 06:00Z (MDT, UTC-6) and ends at 07:00Z on the 8th (MST, UTC-7).
        cleaner = UserFactory()
        worked(organization, cleaner, utc(2027, 11, 7, 6, 0), hours=1)  # 00:00 MDT, first instant
        worked(organization, cleaner, utc(2027, 11, 8, 6, 30), hours=1)  # 23:30 MST, 25th hour
        worked(organization, cleaner, utc(2027, 11, 8, 7, 0), hours=1)  # 00:00 MST on the 8th

        the_7th = owner_client.get(URL, period("2027-11-07")).json()
        the_8th = owner_client.get(URL, period("2027-11-08")).json()

        assert the_7th["entry_count"] == 2
        assert {e["local_date"] for e in the_7th["staff"][0]["entries"]} == {"2027-11-07"}
        assert the_8th["entry_count"] == 1

    def test_hours_across_the_repeated_hour_are_elapsed_not_wall_clock(
        self, organization, owner_client
    ):
        """00:30 MDT to 02:30 MST reads as two hours on a clock and is three."""
        cleaner = UserFactory()
        TimeEntryFactory(
            organization=organization,
            job=JobFactory(organization=organization),
            user=cleaner,
            clock_in=utc(2027, 11, 7, 6, 30),
            clock_out=utc(2027, 11, 7, 9, 30),
        )

        report = owner_client.get(URL, period("2027-11-07")).json()

        assert report["total_seconds"] == 3 * 3600

    def test_the_hour_after_a_23_hour_day_belongs_to_the_next_one(self, organization, owner_client):
        # 14 March 2027: clocks go forward at 02:00 MST. The day starts at
        # 07:00Z (MST) and ends at 06:00Z on the 15th (MDT).
        cleaner = UserFactory()
        worked(organization, cleaner, utc(2027, 3, 14, 7, 0), hours=1)  # 00:00 MST on the 14th
        worked(organization, cleaner, utc(2027, 3, 15, 5, 30), hours=1)  # 23:30 MDT on the 14th
        worked(organization, cleaner, utc(2027, 3, 15, 6, 30), hours=1)  # 00:30 MDT on the 15th

        the_14th = owner_client.get(URL, period("2027-03-14")).json()
        the_15th = owner_client.get(URL, period("2027-03-15")).json()

        assert the_14th["entry_count"] == 2
        assert the_15th["entry_count"] == 1
        assert the_15th["staff"][0]["entries"][0]["local_date"] == "2027-03-15"

    def test_a_zone_ahead_of_utc_with_southern_seasons(self, make_member, make_client):
        """Auckland: UTC+13 in summer, and its 25-hour day is in April."""
        auckland = OrganizationFactory(timezone="Pacific/Auckland")
        client = make_client(make_member(auckland, role=Role.OWNER, email="nz@example.com"))
        cleaner = UserFactory()
        # 4 April 2027: clocks go back at 03:00 NZDT. The day runs from
        # 11:00Z on the 3rd (UTC+13) to 12:00Z on the 4th (UTC+12).
        worked(auckland, cleaner, utc(2027, 4, 3, 11, 0), hours=1)  # 00:00 on the 4th
        worked(auckland, cleaner, utc(2027, 4, 4, 11, 30), hours=1)  # 23:30 on the 4th
        worked(auckland, cleaner, utc(2027, 4, 4, 12, 0), hours=1)  # 00:00 on the 5th

        report = client.get(URL, period("2027-04-04")).json()

        assert report["timezone"] == "Pacific/Auckland"
        assert report["entry_count"] == 2


# --- what is counted -------------------------------------------------------


@pytest.mark.django_db
class TestWhatIsCounted:
    def test_totals_per_person_and_overall(self, organization, owner_client):
        ana = UserFactory(first_name="Ana", last_name="Lopez")
        ben = UserFactory(first_name="Ben", last_name="Okafor")
        worked(organization, ana, utc(2027, 6, 14, 15), hours=2)
        worked(organization, ana, utc(2027, 6, 15, 15), hours=1.5)
        worked(organization, ben, utc(2027, 6, 14, 15), hours=3)

        report = owner_client.get(URL, period("2027-06-14", "2027-06-15")).json()

        by_name = {person["name"]: person for person in report["staff"]}
        assert by_name["Ana Lopez"]["total_seconds"] == 12600
        assert by_name["Ana Lopez"]["entry_count"] == 2
        assert by_name["Ben Okafor"]["total_seconds"] == 10800
        assert report["total_seconds"] == 23400
        assert report["entry_count"] == 3

    def test_an_entry_carries_what_the_page_needs(self, organization, owner_client):
        customer = CustomerFactory(organization=organization, first_name="Dana", last_name="Hale")
        entry = worked(organization, UserFactory(), utc(2027, 6, 14, 15), customer=customer)

        row = owner_client.get(URL, period("2027-06-14")).json()["staff"][0]["entries"][0]

        assert row == {
            "id": str(entry.pk),
            "job": str(entry.job_id),
            "customer_name": "Dana Hale",
            "local_date": "2027-06-14",
            "clock_in": "2027-06-14T15:00:00Z",
            "clock_out": "2027-06-14T17:00:00Z",
            "seconds": 7200,
            "is_open": False,
        }

    def test_an_open_entry_is_listed_flagged_and_not_counted(self, organization, owner_client):
        cleaner = UserFactory()
        worked(organization, cleaner, utc(2027, 6, 14, 15), hours=2)
        worked(organization, cleaner, utc(2027, 6, 14, 19), still_open=True)

        report = owner_client.get(URL, period("2027-06-14")).json()
        person = report["staff"][0]
        still_open = person["entries"][1]

        assert person["total_seconds"] == 7200
        assert person["entry_count"] == 2
        assert person["open_entry_count"] == 1
        assert report["open_entry_count"] == 1
        assert still_open["is_open"] is True
        # Null, not zero: zero would read as "worked nothing".
        assert still_open["seconds"] is None
        assert still_open["clock_out"] is None

    def test_a_total_is_the_sum_of_the_rows_under_it(self, organization, owner_client):
        """Sub-second remainders are dropped per entry, never carried into a total."""
        cleaner = UserFactory()
        for hour in (15, 17, 19):
            TimeEntryFactory(
                organization=organization,
                job=JobFactory(organization=organization),
                user=cleaner,
                clock_in=utc(2027, 6, 14, hour),
                clock_out=utc(2027, 6, 14, hour, 30, 0, 900_000),
            )

        person = owner_client.get(URL, period("2027-06-14")).json()["staff"][0]

        assert [e["seconds"] for e in person["entries"]] == [1800, 1800, 1800]
        assert person["total_seconds"] == 5400

    def test_a_correction_is_what_the_report_reads(
        self, organization, owner_client, dispatcher_client
    ):
        """
        The model keeps no earlier version of a corrected entry, so the
        report reads the row as it stands, including which day it is on.
        """
        entry = worked(organization, UserFactory(), utc(2027, 6, 14, 15), hours=2)

        response = dispatcher_client.patch(
            reverse("scheduling:timeentry-detail", args=[entry.pk]),
            {"clock_in": "2027-06-15T15:00:00Z", "clock_out": "2027-06-15T20:00:00Z"},
            format="json",
        )
        assert response.status_code == 200

        assert owner_client.get(URL, period("2027-06-14")).json()["entry_count"] == 0
        assert owner_client.get(URL, period("2027-06-15")).json()["total_seconds"] == 5 * 3600

    def test_hours_do_not_depend_on_still_being_assigned(self, organization, owner_client):
        """Taken off the job afterwards, the cleaner is still owed the hours."""
        cleaner = UserFactory()
        entry = worked(organization, cleaner, utc(2027, 6, 14, 15), hours=2)
        assignment = JobAssignment.objects.create(
            organization=organization, job=entry.job, user=cleaner
        )
        assignment.delete()

        assert owner_client.get(URL, period("2027-06-14")).json()["total_seconds"] == 7200


# --- soft deletes ----------------------------------------------------------


@pytest.mark.django_db
class TestSoftDeletes:
    """
    The manager filters `TimeEntry` and nothing it joins to (CLAUDE.md gate
    item 5), so each relation here is a decision, and each follows the
    foreign key: an entry cascades with its job; a job is protected from its
    customer going.
    """

    def test_a_deleted_entry_is_gone(self, organization, owner_client):
        entry = worked(organization, UserFactory(), utc(2027, 6, 14, 15))
        entry.delete()

        assert owner_client.get(URL, period("2027-06-14")).json()["entry_count"] == 0

    def test_an_entry_on_a_deleted_job_is_gone(self, organization, owner_client):
        cleaner = UserFactory()
        kept = worked(organization, cleaner, utc(2027, 6, 14, 15), hours=1)
        struck = worked(organization, cleaner, utc(2027, 6, 14, 18), hours=4)
        struck.job.delete()

        report = owner_client.get(URL, period("2027-06-14")).json()

        # The entry row itself is still live: only the join can exclude it.
        assert TimeEntry.objects.filter(pk=struck.pk).exists()
        assert report["total_seconds"] == 3600
        assert [e["id"] for e in report["staff"][0]["entries"]] == [str(kept.pk)]

    def test_hours_for_a_deleted_customer_are_still_owed(self, organization, owner_client):
        customer = CustomerFactory(organization=organization, first_name="Gone", last_name="Away")
        worked(organization, UserFactory(), utc(2027, 6, 14, 15), hours=2, customer=customer)
        customer.delete()

        report = owner_client.get(URL, period("2027-06-14")).json()

        assert report["total_seconds"] == 7200
        assert report["staff"][0]["entries"][0]["customer_name"] == "Gone Away"


# --- shape: ordering, stability, cost --------------------------------------


@pytest.mark.django_db
class TestOrderingAndCost:
    def test_staff_by_name_then_id_and_entries_by_clock_in_then_id(
        self, organization, owner_client
    ):
        zed = UserFactory(first_name="zed", last_name="Young")
        amy = UserFactory(first_name="Amy", last_name="Adams")
        twins = sorted(
            [
                UserFactory(first_name="Sam", last_name="Same"),
                UserFactory(first_name="Sam", last_name="Same"),
            ],
            key=lambda user: str(user.pk),
        )
        for user in (zed, amy, *twins):
            worked(organization, user, utc(2027, 6, 14, 15))
        # Two entries at the same instant for one person: only the id orders them.
        tied = sorted(
            [worked(organization, amy, utc(2027, 6, 14, 20)) for _ in range(2)],
            key=lambda entry: str(entry.pk),
        )

        staff = owner_client.get(URL, period("2027-06-14")).json()["staff"]

        assert [person["user"] for person in staff] == [
            str(amy.pk),
            str(twins[0].pk),
            str(twins[1].pk),
            str(zed.pk),
        ]
        assert [e["id"] for e in staff[0]["entries"]][1:] == [str(e.pk) for e in tied]

    @pytest.mark.parametrize("url", [URL, CSV_URL])
    def test_the_same_data_gives_the_same_bytes(self, url, organization, owner_client):
        for _ in range(4):
            cleaner = UserFactory(first_name="Sam", last_name="Same")
            for _ in range(3):
                worked(organization, cleaner, utc(2027, 6, 14, 15))

        first = owner_client.get(url, period("2027-06-14"))
        second = owner_client.get(url, period("2027-06-14"))

        assert first.status_code == 200
        assert first.content == second.content

    @pytest.mark.parametrize("url", [URL, CSV_URL])
    def test_the_query_count_does_not_grow_with_staff_or_customers(
        self, url, organization, owner_client
    ):
        def grow(people):
            for _ in range(people):
                cleaner = UserFactory()
                for _ in range(2):
                    worked(organization, cleaner, utc(2027, 6, 14, 15))

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert owner_client.get(url, period("2027-06-14")).status_code == 200
            return len(captured)

        grow(2)
        small = queries()
        grow(8)

        assert queries() == small


# --- the query string ------------------------------------------------------


@pytest.mark.django_db
class TestTheQueryString:
    @pytest.mark.parametrize("url", [URL, CSV_URL])
    @pytest.mark.parametrize(
        ("query", "field"),
        [
            ({}, "date_from"),
            ({"date_from": "2027-06-14"}, "date_to"),
            ({"date_from": "14/06/2027", "date_to": "2027-06-14"}, "date_from"),
            ({"date_from": "2027-06-14", "date_to": "2027-06-01"}, "date_to"),
            ({"date_from": "2027-01-01", "date_to": "2027-12-31"}, "date_to"),
        ],
    )
    def test_a_bad_period_is_a_400_naming_the_field(self, url, query, field, owner_client):
        response = owner_client.get(url, query)

        assert response.status_code == 400
        assert field in response.json()


# --- the file --------------------------------------------------------------


@pytest.mark.django_db
class TestCSV:
    def test_it_is_a_download_named_for_its_period(self, owner_client):
        response = owner_client.get(CSV_URL, period("2027-06-14", "2027-06-20"))

        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == (
            'attachment; filename="timesheet_2027-06-14_2027-06-20.csv"'
        )

    def test_asking_for_csv_by_name_is_not_refused(self, owner_client):
        """`Accept: text/csv` is the honest header, and DRF's default 406s it."""
        response = owner_client.get(CSV_URL, period("2027-06-14"), headers={"Accept": "text/csv"})

        assert response.status_code == 200

    def test_one_row_per_entry_under_one_header(self, organization, owner_client):
        customer = CustomerFactory(organization=organization, first_name="Dana", last_name="Hale")
        cleaner = UserFactory(first_name="Ana", last_name="Lopez")
        # 05:30Z on the 15th is 23:30 on the 14th in Denver, an hour and a half.
        entry = worked(organization, cleaner, utc(2027, 6, 15, 5, 30), hours=1.5, customer=customer)
        worked(organization, cleaner, utc(2027, 6, 15, 15), still_open=True, customer=customer)

        rows = read_csv(owner_client.get(CSV_URL, period("2027-06-14", "2027-06-15")))

        assert rows[0] == [
            "staff",
            "local_date",
            "customer",
            "job_id",
            "clock_in_utc",
            "clock_out_utc",
            "clock_in_local",
            "clock_out_local",
            "seconds",
            "hours_decimal",
            "state",
        ]
        assert rows[1] == [
            "Ana Lopez",
            "2027-06-14",
            "Dana Hale",
            str(entry.job_id),
            "2027-06-15T05:30:00Z",
            "2027-06-15T07:00:00Z",
            "2027-06-14T23:30:00-06:00",
            "2027-06-15T01:00:00-06:00",
            "5400",
            "1.50",
            "closed",
        ]
        # Open: no clock-out, and empty rather than zero seconds.
        assert rows[2][5] == ""
        assert rows[2][7] == ""
        assert rows[2][8:] == ["", "", "open"]
        assert len(rows) == 3

    def test_the_csv_and_the_json_agree(self, organization, owner_client):
        for _ in range(3):
            worked(organization, UserFactory(), utc(2027, 6, 14, 15), hours=2.25)

        report = owner_client.get(URL, period("2027-06-14")).json()
        rows = read_csv(owner_client.get(CSV_URL, period("2027-06-14")))[1:]

        assert sum(int(row[8]) for row in rows) == report["total_seconds"]
        assert len(rows) == report["entry_count"]

    @pytest.mark.parametrize(
        "name",
        [
            '=HYPERLINK("http://x","y")',
            "+1+1",
            "-10",
            "@SUM(A1:A9)",
            "\t=1+1",
            "\r=1+1",
        ],
    )
    def test_a_name_that_is_a_formula_is_written_as_text(self, name, organization, owner_client):
        """
        Formula injection. The customer and the cleaner both typed their own
        names, and the owner opens this file in a spreadsheet.
        """
        customer = CustomerFactory(organization=organization, first_name="", company_name=name)
        cleaner = UserFactory(first_name=name, last_name="")
        worked(organization, cleaner, utc(2027, 6, 14, 15), customer=customer)

        row = read_csv(owner_client.get(CSV_URL, period("2027-06-14")))[1]

        # `full_name` strips, so a leading tab or return never reaches the
        # staff cell; whatever does reach it must not start a formula.
        assert row[0] == f"'{cleaner.full_name}"
        assert row[2].startswith("'")
        assert row[2][1:].startswith(name[0])

    def test_numbers_stay_numbers(self, organization, owner_client):
        """Only text is neutralised: a quoted number is a number nobody can sum."""
        worked(organization, UserFactory(), utc(2027, 6, 14, 15), hours=2)

        row = read_csv(owner_client.get(CSV_URL, period("2027-06-14")))[1]

        assert row[8] == "7200"
        assert row[9] == "2.00"
