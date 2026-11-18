"""
Error tracking (ADR-029): unhandled exceptions and ERROR log lines go to
Sentry, so a 500 during the working day reaches a person instead of scrolling
past in a log nobody is reading.

The shape is Stripe's (`billing.connect`): optional as a whole, off unless
`SENTRY_DSN` is set, and `sentry_sdk` is imported only inside `init()`, which
only `settings/production.py` calls. Development and the test suite cannot
send anything, whatever is in a developer's `.env`.

What may leave is decided twice, on purpose. The SDK is configured not to
collect the dangerous parts at all (no request bodies, no local variables, no
cookies, no user). `before_send` then removes them again and redacts by name,
so one option flipped later, or an SDK upgrade that changes a default, does
not put a gate code on someone else's server. ADR-029 lists exactly what an
event still carries.

What this cannot do: an exception message or a log line is sent as written.
A code interpolated into either leaves with it. "Nothing sensitive is logged"
(CLAUDE.md, phase gate 5) is the rule that covers that, not this module.
"""

import re

FILTERED = "[Filtered]"

# Matched against a key broken into lowercase words, so `gateCode`,
# `GATE-CODE` and `gate_code` are one name. Words, not substrings, for the
# short ones: "gate" is also inside "navigate" and "key" inside "monkey".
_SENSITIVE_WORDS = frozenset(
    {
        "access",
        "alarm",
        "auth",
        "code",
        "codes",
        "dsn",
        "gate",
        "key",
        "keys",
        "otp",
        "pin",
        # Who was asking, not a credential: `REMOTE_ADDR`, `X-Forwarded-For`,
        # and `Fly-Client-IP`, which the SDK's own header filter does not know.
        "addr",
        "forwarded",
        "ip",
    }
)
# Distinctive enough to match anywhere in the name: `csrftoken`, `sessionid`,
# `X-CSRFToken`, `set-cookie`, `Stripe-Signature`, `EMAIL_HOST_PASSWORD`.
_SENSITIVE_STEMS = re.compile(
    r"token|passw|passcode|secret|cookie|session|csrf|authori[sz]|credential|signature"
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NOT_A_WORD = re.compile(r"[^a-z0-9]+")

# An identifier here is a UUID (ADR-010), 36 characters. A longer path segment
# is a bearer credential travelling in a URL: the pay link's signed token
# (`/pay/<token>`), a customer's magic link (`/sign-in/<token>`). The same
# URLs turn up in the Referer header and in "Not Found: /api/..." log lines,
# so the rule is applied to the string wherever it sits, not to one field.
_LONG_PATH_SEGMENT = re.compile(r"(?<=/)[A-Za-z0-9_\-:.~%=+]{41,}(?=$|[/?#\s\"'<>)\],;])")
# A `name=value` pair after `?`, `&` or `;`, and after `#`: the emailed links
# carry their token in the fragment (`/pay#token=...`, ADR-036), which no
# request brings here but a built link in a log line or an exception can.
_QUERY_PAIR = re.compile(r"(?<=[?&;#])([^=&#;\s]+)=([^&#;\s]*)")

# Where a URL can appear as part of a string. Stack frames are left out: a
# file path with a long migration name in it is not a credential, and it is
# the line someone needs to read.
_SUBTREES_WITH_URLS = frozenset(
    {"request", "breadcrumbs", "exception", "logentry", "message", "extra", "threads"}
)


def is_sensitive(key) -> bool:
    """Whether a value stored under this name must never leave."""
    name = _CAMEL_BOUNDARY.sub("_", str(key)).lower()
    if _SENSITIVE_STEMS.search(name):
        return True
    words = [word for word in _NOT_A_WORD.split(name) if word]
    # The one `*_code` an event is useless without, and not a secret.
    if words[-2:] == ["status", "code"]:
        words = words[:-2]
    return not _SENSITIVE_WORDS.isdisjoint(words)


def scrub_url(text: str) -> str:
    """Redact credentials carried in a URL: named query values, token paths."""

    def _pair(match: re.Match) -> str:
        name, value = match.group(1), match.group(2)
        return f"{name}={FILTERED}" if value and is_sensitive(name) else match.group(0)

    return _LONG_PATH_SEGMENT.sub(FILTERED, _QUERY_PAIR.sub(_pair, text))


def _scrub(value, *, urls: bool):
    if isinstance(value, dict):
        for key in list(value):
            if key == "vars":
                # Local variables. A local called `code` or `value` holds a
                # gate code as readily as one called `gate_code`, so no name
                # rule is good enough: they go, all of them.
                del value[key]
            elif is_sensitive(key):
                if value[key] not in (None, "", [], {}):
                    value[key] = FILTERED
            elif key == "query_string" and isinstance(value[key], str):
                value[key] = scrub_url("?" + value[key])[1:]
            else:
                value[key] = _scrub(value[key], urls=urls and key != "stacktrace")
        return value
    if isinstance(value, list | tuple):
        return [_scrub(item, urls=urls) for item in value]
    if isinstance(value, str) and urls:
        return scrub_url(value)
    return value


def before_send(event, hint=None):
    """
    The last thing an event passes through before it leaves.

    Removes what must not be there whatever the SDK was told (the request
    body, cookies, local variables, the user), then redacts every value
    stored under a sensitive name, wherever in the event it is. If this
    raises, the SDK drops the event: it fails closed.
    """
    request = event.get("request")
    if isinstance(request, dict):
        # The body of a location update IS the gate code.
        request.pop("data", None)
        request.pop("cookies", None)

    # Who it happened to is not sent, not even as an id (ADR-029).
    event.pop("user", None)

    for key in list(event):
        # `_meta` is the SDK's own notes on what it trimmed, keyed like the
        # event but holding no values. Rewriting it would only corrupt it.
        if key != "_meta":
            event[key] = _scrub(event[key], urls=key in _SUBTREES_WITH_URLS)
    return event


def init(*, dsn: str, environment: str, release: str, transport=None) -> None:
    """
    Start reporting. Called once, from `settings/production.py`, and only
    when a DSN is configured. `transport` exists for the tests, which hand in
    one that keeps the envelopes instead of sending them.
    """
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import ignore_logger

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        transport=transport,
        before_send=before_send,
        # Named rather than discovered: with auto-enabling on, installing a
        # package (redis, boto3, httpx) silently changes what is recorded.
        integrations=[DjangoIntegration(), CeleryIntegration()],
        auto_enabling_integrations=False,
        # No user, no IP address, no cookies, no Authorization header, no
        # Celery task arguments, no SQL parameters.
        send_default_pii=False,
        # A location update's body is the gate code; a sign-in's is the
        # password. Never read, so never sent.
        max_request_body_size="never",
        include_local_variables=False,
        # Errors only. No performance traces, profiles, logs, metrics,
        # release-health sessions or client reports: an error event is the
        # one thing that leaves, and `before_send` sees every one of them.
        traces_sample_rate=None,
        profiles_sample_rate=None,
        enable_logs=False,
        enable_metrics=False,
        auto_session_tracking=False,
        send_client_reports=False,
        # Otherwise every outgoing HTTP call (Stripe, the media bucket) is
        # given `sentry-trace` and `baggage` headers naming this project.
        trace_propagation_targets=[],
    )

    # One line per request, each carrying its URL, kept as breadcrumbs on the
    # worker thread and attached to whatever fails there next.
    ignore_logger("gunicorn.access")
    # Logged at ERROR for every scanner that sends a made-up Host header.
    # Not ours to fix, and enough of them drowns the errors that are.
    ignore_logger("django.security.DisallowedHost")
