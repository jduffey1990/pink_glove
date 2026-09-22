"""
Production settings.

Everything here that could be misconfigured fails at import time rather than
at the first request. `manage.py check --deploy --settings=app.settings.production`
should come back clean.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import (
    ALLOWED_HOSTS,
    FIELD_ENCRYPTION_KEY,
    FRONTEND_BASE_URL,
    MEDIA_BUCKET,
    SECRET_KEY,
    STRIPE_CONNECT_WEBHOOK_SECRET,
    STRIPE_ENABLED,
    UI_DIST_DIR,
    config,
)

DEBUG = False
LOCAL = False

_missing = [
    name
    for name, value in [
        ("SECRET_KEY", SECRET_KEY),
        ("ALLOWED_HOSTS", ALLOWED_HOSTS),
        # CORS_ALLOWED_ORIGINS is not in this list any more: the deployed shape
        # is single-origin (ADR-027), where nothing cross-origin calls the API
        # and the cookies stay SameSite=Lax. Setting it is how a separately
        # hosted frontend opts back in.
        #
        # Without this, every read of an encrypted column raises at runtime
        # rather than at startup. Fail now instead.
        ("FIELD_ENCRYPTION_KEY", FIELD_ENCRYPTION_KEY),
        # Sign-in links are built from it; the base default is the dev server.
        ("FRONTEND_BASE_URL", config("FRONTEND_BASE_URL", default="")),
    ]
    if not value
]
if _missing:
    raise ImproperlyConfigured(
        f"Missing required production environment variables: {', '.join(_missing)}"
    )

if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

# A customer's sign-in token travels in this URL, in an email.
if not FRONTEND_BASE_URL.startswith("https://"):
    raise ImproperlyConfigured("FRONTEND_BASE_URL must be an https:// URL in production.")

# Stripe is optional (ADR-024), but half of it is not: a key with no webhook
# secret would open Checkout to customers and never hear that they paid.
if STRIPE_ENABLED and not STRIPE_CONNECT_WEBHOOK_SECRET:
    raise ImproperlyConfigured(
        "STRIPE_SECRET_KEY is set but STRIPE_CONNECT_WEBHOOK_SECRET is not. Register the "
        "Connect webhook endpoint (docs/DEPLOY.md) and set its signing secret, or unset the key."
    )

# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------
# Photographs of the inside of people's homes, so where they land is checked
# at startup rather than discovered on the first upload.

_media_backend = config("MEDIA_BACKEND", default="filesystem")
if _media_backend == "filesystem":
    # Nothing serves /media/ outside DEBUG (urls.py), so uploads would land
    # on a disk the next deploy throws away and be unreadable even before
    # that. Private, signed object storage is the only deployed shape.
    raise ImproperlyConfigured(
        "MEDIA_BACKEND=filesystem is development only. Set MEDIA_BACKEND to s3 or gcs "
        "and MEDIA_BUCKET to the bucket."
    )
if not MEDIA_BUCKET:
    raise ImproperlyConfigured(
        f"MEDIA_BACKEND={_media_backend} needs a bucket: set MEDIA_BUCKET (or BUCKET_NAME)."
    )

# The build is served from this process (ADR-027). Refusing to start without
# it is what turns "the image was built from the wrong directory" into a
# deploy failure rather than a blank page. A separately hosted frontend sets
# UI_DIST_DIR to an explicit empty value to say so.
if not UI_DIST_DIR and config("UI_DIST_DIR", default=None) != "":
    raise ImproperlyConfigured(
        "The frontend build was not found (UI_DIST_DIR). Build the image from the "
        "repository root, or set UI_DIST_DIR= (empty) if the frontend is hosted elsewhere."
    )

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
# Right for an app on its own subdomain, which is what docs/DEPLOY.md sets up.
# On an apex domain these commit every subdomain to https and invite an
# irreversible preload submission, so they can be switched off.
SECURE_HSTS_INCLUDE_SUBDOMAINS = config("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True, cast=bool)  # noqa: F405
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=True, cast=bool)  # noqa: F405

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
