"""Test settings. Optimised for speed and for failing loudly."""

from .base import *  # noqa: F403
from .base import MIDDLEWARE

DEBUG = False

# Whitenoise isn't under test and warns on every request about a missing
# STATIC_ROOT that only exists after collectstatic.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]
ENV = "test"
LOCAL = False

SECRET_KEY = "django-insecure-test-only"  # noqa: S105

ALLOWED_HOSTS = ["*", "testserver"]

# ~10x faster than the default hasher across a suite that creates many users.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_DOMAIN = None

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Tasks run inline; no broker needed to run the suite.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_RESULT_BACKEND = "cache+memory://"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Throttles would make test outcomes depend on execution order.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {},
}
