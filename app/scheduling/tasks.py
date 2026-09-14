"""
Background work for the schedule.

Every task takes `organization_id` explicitly (ADR-004). There is no implicit
tenant context in a worker -- nothing resolved a request, so nothing set one,
and a task that guessed would eventually guess wrong.

The fan-out/fan-in shape (a task that iterates organizations and dispatches one
task each) keeps a single slow or broken tenant from blocking the rest.
"""

import logging

from celery import shared_task

from organizations.models import Organization
from scheduling.models import RecurringPlan
from scheduling.services import materialize_plan

logger = logging.getLogger(__name__)


@shared_task(name="scheduling.materialize_all_organizations")
def materialize_all_organizations() -> int:
    """Dispatch a per-organization materialization. Returns how many were queued."""
    organization_ids = Organization.objects.filter(is_active=True).values_list("id", flat=True)

    for organization_id in organization_ids:
        materialize_organization.delay(str(organization_id))

    return len(organization_ids)


@shared_task(name="scheduling.materialize_organization")
def materialize_organization(organization_id: str) -> int:
    """
    Top every active plan in one organization back up to the horizon.

    Returns the number of jobs created. A plan whose pricing inputs are missing
    (a per-sqft service on a location with no square footage) raises; that is
    logged and the remaining plans still run, because one misconfigured plan
    should not cost the whole tenant its schedule.
    """
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        logger.warning("materialize_organization: no active organization %s", organization_id)
        return 0

    plans = RecurringPlan.objects.filter(organization=organization, is_active=True).select_related(
        "organization", "service", "location", "customer"
    )

    created = 0
    for plan in plans:
        try:
            created += materialize_plan(plan)
        except ValueError:
            logger.exception(
                "Could not materialize plan %s for organization %s", plan.pk, organization_id
            )

    return created
