"""Local development settings. Never used in a deployed environment."""

from decouple import config

from .base import *  # noqa: F403
from .base import MIDDLEWARE, SECRET_KEY

DEBUG = True
ENV = "local"
LOCAL = True

SECRET_KEY = SECRET_KEY or "django-insecure-local-only-do-not-deploy-this-value"  # noqa: S105

ALLOWED_HOSTS = ["*"]

# Cookies cannot be Secure over plain http://localhost, and SameSite=None
# requires Secure -- so both relax here and only here.
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Prints every query and a running count per request. Noisy on purpose.
if config("QUERY_COUNT_DEBUG", default=False, cast=bool):
    MIDDLEWARE = MIDDLEWARE + ["app.middleware.queries.QueryCountDebugMiddleware"]
