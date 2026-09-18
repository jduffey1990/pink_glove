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

Everything host-specific is an environment variable, and `app/.env.example`
documents each one where it is set: any managed Postgres and Redis (Redis
also holds the throttle counters, so it is required), private object storage
for photographs read through expiring URLs, the proxy depth the sign-in
throttles rely on, and static files served by whitenoise from inside the
container so no object storage is needed for them. Production settings refuse
to import when one of these is wrong, rather than failing on the first
upload.

Before deploying anywhere, this must come back clean:

```bash
cd app && bin/deploy-audit.sh
```

The image runs as a non-root user and bakes static files at build time, so the
running container needs no write access to `STATIC_ROOT`. CI
(`.github/workflows/ci.yml`) runs the backend and frontend checks, builds the
image, and deploys `main` to staging once that is stood up.

## Status

See `docs/PLAN.md` for the phase table and what is next.
