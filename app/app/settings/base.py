"""
Base settings shared by every environment.

Environment-specific modules (local / test / production) import * from here and
override. Nothing in this file may contain a secret or an environment-specific
hostname -- those come from the environment. See .env.example.
"""

from pathlib import Path

import dj_database_url
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
    "django.contrib.admin",
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
]

LOCAL_APPS = [
    "base",
    "organizations",
    "users",
    "two_factor",
    "customers",
    "catalog",
    "audit",
    "health",
    # Phase 3 adds: scheduling
    # Phase 4 adds: billing
    # Phase 5 adds: notifications
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow AuthenticationMiddleware: it resolves the tenant from
    # request.user's memberships.
    "app.middleware.tenant.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
    "DEFAULT_THROTTLE_RATES": {
        # Phase 1: consumed by the two_factor views.
        "two_factor_issue": "5/hour",
        "two_factor_verify": "10/hour",
        "magic_link": "5/hour",
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
CSRF_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SAMESITE = "None"
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
# deploy. Media storage is env-selected; see docs/DECISIONS.md ADR-006.

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

_media_backend = config("MEDIA_BACKEND", default="filesystem")

_MEDIA_BACKENDS = {
    "filesystem": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "s3": {"BACKEND": "storages.backends.s3.S3Storage"},
    "gcs": {"BACKEND": "storages.backends.gcloud.GoogleCloudStorage"},
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

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
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

REDIS_URL = CELERY_BROKER_URL

# --------------------------------------------------------------------------
# Application URLs
# --------------------------------------------------------------------------

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
