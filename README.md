# pink_glove

CRM, scheduling, and billing for a cleaning company. Django 5.2 LTS + DRF,
multi-tenant, PostgreSQL, Celery, deployed as a Docker container.

## Quick start

```bash
cd app
cp .env.example .env        # required; .env is gitignored
docker compose up -d
```

Then:

- http://localhost:8000/health/live/ → `{"status": "ok"}`
- http://localhost:8000/health/ready/ → database and redis status
- http://localhost:8000/admin/

Create an admin user:

```bash
docker compose run --rm web python manage.py createsuperuser
```

## Development

Docker is the primary path. A host virtualenv gives a faster test loop:

```bash
cd app
docker compose up -d db redis          # dependencies only

python3 -m venv .venv
.venv/bin/pip install ".[dev]"

DATABASE_URL=postgres://pink_glove:pink_glove@localhost:5432/pink_glove \
REDIS_URL=redis://localhost:6379/0 \
  .venv/bin/pytest -q

.venv/bin/ruff check . && .venv/bin/ruff format .
```

The default `DATABASE_URL` uses the `db` hostname, which only resolves inside
compose — override it when running against the published port from the host.

Install the git hooks once:

```bash
pre-commit install
```

## Layout

```
pink_glove/
├── Dockerfile          one image: API, workers and the built frontend
├── app/                Django backend
│   ├── app/            project package: settings/, urls, celery, middleware
│   ├── base/           shared model + view foundations
│   ├── users/          authentication and membership
│   └── health/         liveness and readiness probes
├── ui/                 Vue 3 + Vuetify SPA
├── deploy/             fly.toml, and the local rehearsal of the deployed shape
├── docs/
│   ├── PLAN.md         phased build plan and status
│   ├── DECISIONS.md    architecture decisions, incl. rejected alternatives
│   └── DEPLOY.md       the runbook
└── CLAUDE.md           project context and invariants
```

Settings are selected with `DJANGO_SETTINGS_MODULE`: `app.settings.local`
(default for `manage.py`), `app.settings.test` (default for pytest), or
`app.settings.production`.

## Deployment

Fly.io, one image, three process groups (ADR-027). `docs/DEPLOY.md` is the
runbook: standing up staging, production, a local rehearsal of the exact
image, rotation and recovery. Production is Jordan's alone.

The image is built from the **repository root** and carries the frontend
build, which gunicorn serves from the same origin as the API:

```bash
docker build -t pink-glove .
```

Everything host-specific is an environment variable:

- `DATABASE_URL` — any managed Postgres
- `REDIS_URL` — any managed Redis. Also holds the throttle counters, so it is
  required, not optional
- `MEDIA_BACKEND` — `s3` (installed in the image; Tigris on Fly, or AWS) or
  `gcs`. `filesystem` is refused in production unless `MEDIA_ROOT` names a
  mounted volume. Objects are written private and read through URLs that
  expire after `MEDIA_URL_TTL_SECONDS` (default 300)
- `MEDIA_BUCKET` — or `BUCKET_NAME`, which `fly storage create` sets
- `UI_DIST_DIR` — where the frontend build is; the image's default is right.
  Set it empty only to host the frontend elsewhere, and then also set
  `CORS_ALLOWED_ORIGINS`, which loosens the cookies to `SameSite=None`
- `NUM_PROXIES` — how many proxies you control sit in front of the app
  (1 on Fly, the default in production). The sign-in throttles key on the
  client IP, and this is how it is found: too low and every client shares
  the proxy's bucket, too high and `X-Forwarded-For` is spoofable
- `SECURE_SSL_REDIRECT` — leave off where the platform already redirects at
  the edge (Fly does); turn on for a bare VPS
- Static files are served by whitenoise from inside the container, so no
  object storage is needed for them

Before deploying anywhere, this must come back clean:

```bash
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))") \
SECURE_SSL_REDIRECT=True ALLOWED_HOSTS=example.com \
FIELD_ENCRYPTION_KEY=audit-only-not-a-key \
MEDIA_BACKEND=s3 MEDIA_BUCKET=audit-only UI_DIST_DIR= \
  python manage.py check --deploy --fail-level WARNING --settings=app.settings.production
```

The image runs as a non-root user and bakes static files at build time, so the
running container needs no write access to `STATIC_ROOT`. CI
(`.github/workflows/ci.yml`) runs the backend and frontend checks, builds the
image, and deploys `main` to staging once that is stood up.

## Status

See `docs/PLAN.md` for the phase table and what is next.
