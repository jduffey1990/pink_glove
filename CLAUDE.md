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

# Deploy audit -- must come back clean. It needs a real-looking SECRET_KEY (a
# short one is itself a warning) and SECURE_SSL_REDIRECT, which production.py
# leaves off by default because most platforms redirect at the edge.
SECRET_KEY=$(.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))") \
SECURE_SSL_REDIRECT=True ALLOWED_HOSTS=example.com \
CORS_ALLOWED_ORIGINS=https://example.com \
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
Do not also run `npm run dev` on the host while that container is up: the port
is `strictPort`, so the second one now fails rather than drifting to :3001 and
being refused by CORS on every call.

**Signing in locally.** Owner, admin and dispatcher are challenged on every
sign-in by design (ADR-008) — there is no bypass and none should be added.
Instead the API returns the code in the login response as `dev_code` whenever
it runs with `LOCAL = True`, and the verify page shows it with a "Use it"
button. It is absent in any deployed environment, so nothing renders there.
Cleaners can tick "Trust this device" and skip the challenge for 30 days;
customers use magic links and are never challenged.

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
  from the job's `next_statuses` (and a 409 body's `allowed` list when one
  arrives), never from a client-side copy. `is_terminal` likewise.
- A dialog rendered behind a `v-if` that flips in the same tick as its
  `v-model` mounts with the model already true, so a `watch` on it needs
  `{ immediate: true }` or it never fires.
- One app per bounded concern; enums in `<app>/enums.py`.
- Views: DRF generic views and viewsets. Business logic that outgrows a view
  goes in `<app>/services.py`, not a fat model.


## Branches and production

New work happens on a branch, never directly on `main`. Cut one per phase or
per coherent piece of work (`phase-4-billing`, `fix-reveal-buffer`), commit
there, and leave merging into `main` to Jordan. Agents never commit on, merge
into, or push to `main`, and never force-push anything. Push a branch only
when Jordan asks.

There is no production yet — the deploy target is deliberately deferred
(ADR-006). When one exists, it is Jordan's alone: agents do not run anything
against a live database or service, do not read or use production credentials,
and do not change DNS, hosting settings, CI secrets, or deploy triggers.
Deploy and cutover steps are written and rehearsed locally by agents and
executed by Jordan.

## Commits

Commit early and often, one commit per coherent goal (a model with its
migration, an endpoint with its tests, a page). Do not leave long-running
uncommitted work. Every commit passes, for the side it touches:

- backend (`app/`): `pytest -q` and `ruff check . && ruff format --check .`;
- frontend (`ui/`): `npm run lint`, `npm run type-check` and `npm test`;
- a serializer, view, url, model or enum change also carries the regenerated
  `ui/openapi.yaml` and `ui/src/api/schema.d.ts` in the same commit (ADR-019).

There is no CI; pre-commit (ruff, schema currency, eslint, gitleaks) is the
only automatic check, so do not bypass it with `--no-verify`. It only runs if
the hook is installed in this checkout -- `app/.venv/bin/pre-commit install`
once, and check `.git/hooks/pre-commit` exists before trusting a quiet commit.

## Phase gate

Run this at the end of every phase, before starting the next. Spawn review
subagents in parallel, or work through the goals in sequence. Fix what they
find, and record the outcome in the phase's "as built" section of
`docs/PLAN.md` under a **Phase gate** heading — one line per item below, so a
fresh session can read what was checked and what was left.

1. **Test coverage.** New models, endpoints, services and Celery tasks have
   pytest tests; new stores, `src/lib/` helpers and non-trivial page logic have
   vitest specs. Every new tenant-scoped endpoint has a cross-tenant test (a
   user in organization A gets nothing of organization B's), and every new
   permission rule has a test per role on each side of the line. Update the
   expected test counts in `docs/PLAN.md`. List any gap explicitly.
2. **DRY.** Remove duplication the phase introduced or touched. Usual suspects
   here: date handling outside `ui/src/lib/datetime.ts`, API calls outside
   `ui/src/api/`, hand-built scheduling fixtures instead of
   `scheduling/tests/factories.py`, and client-side copies of server-owned
   rules or copy (ADR-023).
3. **Modularity.** Pages and components reach the backend through
   `ui/src/api/` and the stores, never `fetch` directly. Backend logic that
   outgrows a view lives in `<app>/services.py`; apps talk to each other
   through those services and model relations, not by reaching into another
   app's views or serializers.
4. **Orthogonality.** A change to one concern (auth, tenancy, scheduling,
   billing, a page) should not force edits in another. Where coupling is
   unavoidable, note it in a code comment and in the list below. Known
   unavoidable cases:
   - `ui/src/api/schema.d.ts` mirrors the serializers and views (ADR-019) —
     mechanical, and enforced by the pre-commit schema check;
   - `ENUM_NAME_OVERRIDES` in settings needs an entry per choice set whose
     field name collides (`Role`, `JobStatus`/`CustomerStatus`);
   - the Vite port (`:3000`, `strictPort`) must match `CORS_ALLOWED_ORIGINS`;
   - the job state machine lives on the server: `Job.next_statuses` and
     `is_terminal` say what the caller may do, and a 409's `allowed` list
     corrects a stale page. A new status touches the backend enum, the
     transitions, the schema and its label in `ui/src/lib/jobStatus.ts` --
     never a client-side table of moves;
   - role tiers are mirrored in `ui/`: `DISPATCHER_ROLES` in
     `src/stores/session.ts`, the route tiers in `src/router/index.ts`,
     `canEdit` in `ServicesPage.vue`, and the customer filter in
     `listAssignableStaff`. Changing a tier in `users/enums.py` means all four;
   - `scheduling/serializers.py` picks the field for a pricing 400 by reading
     the text of the `ValueError` from `catalog.models.Service.quote_cents`;
   - `customers/views.py` lazy-imports `scheduling.permissions`, because
     `scheduling` already imports `customers.models`.
5. **Security (authentication and authorization).**
   - Every new model holding tenant data inherits `TenantModel`, and every
     view over it inherits `TenantViewSetMixin` (invariant 1).
   - `organization` is stamped from the resolved tenant, never accepted from
     request data (invariant 2); new Celery tasks take `organization_id`
     (invariant 3).
   - No view relies on the global default by accident: each states its role
     permission, and anything `AllowAny` is deliberate and listed in the gate
     record (invariant 4).
   - No authorization decision lives only in the UI — hiding a button is not
     a permission.
   - A full `ModelViewSet` guards update as well as create and delete, and any
     join across a soft-deletable relation filters `deleted_at` itself (the
     manager only covers the model being queried; see `assigned_to()`).
   - Access codes stay write-only and encrypted, and are read only through the
     audited reveal (ADR-014, ADR-016); nothing sensitive is logged.
   - No secrets in git or in the `ui/` bundle (invariant 7).
   - The deploy audit (`check --deploy`) comes back clean.
   - Run `/security-review` on the phase diff.
6. **Branch and production guard.** Confirm the phase's work is on its branch,
   nothing was committed to `main`, and — once a production exists — nothing
   touched it.
