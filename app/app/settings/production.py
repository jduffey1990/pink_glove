"""
Production settings.

Everything here that could be misconfigured fails at import time rather than
at the first request. `manage.py check --deploy --settings=app.settings.production`
should come back clean.
"""

from email.utils import parseaddr

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import (
    ALLOWED_HOSTS,
    DEFAULT_FROM_EMAIL,
    EMAIL_BACKEND,
    EMAIL_HOST_PASSWORD,
    FIELD_ENCRYPTION_KEY,
    FRONTEND_BASE_URL,
    MEDIA_BUCKET,
    SECRET_KEY,
    STRIPE_CONNECT_WEBHOOK_SECRET,
    STRIPE_ENABLED,
    STRIPE_PLATFORM_PRICE_ID,
    STRIPE_PLATFORM_WEBHOOK_SECRET,
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

# The same for the platform subscription (Phase 4c): a price with no platform
# webhook secret would take subscriptions and never hear one lapse -- a free
# product with a card form in front of it.
if STRIPE_PLATFORM_PRICE_ID and not STRIPE_PLATFORM_WEBHOOK_SECRET:
    raise ImproperlyConfigured(
        "STRIPE_PLATFORM_PRICE_ID is set but STRIPE_PLATFORM_WEBHOOK_SECRET is not. Register "
        "the platform webhook endpoint (docs/DEPLOY.md) and set its signing secret, or unset "
        "the price."
    )
if STRIPE_PLATFORM_PRICE_ID and not STRIPE_ENABLED:
    raise ImproperlyConfigured(
        "STRIPE_PLATFORM_PRICE_ID is set but STRIPE_SECRET_KEY is not. The subscription is "
        "charged through the platform account's key."
    )

# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
# Sign-in codes and invoices leave by email, and mail that is never accepted
# fails without a sound: the owner waits for a code that is not coming. The
# two ways that happens by configuration alone are refused here.

_from_local, _from_at, _from_domain = parseaddr(DEFAULT_FROM_EMAIL)[1].rpartition("@")
_from_domain = _from_domain.lower()
if (
    not (_from_local and _from_at and _from_domain)
    # base.py's default, and reserved (RFC 2606): no receiver accepts it.
    or _from_domain in {"example.com", "example.net", "example.org"}
    or _from_domain.endswith((".example.com", ".example.net", ".example.org"))
):
    raise ImproperlyConfigured(
        f"DEFAULT_FROM_EMAIL={DEFAULT_FROM_EMAIL!r} is not an address mail can be sent from. "
        "Set it to a sender on a domain you have authenticated (docs/DEPLOY.md, 'Email')."
    )

# Only the backend that needs it: a rehearsal or a first staging deploy
# prints mail with the console backend and has no key to give.
if EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend" and not EMAIL_HOST_PASSWORD:
    raise ImproperlyConfigured(
        "EMAIL_BACKEND is SMTP but EMAIL_HOST_PASSWORD is empty, so every sign-in code and "
        "invoice would be refused by the mail server. Set EMAIL_HOST_PASSWORD (the SendGrid "
        "API key), or set EMAIL_BACKEND to the console backend to print mail instead."
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
X_FRAME_OPTIONS = "DENY"

# --------------------------------------------------------------------------
# Error tracking (ADR-029)
# --------------------------------------------------------------------------
# Optional, like Stripe: with no DSN nothing is initialised and `sentry_sdk`
# is never imported. Last in this file on purpose: a deployment that fails
# one of the checks above never gets as far as reporting anywhere. Only this
# module starts it, so development and the test suite cannot send an event.

SENTRY_DSN = config("SENTRY_DSN", default="")  # noqa: F405
if SENTRY_DSN:
    # Staging and production are the same image and the same settings, so
    # nothing here can tell them apart. An event filed under the wrong one is
    # a production error nobody looks at; refuse to guess.
    SENTRY_ENVIRONMENT = config("SENTRY_ENVIRONMENT", default="")  # noqa: F405
    if not SENTRY_ENVIRONMENT:
        raise ImproperlyConfigured(
            "SENTRY_DSN is set but SENTRY_ENVIRONMENT is not. Set it to 'staging' or "
            "'production' (docs/DEPLOY.md, 'Error tracking'), or unset the DSN."
        )
    # Fly puts the image a machine was started from in FLY_IMAGE_REF, which
    # is unique per deploy and is what `fly releases --image` lists. Never
    # left to the SDK: unset, it goes looking for a git checkout.
    SENTRY_RELEASE = (
        config("SENTRY_RELEASE", default="")  # noqa: F405
        or config("FLY_IMAGE_REF", default="")  # noqa: F405
        or "unknown"
    )

    from app import sentry

    sentry.init(dsn=SENTRY_DSN, environment=SENTRY_ENVIRONMENT, release=SENTRY_RELEASE)
