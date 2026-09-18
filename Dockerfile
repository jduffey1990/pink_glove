# syntax=docker/dockerfile:1
#
# One image for the whole product (ADR-027): the API, its workers, and the
# built frontend, served from the same origin. Build from the REPOSITORY ROOT:
#
#   docker build -t pink-glove .
#
# The compose file in app/ does this for you (context: ..). Which process the
# container runs is decided at start: the default CMD is gunicorn; the worker
# and beat override it (see app/docker-compose.yml and deploy/fly.toml).

# --------------------------------------------------------------------------
# Frontend build -- Vite's output, and nothing else, leaves this stage.
# --------------------------------------------------------------------------
# Node 22 because eslint-config-vuetify's toolchain calls Object.groupBy,
# which does not exist before Node 21. Matches ui/.nvmrc and the compose `ui`
# service. Type-checking and lint run in CI, not here: the build only needs
# to compile, and doubling the work on every image build buys nothing.
FROM node:22-alpine AS ui

WORKDIR /ui

COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-fund --no-audit

COPY ui/ ./
# No VITE_API_BASE_URL: a production build calls the API with relative URLs,
# because it is served by the API (see ui/src/api/client.ts).
RUN npm run build-only

# --------------------------------------------------------------------------
# Python builder -- dependencies land in a venv that the runtime stage copies
# out, so build tooling never ships in the final image.
# --------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY app/pyproject.toml ./
# The s3 extra is what deployed media uses (Tigris on Fly, or AWS itself);
# it is installed here rather than at deploy time so the image is the same
# thing everywhere it runs.
RUN pip install --no-cache-dir ".[s3]"

# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# libpq for psycopg, curl for container healthchecks.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv

# The frontend build, where settings expect it by default (UI_DIST_DIR):
# /ui/dist in the image is ../ui/dist relative to /code, the same shape as a
# checkout. Whitenoise serves its files; app.spa serves its index.
COPY --from=ui --chown=app:app /ui/dist /ui/dist

WORKDIR /code
COPY --chown=app:app app/ /code

# Baked in at build time so the running container needs no write access to
# STATIC_ROOT. Uses production settings so a settings misconfiguration fails
# the build rather than the first deploy.
# These values exist only for the duration of this layer -- they are not
# persisted into the image and are never used to encrypt anything or reach
# any service; production settings only check they are present, and the
# Fernet key is parsed on first use, not at import. The real ones come from
# the environment at runtime.
RUN SECRET_KEY=build-time-only \
    ALLOWED_HOSTS=localhost \
    FRONTEND_BASE_URL=https://localhost \
    FIELD_ENCRYPTION_KEY=build-time-only-not-a-key \
    MEDIA_BACKEND=s3 \
    MEDIA_BUCKET=build-time-only \
    DJANGO_SETTINGS_MODULE=app.settings.production \
    python manage.py collectstatic --noinput --clear

USER app

EXPOSE 8000

CMD ["gunicorn", "--config", "gunicorn_conf.py", "app.wsgi:application"]
