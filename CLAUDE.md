# pink_glove

CRM, scheduling, and billing for a cleaning company. Django + DRF backend,
multi-tenant from the start, deployed as a Docker container.

Built by taking the foundations of a production Django app and leaving the rest.
**Source repo (read-only reference): `/Users/jordanduffey/Desktop/pomarium copy`**
— it is outside this working directory, so read it by absolute path. Do not
modify it. Nothing Pomarium-branded belongs in this repo.

## Read before changing anything structural

- `docs/DECISIONS.md` — every load-bearing decision, **with what was rejected and
  why**. Several rejected designs look like obvious improvements. Read the
  rejection before "fixing" one.
- `docs/PLAN.md` — the phased build plan and current status.

## Layout

```
pink_glove/
├── app/                    # Django backend
│   ├── app/                # project package: settings/, urls, celery, middleware
│   ├── base/               # Base + TenantModel, managers, TenantViewSetMixin, permissions
│   ├── users/              # CustomUser, Membership, Role enum, auth
│   ├── customers/          # Customer, ServiceLocation (codes encrypted)
│   ├── catalog/            # Service + pricing models
│   ├── audit/              # AccessReveal, the end-of-day evaluator
│   ├── scheduling/         # RecurringPlan, Job, assignments, time, notes, photos
│   ├── health/             # liveness + readiness
│   └── ...                 # organizations, two_factor; billing and
│                           #   notifications by phase
├── ui/                     # Vue 3 + Vuetify 4 + TypeScript SPA (ADR-018)
│   ├── src/api/            # client.ts (CSRF + org header), generated schema.d.ts
│   ├── src/stores/         # session store: boot, login, organization choice
│   ├── src/lib/            # datetime helpers, all in the org's timezone
│   └── src/pages/          # one file per screen
└── docs/
```

Settings are a package, selected by `DJANGO_SETTINGS_MODULE`:
`app.settings.local` (manage.py default) / `.test` (pytest default) /
`.production`.

## Invariants

These hold across every phase. A change that breaks one needs a new ADR, not a
quiet edit.

1. **Tenant data is scoped at the chokepoint.** Models holding tenant data
   inherit `TenantModel`; views over them inherit `TenantViewSetMixin`. Never
   auto-filter a default manager from request context — see ADR-002.
2. **`organization` is stamped from the resolved tenant, never read from request
   data.**
3. **Celery tasks take `organization_id` explicitly.** No implicit tenant
   context in background work.
4. **DRF denies by default.** `IsAuthenticated` is the global default; public
   endpoints opt in with `AllowAny` explicitly.
5. **Money is integer cents.** Never floats.
6. **Concrete models that declare `Meta` must inherit `Base.Meta`**
   (`class Meta(Base.Meta):`), or soft delete and manager resolution break
   silently. A test enforces this.
7. **No secrets in source.** Everything via env; `.env` is gitignored.
   `gitleaks` runs in pre-commit.
8. **Timestamps stored UTC.** Display and recurrence expansion happen in
   `Organization.timezone`. Recurrence expands *naive local* and converts to
   UTC last — expanding in UTC shifts every job by an hour for half the year.
9. **The generated schema has zero warnings.** A test asserts it. An action
   drf-spectacular cannot describe is one the frontend cannot call, so new
   `@action`s carry `@extend_schema` (ADR-019).

## Commands

Run from `app/`. Docker is the primary path; the host venv is for a fast test
loop.

```bash
# Full stack (web, worker, beat, db, redis)
docker compose up -d
docker compose logs -f web
docker compose down

# Dependencies only, for host-based work
docker compose up -d db redis

# Host venv
python3 -m venv .venv && .venv/bin/pip install ".[dev]"

# Tests -- DATABASE_URL points at the container's published port
DATABASE_URL=postgres://pink_glove:pink_glove@localhost:5432/pink_glove \
REDIS_URL=redis://localhost:6379/0 \
  .venv/bin/pytest -q

# Lint / format
.venv/bin/ruff check . && .venv/bin/ruff format .

# Deploy audit -- must come back clean
SECRET_KEY=x ALLOWED_HOSTS=example.com CORS_ALLOWED_ORIGINS=https://example.com \
  .venv/bin/python manage.py check --deploy --settings=app.settings.production

# Migrations
docker compose run --rm web python manage.py makemigrations
docker compose run --rm web python manage.py migrate

# Two demo organizations, each with users in every role, three customers,
# three services, two recurring plans and a materialized board of jobs
# (password printed at the end). Two, not one -- tenant isolation bugs are
# invisible with a single tenant.
docker compose run --rm web python manage.py seed_demo

# OpenAPI schema (ADR-019). Both are authenticated; sign in first.
#   GET /api/schema/   the schema itself
#   GET /api/docs/     Swagger UI
python manage.py spectacular --file openapi.yaml
```

`.env` is required (`cp .env.example .env`). The default `DATABASE_URL` uses the
`db` hostname, which only resolves inside compose — override it for host runs.

### Frontend

Run from `ui/`. **Node 22 is required**, not preferred: eslint's config
toolchain calls `Object.groupBy`, which does not exist before Node 21, so
linting crashes with a `TypeError` from inside a dependency on Node 20.
`nvm use` reads `ui/.nvmrc`.

```bash
npm install
npm run dev            # Vite on :3000, matching CORS_ALLOWED_ORIGINS
npm run lint           # eslint; lint:fix to apply
npm run type-check     # vue-tsc
npm test               # vitest
npm run build

# Regenerate the API contract after ANY backend serializer or view change,
# and commit the result in the same diff (ADR-019). Needs app/.venv.
npm run api:types      # -> ui/openapi.yaml and ui/src/api/schema.d.ts
```

The whole stack, including the dev server, comes up with
`docker compose up -d` from `app/` — the `ui` service runs Vite on :3000.

## Conventions

- Line length 100, `ruff` for lint and format, `ruff` rule set in
  `pyproject.toml`.
- Tests live in `<app>/tests/`, named `test_*.py`, pytest style.
- **Sign tests in with `client.force_login(user)`, never DRF's
  `force_authenticate()`.** The latter attaches the user after Django
  middleware has run, so `TenantMiddleware` sees `AnonymousUser` and resolves
  no organization — tests then pass or fail for the wrong reason. Use the
  `authed_client` fixture.
- `override_settings` cannot decorate a plain pytest class. Use pytest-django's
  `settings` fixture in an autouse fixture instead.
- **Compose DRF permissions on the classes, not instances**:
  `(IsDispatcherOrHigher | IsAssignedCleaner)()`. The instance form raises
  `TypeError` at request time, which reads as a 500 rather than a mistake.
- Build scheduling rows with `scheduling/tests/factories.py`. A `Job` needs
  four related rows that must all agree about the tenant; by hand that is a
  test about fixtures rather than about behaviour.

### Frontend

- **Never hand-edit `ui/src/api/schema.d.ts`.** It is generated; change the
  backend and rerun `npm run api:types`.
- **Write paths use the generated *request* types**, not the response ones.
  They differ where it matters: a location's access codes are write-only, so
  they exist on `ServiceLocationRequest` and not on `ServiceLocation`. The
  types are what keep a code off a read payload.
- **Dates are reckoned in `session.organization.timezone`, never the
  browser's.** Use `src/lib/datetime.ts`; the API's `date_from`/`date_to` are
  organization-local dates and the backend does the conversion.
- **The server owns the job state machine.** Render the next-status buttons
  from the 409 body's `allowed` list rather than from a client-side copy.
- A dialog rendered behind a `v-if` that flips in the same tick as its
  `v-model` mounts with the model already true, so a `watch` on it needs
  `{ immediate: true }` or it never fires.
- One app per bounded concern; enums in `<app>/enums.py`.
- Views: DRF generic views and viewsets. Business logic that outgrows a view
  goes in `<app>/services.py`, not a fat model.
