"""
Background work for sign-in.

One task, and it is the recorded exception to "every task takes
`organization_id`" (ADR-004, CLAUDE.md invariant 3; see ADR-034). A password
reset belongs to a person, not to an organization: it happens before any
tenant is resolved, exactly as a 2FA code does, so there is no organization
to pass and no tenant data for a missing one to leak.
"""

from celery import shared_task

from two_factor import services


@shared_task(name="two_factor.send_password_reset", ignore_result=True)
def send_password_reset(email: str) -> None:
    """
    Decide whether `email` gets a reset link, and send it if so.

    The whole decision is made here and not in the request, which is why the
    view enqueues this for every address it is given. Had the view looked the
    account up, a staff address would have cost it a token and an SMTP
    conversation and any other address nothing, and the response time alone
    would say which addresses are staff.

    The link is created in the worker and goes nowhere but the email: the
    broker only ever carries the address somebody typed. Nothing is returned
    and no result is stored, so neither the worker log nor the results table
    records which addresses had accounts.
    """
    issued = services.issue_password_reset(email)
    if issued is not None:
        services.send_password_reset(*issued)
