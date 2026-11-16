"""
The timesheet: what each person worked in a period.

Read-only, and built from `TimeEntry` alone. Four rules, each decided in
docs/DECISIONS.md ADR-030 and each easy to "improve" wrongly:

* **An entry belongs to the organization-local day it was clocked in on**, and
  all of it belongs there. A shift from 22:00 to 02:00 is four hours on the
  day it started; nothing is split at midnight or at the edge of the period.
  So every entry sits in exactly one day and one period, and two periods laid
  end to end add up to the whole.
* **Seconds are elapsed time**, the difference of two UTC instants. On the
  night the clocks go back, 00:30 to 02:30 on the wall is three hours worked
  and is reported as three.
* **An open entry is listed and flagged, and counts for nothing.** Someone
  still clocked in has not worked a knowable number of hours; `clock_out` and
  `seconds` are null rather than zero, which would read as worked.
* **A correction is the entry.** A dispatcher's PATCH rewrites `clock_in` and
  `clock_out` in place and the model keeps no earlier version, so the report
  reads the row as it stands.

Soft deletes follow the foreign keys (CLAUDE.md gate item 5: the manager only
covers the model being queried). `TimeEntry.job` cascades, so an entry does
not outlive a deleted job and the join filters it out. `Job.customer`
protects, so removing a customer from the address book does not unwork the
hours: the entry stays, name and all.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from scheduling.models import TimeEntry


def worked_seconds(entry: TimeEntry) -> int | None:
    """
    Whole seconds between clock-in and clock-out. None while the entry is open.

    Truncated per entry, and totals are sums of these, so a total is always
    exactly the sum of the rows printed under it.
    """
    if entry.clock_out is None:
        return None
    return int((entry.clock_out - entry.clock_in).total_seconds())


def timesheet(organization, *, date_from: dt.date, date_to: dt.date) -> dict:
    """
    Hours per staff member for [date_from, date_to], both organization-local
    and inclusive.

    One query, whatever the number of people or customers. Ordered by name
    and then id for staff, clock-in and then id for entries, so the same data
    always produces the same bytes.
    """
    start, end = organization.local_day_bounds(date_from, date_to)
    tz = organization.tz

    entries = (
        TimeEntry.objects.filter(
            organization=organization,
            clock_in__gte=start,
            clock_in__lt=end,
            # The manager filters `TimeEntry` and nothing it joins to.
            job__deleted_at__isnull=True,
        )
        .select_related("user", "job__customer")
        .order_by("clock_in", "pk")
    )

    staff: dict = {}
    for entry in entries:
        seconds = worked_seconds(entry)
        is_open = seconds is None

        person = staff.setdefault(
            entry.user_id,
            {
                "user": entry.user_id,
                "name": entry.user.full_name,
                "total_seconds": 0,
                "entry_count": 0,
                "open_entry_count": 0,
                "entries": [],
            },
        )
        person["entry_count"] += 1
        person["open_entry_count"] += int(is_open)
        person["total_seconds"] += seconds or 0
        person["entries"].append(
            {
                "id": entry.pk,
                "job": entry.job_id,
                "customer_name": entry.job.customer.display_name,
                "local_date": entry.clock_in.astimezone(tz).date(),
                "clock_in": entry.clock_in,
                "clock_out": entry.clock_out,
                "seconds": seconds,
                "is_open": is_open,
            }
        )

    people = sorted(staff.values(), key=lambda p: (p["name"].casefold(), str(p["user"])))

    return {
        "date_from": date_from,
        "date_to": date_to,
        "timezone": organization.timezone,
        "total_seconds": sum(p["total_seconds"] for p in people),
        "entry_count": sum(p["entry_count"] for p in people),
        "open_entry_count": sum(p["open_entry_count"] for p in people),
        "staff": people,
    }


TIMESHEET_CSV_HEADER = (
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
)


def _hours(seconds: int) -> Decimal:
    """Seconds as decimal hours to two places, half up: 5400 is 1.50."""
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(instant: dt.datetime | None) -> str:
    return "" if instant is None else instant.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(instant: dt.datetime | None, tz) -> str:
    """Wall-clock time with its offset, so 01:30 on the night the clocks go
    back still says which 01:30 it was."""
    return "" if instant is None else instant.astimezone(tz).isoformat(timespec="seconds")


def timesheet_csv_rows(report: dict, tz) -> list[tuple]:
    """
    One row per entry, in the report's own order.

    No totals row: a spreadsheet sums a column, and a total among the rows is
    counted twice by the first person who does. An open entry has empty
    `seconds` and `hours_decimal`, not zero.
    """
    rows = []
    for person in report["staff"]:
        for entry in person["entries"]:
            seconds = entry["seconds"]
            rows.append(
                (
                    person["name"],
                    entry["local_date"].isoformat(),
                    entry["customer_name"],
                    str(entry["job"]),
                    _utc(entry["clock_in"]),
                    _utc(entry["clock_out"]),
                    _local(entry["clock_in"], tz),
                    _local(entry["clock_out"], tz),
                    seconds,
                    None if seconds is None else _hours(seconds),
                    "open" if entry["is_open"] else "closed",
                )
            )
    return rows
