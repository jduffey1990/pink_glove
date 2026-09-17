"""
Production settings.

Everything here that could be misconfigured fails at import time rather than
at the first request. `manage.py check --deploy --settings=app.settings.production`
should come back clean.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, FIELD_ENCRYPTION_KEY, SECRET_KEY

DEBUG = False
LOCAL = False

_missing = [
    name
    for name, value in [
        ("SECRET_KEY", SECRET_KEY),
        ("ALLOWED_HOSTS", ALLOWED_HOSTS),
        ("CORS_ALLOWED_ORIGINS", CORS_ALLOWED_ORIGINS),
        # Without this, every read of an encrypted column raises at runtime
        # rather than at startup. Fail now instead.
        ("FIELD_ENCRYPTION_KEY", FIELD_ENCRYPTION_KEY),
    ]
    if not value
]
if _missing:
    raise ImproperlyConfigured(
        f"Missing required production environment variables: {', '.join(_missing)}"
    )

if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

# --------------------------------------------------------------------------
# Transport security
# --------------------------------------------------------------------------
# SECURE_SSL_REDIRECT is off by default because most managed platforms already
# redirect at the edge and doubling it causes loops. Turn on for a bare VPS.

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)  # noqa: F405

# The same assumption as SECURE_PROXY_SSL_HEADER above: exactly one proxy we
# control in front of the app, so the last X-Forwarded-For entry is the one it
# wrote. Set NUM_PROXIES to the real depth if the deploy target differs (a CDN
# in front of a load balancer is 2) -- too low and every client shares the
# proxy's bucket, too high and the header is spoofable again.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "NUM_PROXIES": config("NUM_PROXIES", default=1, cast=int),  # noqa: F405
}

SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365, cast=int)  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
