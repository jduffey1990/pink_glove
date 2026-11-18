"""
Background work for customer notifications.

Every task takes `organization_id` explicitly (invariant 3, ADR-004). The
fan-out is hourly; `notifications.services` decides from each organization's
own clock whether anything is due, so one beat entry serves every timezone
(the shape of `audit.evaluate_access_reveals_all`).
"""

import logging

from celery import shared_task
from django.utils import timezone

from notifications import services
from organizations.models import Organization

logger = logging.getLogger(__name__)


def _remindable_organizations():
    # A deactivated organization is read-only (ADR-028): it books nothing and
    # tells its customers nothing until it is back.
    return Organization.objects.filter(is_active=True, visit_reminders_enabled=True)


@shared_task(name="notifications.send_visit_reminders_all")
def send_visit_reminders_all() -> int:
    """Dispatch one reminder pass per organization that wants them. Returns how many."""
    organization_ids = list(_remindable_organizations().values_list("id", flat=True))
    for organization_id in organization_ids:
        send_visit_reminders.delay(str(organization_id))
    return len(organization_ids)


@shared_task(name="notifications.send_visit_reminders")
def send_visit_reminders(organization_id: str) -> int:
    """
    Send the reminders due now in one organization. Returns how many went.

    Safe to run twice, concurrently or not: each reminder is claimed in the
    ledger before it is sent (ADR-035). The organization is looked up again
    here, because it may have been switched off or deactivated since the
    fan-out queued this.
    """
    organization = _remindable_organizations().filter(pk=organization_id).first()
    if organization is None:
        logger.info("send_visit_reminders: organization %s is not sending", organization_id)
        return 0
    return services.send_due_reminders(organization, timezone.now())
