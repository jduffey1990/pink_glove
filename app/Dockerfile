# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------
# Builder -- dependencies land in a venv that the runtime stage copies out,
# so build tooling never ships in the final image.
# --------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

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

WORKDIR /code
COPY --chown=app:app . /code

# Baked in at build time so the running container needs no write access to
# STATIC_ROOT. Uses production settings so a settings misconfiguration fails
# the build rather than the first deploy.
# These values exist only for the duration of this layer -- they are not
# persisted into the image and are never used to encrypt anything. The real
# ones come from the environment at runtime.
RUN SECRET_KEY=build-time-only \
    ALLOWED_HOSTS=localhost \
    CORS_ALLOWED_ORIGINS=http://localhost \
    FIELD_ENCRYPTION_KEY=YnVpbGQtdGltZS1vbmx5LW5vdC1hLXJlYWwta2V5ISE= \
    DJANGO_SETTINGS_MODULE=app.settings.production \
    python manage.py collectstatic --noinput --clear

USER app

EXPOSE 8000

CMD ["gunicorn", "--config", "gunicorn_conf.py", "app.wsgi:application"]
