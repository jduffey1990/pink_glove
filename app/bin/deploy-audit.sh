#!/usr/bin/env bash
#
# Django's deploy audit against the production settings, with the values it
# needs to import at all. One script so CLAUDE.md, README and CI run the same
# thing; a new required production variable is added here, once.
#
# The values are placeholders that exist for this process only and reach no
# service: production.py checks they are present, and the Fernet key is parsed
# on first use, not at import. SECURE_SSL_REDIRECT is forced on because the
# audit warns without it, though Fly redirects at the edge and the deploy
# leaves it off. UI_DIST_DIR is set EMPTY, the explicit "frontend is not
# here", so the audit needs no build. The mail values are the default SMTP
# backend with a sender and a password that are only checked for being there;
# nothing is sent.
#
# STRIPE_SECRET_KEY, STRIPE_PLATFORM_PRICE_ID and SENTRY_DSN are set EMPTY for
# the other reason: python-decouple fills anything the environment leaves out
# from app/.env, so a developer's Stripe key (with no Connect webhook secret
# beside it), or a platform price with the key emptied here, failed the audit
# on their machine while CI, which has no .env, passed. Empty here means the
# optional integrations are off, whatever the .env says, and an audit run can
# never start error reporting. A new optional integration that production.py
# refuses half-configured is set empty here too.
#
# The Dockerfile's collectstatic layer and deploy/docker-compose.rehearsal.yml
# import production settings too and carry their own copies of this list.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"

SECRET_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(64))')" \
SECURE_SSL_REDIRECT=True \
ALLOWED_HOSTS=example.com \
FRONTEND_BASE_URL=https://example.com \
FIELD_ENCRYPTION_KEY=audit-only-not-a-key \
DEFAULT_FROM_EMAIL=audit-only@pink-glove.invalid \
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend \
EMAIL_HOST_PASSWORD=audit-only-not-a-key \
STRIPE_SECRET_KEY= \
STRIPE_PLATFORM_PRICE_ID= \
SENTRY_DSN= \
MEDIA_BACKEND=s3 \
MEDIA_BUCKET=audit-only \
UI_DIST_DIR= \
  "$PYTHON" manage.py check --deploy --fail-level WARNING --settings=app.settings.production
