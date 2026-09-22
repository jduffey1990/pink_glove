"""Local development settings. Never used in a deployed environment."""

from decouple import config

from .base import *  # noqa: F403
from .base import FIELD_ENCRYPTION_KEY, MIDDLEWARE, REST_FRAMEWORK, SECRET_KEY

DEBUG = True
ENV = "local"
LOCAL = True

SECRET_KEY = SECRET_KEY or "django-insecure-local-only-do-not-deploy-this-value"  # noqa: S105

# A fixed throwaway key, so encrypted columns survive a local database that
# outlives the process. Anything written with it is readable by anyone.
FIELD_ENCRYPTION_KEY = FIELD_ENCRYPTION_KEY or "cGluay1nbG92ZS1sb2NhbC1kZXYta2V5LU5PVFJFQUw="  # noqa: S105

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

# Five sign-ins an hour is right for a deployed environment and unworkable for
# development: building the login screen means signing in dozens of times, and
# the throttle is keyed on IP, so every developer and every browser profile on
# the machine shares one bucket. Raised here and only here -- production keeps
# the real rates from base.py.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        "two_factor_issue": "1000/hour",
        "two_factor_verify": "1000/hour",
        "magic_link": "1000/hour",
        "login_account": "1000/hour",
        "pay_page": "1000/hour",
        "pay_checkout": "1000/hour",
    },
}

# Prints every query and a running count per request. Noisy on purpose.
if config("QUERY_COUNT_DEBUG", default=False, cast=bool):
    MIDDLEWARE = MIDDLEWARE + ["app.middleware.queries.QueryCountDebugMiddleware"]
