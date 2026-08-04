"""Recording helpers for the audit trail."""

from audit.models import AccessReveal


def client_ip(request) -> str | None:
    """
    Best-effort client IP.

    X-Forwarded-For is only meaningful behind a proxy you control -- a direct
    client can set it to anything. It is recorded as a hint for a human
    reviewing a flag, never as an access-control input.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def record_reveal(*, request, location, fields_revealed, acknowledged: bool) -> AccessReveal:
    return AccessReveal.objects.create(
        organization=location.organization,
        user=request.user,
        location=location,
        fields_revealed=list(fields_revealed),
        acknowledged=acknowledged,
        ip_address=client_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:512],
    )
