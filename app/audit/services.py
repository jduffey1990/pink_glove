"""Recording helpers for the audit trail."""

import ipaddress

from django.core.exceptions import ValidationError
from rest_framework.settings import api_settings

from audit.models import AccessReveal
from users.enums import DISPATCHER_ROLES


def client_ip(request) -> str | None:
    """
    Best-effort client IP, read the same way the throttles read it.

    X-Forwarded-For is only meaningful behind a proxy you control -- a direct
    client can set it to anything. So with `NUM_PROXIES` at 0 it is ignored,
    and otherwise the entry that many from the end is the one our own proxy
    wrote. It is recorded as a hint for a human reviewing a flag, never as an
    access-control input.

    Anything that does not parse as an address is dropped rather than stored:
    the column is an inet, and a junk header would otherwise turn a reveal
    into a 500.
    """
    num_proxies = api_settings.NUM_PROXIES or 0
    candidate = request.META.get("REMOTE_ADDR")

    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    if num_proxies > 0 and forwarded != [""]:
        candidate = forwarded[-min(num_proxies, len(forwarded))]

    try:
        return str(ipaddress.ip_address(candidate))
    except (TypeError, ValueError):
        return None


def resolve_reveal_job(*, request, location, job_id=None):
    """
    Work out which visit a reveal belongs to.

    The binding matters because the end-of-day evaluator judges a reveal
    against its job's window. A wrong job would produce a confident, wrong
    verdict; null produces an honest, looser one.

    By tier (ADR-017):

    * **Cleaner** -- the job that justified letting them through at all. Where
      several qualify (a morning and an evening visit at the same address), the
      one starting nearest now. A cleaner cannot reach this code without one:
      `IsAssignedCleaner` has already refused them otherwise.
    * **Dispatcher and above** -- the job they named, after checking it is
      really at this location in this organization. If they named none, the
      single non-terminal job at this location inside the window, and only if
      there is exactly one. Otherwise null.
    """
    from scheduling.models import Job
    from scheduling.permissions import nearest_job_for_location, sole_open_job_for_location

    organization = location.organization
    membership = getattr(request, "membership", None)
    is_dispatcher = (membership is not None and membership.role in DISPATCHER_ROLES) or (
        request.user.is_superuser
    )

    if not is_dispatcher:
        return nearest_job_for_location(
            user=request.user, location=location, organization=organization
        )

    if job_id:
        job = Job.objects.filter(pk=job_id, organization=organization, location=location).first()
        if job is None:
            # 400 rather than a silent null: they asked for a specific visit,
            # and binding the record to a different one would be worse than
            # refusing.
            raise ValidationError({"job": "That job is not at this location."})
        return job

    return sole_open_job_for_location(location=location, organization=organization)


def record_reveal(*, request, location, fields_revealed, acknowledged: bool, job=None):
    return AccessReveal.objects.create(
        organization=location.organization,
        user=request.user,
        location=location,
        job=job,
        fields_revealed=list(fields_revealed),
        acknowledged=acknowledged,
        ip_address=client_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:512],
    )
