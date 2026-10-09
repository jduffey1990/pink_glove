"""
The platform subscription's one rule (Phase 4c, ADR-028): is this
organization covered, what the people in it should be told, and when it
becomes read-only.

Pure functions of an organization and a moment, so the serializer, the daily
task and the webhook handler all get the same answer. Nothing here talks to
Stripe -- `billing.subscription` does, and then calls `apply_coverage`;
this module lives in `organizations` because `OrganizationSerializer` has to
publish the result and `organizations` must not import `billing`.

Coverage is a date. `covered_until` is the later of the trial's end and the
end of the last period Stripe was *paid* for; `PLATFORM_GRACE_DAYS` after it
the organization is deactivated, which `base.permissions.IsOrgMember` already
makes read-only. One rule for every lapse: a trial nobody subscribed after,
a renewal whose card failed, a subscription the owner cancelled.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

from base.permissions import ORGANIZATION_INACTIVE
from organizations.enums import BillingState, NoticeLevel
from users.enums import Role

if TYPE_CHECKING:
    from organizations.models import Organization

#: Stripe statuses under which a subscription is still the tenant's to keep:
#: paying, in a Stripe-side trial, or being retried. Anything else
#: (`canceled`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`) means
#: a new one is needed.
LIVE_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due")

#: The trial banner stays quiet until this close to the end. A month of
#: "your trial ends on" is noise; a week of it is a reminder.
TRIAL_NOTICE_DAYS = 7

SUBSCRIBE = "subscribe"
MANAGE = "manage"


def billing_enabled() -> bool:
    return bool(settings.PLATFORM_BILLING_ENABLED)


def has_live_subscription(organization: Organization) -> bool:
    return organization.subscription_status in LIVE_SUBSCRIPTION_STATUSES


def covered_until(organization: Organization) -> dt.datetime | None:
    """
    When this organization's coverage ends. `None` means indefinitely:
    comped by hand, or platform billing is not configured on the server.

    An organization with neither a trial nor a paid period -- one created
    exempt and later un-exempted -- has been uncovered since it existed.
    """
    if organization.billing_exempt or not billing_enabled():
        return None
    ends = [
        when
        for when in (organization.trial_ends_at, organization.subscription_paid_through)
        if when is not None
    ]
    if not ends:
        return organization.created_at or timezone.now()
    return max(ends)


def grace_ends_at(organization: Organization) -> dt.datetime | None:
    until = covered_until(organization)
    if until is None:
        return None
    return until + dt.timedelta(days=settings.PLATFORM_GRACE_DAYS)


def _covered_by_trial(organization: Organization) -> bool:
    """Is the trial what the coverage comes from, rather than a paid period?"""
    trial, paid = organization.trial_ends_at, organization.subscription_paid_through
    if trial is None:
        return False
    return paid is None or trial >= paid


def billing_state(organization: Organization, now: dt.datetime | None = None) -> str:
    now = now or timezone.now()
    if not organization.is_active:
        return BillingState.DEACTIVATED

    until = covered_until(organization)
    if until is None:
        return BillingState.EXEMPT
    if organization.subscription_status == "past_due":
        # Covered or not: a failed card is the thing to say, and the thing to fix.
        return BillingState.PAST_DUE
    if until > now:
        if organization.subscription_status in ("active", "trialing"):
            return BillingState.ACTIVE
        if _covered_by_trial(organization):
            return BillingState.TRIAL
        # Paid through a future date on a subscription Stripe no longer
        # considers live: cancelled, but what was paid for is still theirs.
        return BillingState.ACTIVE
    return BillingState.LAPSED


def available_actions(organization: Organization) -> list[str]:
    """
    What the subscription endpoints will accept right now, for the page to
    draw its buttons from (ADR-023). Who may press them is the permission
    layer's business, as it is for Connect.
    """
    if not billing_enabled() or organization.billing_exempt:
        return []
    actions = []
    if not has_live_subscription(organization):
        actions.append(SUBSCRIBE)
    if organization.stripe_customer_id:
        actions.append(MANAGE)
    return actions


def _local_date(organization: Organization, when: dt.datetime) -> str:
    """'October 9, 2026', on the organization's own calendar."""
    local = when.astimezone(organization.tz)
    return f"{local:%B} {local.day}, {local.year}"


def billing_notice(
    organization: Organization, role: str | None, now: dt.datetime | None = None
) -> tuple[str, str] | None:
    """
    The banner's `(level, sentence)`, or `None` when there is nothing to say.

    The server writes the sentence (ADR-023), and writes it for the reader:
    the owner is told what to do, everyone else is told to ask the owner,
    because telling a cleaner to update a card they cannot see is noise.
    """
    now = now or timezone.now()
    state = billing_state(organization, now)
    owner = role == Role.OWNER
    until = covered_until(organization)
    grace = grace_ends_at(organization)

    if state == BillingState.EXEMPT:
        return None

    if state == BillingState.DEACTIVATED:
        if organization.deactivated_for_billing_at is None:
            # Switched off by a person, for a reason billing does not know.
            return NoticeLevel.ERROR, ORGANIZATION_INACTIVE
        what = (
            f"its free trial ended on {_local_date(organization, until)} and no subscription "
            "was started"
            if _covered_by_trial(organization)
            else f"its subscription lapsed on {_local_date(organization, until)}"
        )
        fix = (
            "Subscribe to restore access."
            if owner
            else "Ask the organization owner to subscribe to restore access."
        )
        return NoticeLevel.ERROR, f"This organization is read-only because {what}. {fix}"

    if state == BillingState.TRIAL:
        if until - now > dt.timedelta(days=TRIAL_NOTICE_DAYS):
            return None
        fix = (
            "Subscribe to keep scheduling after that."
            if owner
            else "Ask the organization owner to subscribe to keep scheduling after that."
        )
        return (
            NoticeLevel.WARNING,
            f"Your free trial ends on {_local_date(organization, until)}. {fix}",
        )

    if state == BillingState.ACTIVE:
        if organization.subscription_cancel_at_period_end or not has_live_subscription(
            organization
        ):
            return (
                NoticeLevel.INFO,
                f"Your subscription ends on {_local_date(organization, until)}.",
            )
        return None

    if state == BillingState.PAST_DUE:
        fix = (
            f"Update your card by {_local_date(organization, grace)} to keep scheduling."
            if owner
            else "Ask the organization owner to update the card by "
            f"{_local_date(organization, grace)} to keep scheduling."
        )
        return NoticeLevel.WARNING, f"A payment for your subscription failed. {fix}"

    # LAPSED: coverage has ended and the grace is running (or has just run out,
    # ahead of the daily task).
    ended = (
        f"Your free trial ended on {_local_date(organization, until)}."
        if _covered_by_trial(organization)
        else f"Your subscription ended on {_local_date(organization, until)}."
    )
    fix = (
        f"Subscribe by {_local_date(organization, grace)} to keep scheduling."
        if owner
        else "Ask the organization owner to subscribe by "
        f"{_local_date(organization, grace)} to keep scheduling."
    )
    return NoticeLevel.WARNING, f"{ended} {fix}"


def apply_coverage(organization: Organization, now: dt.datetime | None = None) -> bool:
    """
    The one place `is_active` moves for billing. Returns whether it did.

    Past the grace and still active: deactivate, and stamp why. Covered again
    and deactivated *for billing*: reactivate, and clear the stamp. An
    organization that is inactive with no stamp was switched off by a person
    and is never touched -- whatever Stripe says.
    """
    now = now or timezone.now()
    until = covered_until(organization)
    grace = grace_ends_at(organization)

    if organization.is_active:
        if grace is not None and now >= grace:
            organization.is_active = False
            organization.deactivated_for_billing_at = now
            organization.save(
                update_fields=["is_active", "deactivated_for_billing_at", "updated_at"]
            )
            return True
        return False

    if organization.deactivated_for_billing_at is None:
        return False
    if until is None or until > now:
        organization.is_active = True
        organization.deactivated_for_billing_at = None
        organization.save(update_fields=["is_active", "deactivated_for_billing_at", "updated_at"])
        return True
    return False
