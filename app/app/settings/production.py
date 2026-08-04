"""
Production settings.

Everything here that could be misconfigured fails at import time rather than
at the first request. `manage.py check --deploy --settings=app.settings.production`
should come back clean.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS, SECRET_KEY

DEBUG = False
LOCAL = False

_missing = [
    name
    for name, value in [
        ("SECRET_KEY", SECRET_KEY),
        ("ALLOWED_HOSTS", ALLOWED_HOSTS),
        ("CORS_ALLOWED_ORIGINS", CORS_ALLOWED_ORIGINS),
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

SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365, cast=int)  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
