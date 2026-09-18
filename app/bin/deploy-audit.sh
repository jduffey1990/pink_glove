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
# here", so the audit needs no build.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"

SECRET_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(64))')" \
SECURE_SSL_REDIRECT=True \
ALLOWED_HOSTS=example.com \
FRONTEND_BASE_URL=https://example.com \
FIELD_ENCRYPTION_KEY=audit-only-not-a-key \
MEDIA_BACKEND=s3 \
MEDIA_BUCKET=audit-only \
UI_DIST_DIR= \
  "$PYTHON" manage.py check --deploy --fail-level WARNING --settings=app.settings.production
