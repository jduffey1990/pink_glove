"""
Base settings shared by every environment.

Environment-specific modules (local / test / production) import * from here and
override. Nothing in this file may contain a secret or an environment-specific
hostname -- those come from the environment. See .env.example.
"""

from datetime import timedelta
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from decouple import Csv, config

# app/app/settings/base.py -> app/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

# Empty by default so local/test can substitute a throwaway value.
# production.py raises ImproperlyConfigured if this is still empty.
SECRET_KEY = config("SECRET_KEY", default="")

DEBUG = False

ENV = config("ENV", default="local")

#: Only ever True in app.settings.local. Gates development-only affordances
#: such as returning a 2FA code in the API response.
LOCAL = False

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="", cast=Csv())

#: Fernet key for base.fields.EncryptedTextField (gate codes, alarm codes).
#: Separate from SECRET_KEY on purpose: rotating SECRET_KEY only invalidates
#: sessions, whereas rotating this one makes existing ciphertext unreadable.
#: local/test substitute a throwaway value; production.py requires a real one.
FIELD_ENCRYPTION_KEY = config("FIELD_ENCRYPTION_KEY", default="")

ROOT_URLCONF = "app.urls"
WSGI_APPLICATION = "app.wsgi.application"
ASGI_APPLICATION = "app.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "users.CustomUser"

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    # Not django.contrib.admin: the default site is swapped for one that only
    # admits a 2FA-verified session. See app/admin.py.
    "app.admin.PinkGloveAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    "django_filters",
    "django_extensions",
    "django_celery_results",
    "django_celery_beat",
    "drf_spectacular",
]

LOCAL_APPS = [
    "base",
    "organizations",
    "users",
    "two_factor",
    "customers",
    "catalog",
    "audit",
    "scheduling",
    "billing",
    "health",
    # Phase 5 adds: notifications
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Above whitenoise on purpose: whitenoise answers a file without running
    # any middleware below it, and the SPA's files are served that way since
    # ADR-027. It is pure process_response, so nothing else moves.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Whitenoise, taught the shape of Vite's hashed asset names (ADR-027).
    "app.middleware.static_files.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow AuthenticationMiddleware: it resolves the tenant from
    # request.user's memberships.
    "app.middleware.tenant.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# One DATABASE_URL rather than five separate vars, so any managed Postgres
# (Fly, Render, Railway, Cloud SQL, a plain VPS) works without a code change.

DATABASES = {
    "default": dj_database_url.config(
        default=config(
            "DATABASE_URL",
            default="postgres://pink_glove:pink_glove@db:5432/pink_glove",
        ),
        conn_max_age=config("DB_CONN_MAX_AGE", default=60, cast=int),
        conn_health_checks=True,
    )
}
DATABASES["default"].setdefault("OPTIONS", {})["connect_timeout"] = 5

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------
# Deny by default. A view that should be public opts in with AllowAny
# explicitly, so a forgotten permission_classes fails closed rather than open.

REST_FRAMEWORK = {
    # Renders Django's ValidationError as a 400 rather than letting it escape
    # as a 500. See app/exceptions.py.
    "EXCEPTION_HANDLER": "app.exceptions.exception_handler",
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    # ADR-019: the schema is the frontend's contract, generated from the
    # viewsets rather than hand-maintained.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        # Phase 1: consumed by the two_factor views. Keyed on client IP.
        "two_factor_issue": "5/hour",
        "two_factor_verify": "10/hour",
        "magic_link": "5/hour",
        # Keyed on the address being signed in to, whatever IP asks -- the IP
        # buckets above do nothing against a guesser who rotates addresses.
        # Looser than they are because it also counts the owner's own typos.
        "login_account": "20/hour",
    },
    # How many proxies sit in front of the app, which is how DRF decides which
    # entry of X-Forwarded-For to believe. Left unset, DRF keys throttles on the
    # raw header, and a client that varies it gets a fresh bucket per request.
    # 0 means "trust REMOTE_ADDR only". production.py defaults it to 1.
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
}

# --------------------------------------------------------------------------
# OpenAPI schema
# --------------------------------------------------------------------------
# See docs/DECISIONS.md ADR-019. `ui/src/api/schema.d.ts` is generated from
# this and committed, so an API change is visible in the diff that caused it.
# A test asserts the schema generates with zero warnings -- an action
# spectacular cannot describe is an action the frontend cannot call.

SPECTACULAR_SETTINGS = {
    "TITLE": "pink_glove API",
    "DESCRIPTION": "CRM, scheduling, and billing for cleaning companies.",
    "VERSION": "0.1.0",
    # The schema endpoint keeps the global IsAuthenticated default; there is no
    # reason to publish our full surface area to anonymous callers.
    # SERVE_PERMISSIONS is spelled out because spectacular's own default is
    # AllowAny -- it does not inherit DEFAULT_PERMISSION_CLASSES.
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api",
    # Role is reached both through Membership.role and through the session's
    # `current_role`. Without this the generator names the same choice set
    # twice and warns; the schema test treats that warning as a failure.
    "ENUM_NAME_OVERRIDES": {
        "RoleEnum": "users.enums.Role.choices",
        # Two different choice sets are both exposed as a field called
        # "status". Left alone the generator invents names like
        # "StatusAccEnum", which the frontend then has to guess at.
        "JobStatusEnum": "scheduling.enums.JobStatus.choices",
        "CustomerStatusEnum": "customers.enums.CustomerStatus.choices",
        "InvoiceStatusEnum": "billing.enums.InvoiceStatus.choices",
        # Left alone these generate as "MethodEnum" and "KindEnum" -- generic
        # enough that the frontend would have to guess what they belong to.
        "PaymentMethodEnum": "billing.enums.PaymentMethod.choices",
        "LineKindEnum": "billing.enums.LineKind.choices",
    },
}

# --------------------------------------------------------------------------
# Sessions, CSRF, CORS
# --------------------------------------------------------------------------
# The SPA talks to this API cross-origin with credentials, so these have to
# agree with the frontend's deployed origin. All env-driven -- no hardcoded
# company domain anywhere in source.

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

_cookie_domain = config("COOKIE_DOMAIN", default="")
CSRF_COOKIE_DOMAIN = _cookie_domain or None
SESSION_COOKIE_DOMAIN = _cookie_domain or None

CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
# SameSite=None is what a cross-origin SPA needs, and it is also the setting
# that lets any site the browser visits carry our cookie on a request here. The
# deployed shape is single-origin (ADR-027): the SPA is served by this process,
# so there is no cross-origin caller and the cookie can stay Lax. Only when an
# origin is actually allowed to call with credentials does the cookie loosen to
# match -- one setting decides both, so they cannot disagree.
CSRF_COOKIE_SAMESITE = "None" if CORS_ALLOWED_ORIGINS else "Lax"
SESSION_COOKIE_SAMESITE = CSRF_COOKIE_SAMESITE
SESSION_COOKIE_HTTPONLY = True
# The SPA reads the CSRF token from its cookie, so this one cannot be HttpOnly.
CSRF_COOKIE_HTTPONLY = False

SESSION_COOKIE_AGE = config("SESSION_COOKIE_AGE", default=60 * 60 * 12, cast=int)

# --------------------------------------------------------------------------
# Internationalization
# --------------------------------------------------------------------------
# Storage is always UTC. Display and recurrence expansion happen in the
# organization's own timezone (Organization.timezone, Phase 1) -- a weekly
# 9am job must stay 9am local across a DST boundary.

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static and media
# --------------------------------------------------------------------------
# Whitenoise serves static from the container, so no bucket is required to
# deploy. Media storage is env-selected; see docs/DECISIONS.md ADR-027.

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
# Only read with MEDIA_BACKEND=filesystem, which is development only:
# production.py refuses it, because nothing serves /media/ outside DEBUG and
# the container's disk is thrown away on every deploy anyway.
MEDIA_ROOT = BASE_DIR / "media"

# The built frontend (ADR-027). Vite's output directory: `index.html` plus
# `assets/` full of content-hashed files. Whitenoise serves the files from
# here at the root URL; app.spa answers every SPA route with the index. Empty
# means "not served" -- the Vite dev server is the frontend in development,
# and the compose file sets it empty so a stale build baked into the image
# does not sit beside the live one. The image copies the build to /ui/dist,
# which is also where a host checkout's `ui/dist` lands relative to app/.
UI_DIST_DIR = config("UI_DIST_DIR", default=str(BASE_DIR.parent / "ui" / "dist"))
if UI_DIST_DIR and not Path(UI_DIST_DIR).is_dir():
    UI_DIST_DIR = ""

# Whitenoise serves the SPA's files at the root URL (/assets/..., /favicon.ico)
# alongside Django's own static under /static/. Vite names every file under
# assets/ with a content hash, so those may be cached forever -- the middleware
# subclass in app.middleware.static_files knows that shape. Everything else
# (index.html, favicon) keeps whitenoise's short default.
WHITENOISE_ROOT = UI_DIST_DIR or None

_media_backend = config("MEDIA_BACKEND", default="filesystem")

# One bucket name for either cloud backend. `fly storage create` (Tigris) sets
# BUCKET_NAME on the app, so that is the fallback; credentials and, for S3,
# the endpoint and region follow the AWS SDK's own environment variables
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3, AWS_REGION),
# which boto3 reads on its own. They are passed through explicitly all the same
# so a signed URL is built from the same values the upload used.
MEDIA_BUCKET = config("MEDIA_BUCKET", default="") or config("BUCKET_NAME", default="")

# Media here is photographs of the inside of people's homes. Whatever the
# bucket's own policy says, objects are written private and read only through
# a signed URL that expires in minutes -- so a misconfigured bucket, or a URL
# pasted into a chat, does not publish someone's living room. Names are never
# overwritten: a job photo is evidence, and a second upload must not replace it.
MEDIA_URL_TTL_SECONDS = config("MEDIA_URL_TTL_SECONDS", default=300, cast=int)

_MEDIA_BACKENDS = {
    "filesystem": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "s3": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": MEDIA_BUCKET,
            # None lets boto3 fall back to its own environment and config;
            # Tigris and MinIO need the explicit endpoint, AWS itself does not.
            "endpoint_url": config("AWS_ENDPOINT_URL_S3", default=None),
            "region_name": config("AWS_REGION", default=None),
            "access_key": config("AWS_ACCESS_KEY_ID", default=None),
            "secret_key": config("AWS_SECRET_ACCESS_KEY", default=None),
            # Tigris and MinIO sign path-style URLs; virtual-hosted style
            # is the AWS default and fails on both.
            "addressing_style": config("AWS_S3_ADDRESSING_STYLE", default="path"),
            "signature_version": "s3v4",
            "default_acl": "private",
            "querystring_auth": True,
            "querystring_expire": MEDIA_URL_TTL_SECONDS,
            "file_overwrite": False,
        },
    },
    "gcs": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": MEDIA_BUCKET,
            # None, not "private": buckets with uniform bucket-level access
            # (the default for new ones) reject any per-object ACL.
            "default_acl": None,
            "querystring_auth": True,
            "expiration": timedelta(seconds=MEDIA_URL_TTL_SECONDS),
            "file_overwrite": False,
        },
    },
}

if _media_backend not in _MEDIA_BACKENDS:
    raise ValueError(f"MEDIA_BACKEND={_media_backend!r} is not one of {sorted(_MEDIA_BACKENDS)}")

STORAGES = {
    "default": _MEDIA_BACKENDS[_media_backend],
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

# Env-selectable so a rehearsal of the production image can print mail (and
# the 2FA codes in it) to the worker log instead of needing a mail key.
# local.py and test.py override it regardless.
EMAIL_BACKEND = config("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = config("EMAIL_HOST", default="smtp.sendgrid.net")
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="apikey")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="support@example.com")

# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------

CELERY_BROKER_URL = config("REDIS_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# DatabaseScheduler reads this dict on startup and syncs it into the database,
# so adding an entry here needs no data migration.
#
# Both fan-out tasks iterate active organizations and dispatch one task each;
# neither takes a tenant from ambient context (ADR-004).
CELERY_BEAT_SCHEDULE = {
    # Tops every active recurring plan back up to the 8-week horizon. Runs in
    # the small hours UTC, which is outside the working day of every timezone
    # this product plausibly serves.
    "materialize-recurring-jobs": {
        "task": "scheduling.materialize_all_organizations",
        "schedule": crontab(hour="2", minute="0"),
    },
    # Hourly rather than daily on purpose: the task only judges local days that
    # are already over, so running every hour gives every tenant an
    # after-close pass in its own timezone without a per-tenant cron entry.
    "evaluate-access-reveals": {
        "task": "audit.evaluate_access_reveals_all",
        "schedule": crontab(minute="0"),
    },
}

REDIS_URL = CELERY_BROKER_URL

# Throttle counters live here. The default per-process LocMemCache would give
# every gunicorn worker its own count -- four workers, four times the rate --
# and forget it whenever one recycles.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "pink_glove",
    }
}

# --------------------------------------------------------------------------
# Application URLs
# --------------------------------------------------------------------------

# Where sign-in links point (magic links, the admin's redirect). Deployed it is
# the API's own origin (ADR-027); production.py requires it and requires https,
# because a customer's sign-in token travels in it.
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": config("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django.db.backends": {"level": "INFO", "handlers": ["console"], "propagate": False},
    },
}
