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
├── app/                Django backend
│   ├── app/            project package: settings/, urls, celery, middleware
│   ├── base/           shared model + view foundations
│   ├── users/          authentication and membership
│   └── health/         liveness and readiness probes
├── docs/
│   ├── PLAN.md         phased build plan and status
│   └── DECISIONS.md    architecture decisions, incl. rejected alternatives
└── CLAUDE.md           project context and invariants
```

Settings are selected with `DJANGO_SETTINGS_MODULE`: `app.settings.local`
(default for `manage.py`), `app.settings.test` (default for pytest), or
`app.settings.production`.

## Deployment

No target is chosen yet, and nothing in the code assumes one. Everything
host-specific is behind an environment variable:

- `DATABASE_URL` — any managed Postgres
- `REDIS_URL` — any managed Redis
- `MEDIA_BACKEND` — `filesystem`, `s3`, or `gcs` (the latter two need
  `pip install ".[s3]"` / `".[gcs]"`)
- Static files are served by whitenoise from inside the container, so **no
  object storage is required to deploy at all**
- `SECURE_SSL_REDIRECT` — leave off where the platform already redirects at the
  edge; turn on for a bare VPS

Before deploying anywhere, this must come back clean:

```bash
python manage.py check --deploy --settings=app.settings.production
```

The image runs as a non-root user and bakes static files at build time, so the
running container needs no write access to `STATIC_ROOT`.

## Status

Phase 0 (scaffold) is complete. Phase 1 (tenancy core and identity) is next —
see `docs/PLAN.md`.
