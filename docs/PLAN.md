# Build plan

Rationale for the decisions referenced here lives in `docs/DECISIONS.md`.
Invariants that hold across all phases are in `CLAUDE.md`.

---

## Start here

Everything through Phase 2.5 is on `main`, tests green. Next work is Phase 3
(scheduling backend, then the first frontend slice), specified below. The
frontend stack is decided (ADR-018, ADR-019); the source repo's `ui/` is
reference only.

```bash
cd app
cp .env.example .env                    # required, gitignored
docker compose up -d                    # web, worker, beat, db, redis
docker compose run --rm web python manage.py seed_demo

python3 -m venv .venv                   # faster test loop than docker
.venv/bin/pip install ".[dev]"
DATABASE_URL=postgres://pink_glove:pink_glove@localhost:5432/pink_glove \
REDIS_URL=redis://localhost:6379/0 .venv/bin/pytest -q
```

Expect **149 passing**. Read `CLAUDE.md` first — it has the invariants and the
testing gotchas that will otherwise cost you an hour each.

**Decisions still open, listed where they bite:**

1. **Deploy target** — deliberately deferred (ADR-006). Nothing in the code
   assumes one. Now also covers how the `ui/` build is served.
2. **Stripe Connect** — one Stripe account is right for one tenant; real
   paying tenants change the model (ADR-007). Decide before building Phase 4
   hard against the single-account assumption.

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, settings, Docker, test harness | **Done** |
| 1 | Tenancy core + identity | **Done** |
| 2 | Customers + service catalog | **Done** |
| 2.5 | Access audit trail | **Done** (flagging pass deferred to 3) |
| 3a | Scheduling backend, audit evaluator, OpenAPI | Next |
| 3b | Frontend slice (`ui/`), built against 3a | After 3a |
| 4 | Billing | Not started |
| 5 | Notifications + customer portal | Not started |

---

## Phase 0 — Scaffold ✅

Delivered and verified:

- `pyproject.toml` as the single dependency source (15 runtime packages, down
  from the source repo's 60-package `pip freeze`). Django 5.2.17 LTS.
- Settings package — `base` / `local` / `test` / `production`, all secrets and
  hostnames env-driven. `check --deploy` clean.
- `base` app — `Base` abstract model, UUID PK, working soft delete
  (`SoftDeleteManager` / `all_objects`).
- `users` app — `CustomUser`, email-keyed, no org or role field (ADR-003).
  Shipped in Phase 0 so `AUTH_USER_MODEL` predates the first migration
  (ADR-011).
- `health` app — split liveness / readiness (ADR: a DB blip should not
  restart-loop every container).
- Multi-stage Dockerfile, non-root, `collectstatic` at build time under
  production settings so a settings error fails the build.
- `docker-compose.yml` — db, redis, migrate (runs to completion first), web,
  worker, beat, with healthchecks and `service_healthy` gating.
- pytest + ruff + pre-commit (incl. `gitleaks`).

**Verified:** `docker compose up` → all services healthy; `/health/live/` and
`/health/ready/` return ok; 22 tests pass; ruff clean; `check --deploy` clean.

---

## Phase 1 — Tenancy core + identity ✅

78 tests passing. The conformance test was verified by deliberately removing
`TenantViewSetMixin` from `MembershipViewSet`: it failed and named both leaking
routes, and the two behavioural cross-org tests failed alongside it. Restored
and green.

End-to-end against `docker compose up`, with two seeded organizations: owner
login returns a challenge; the session stays anonymous until verification; a
wrong code is refused; a correct code returns the session; `/api/users/memberships/`
returns 5 of the 10 seeded memberships, all from the caller's own organization.
Cleaner login is challenged (untrusted device), customer login is not.

Notes for whoever picks up Phase 2:

- **Tests must sign in with `client.force_login()`, not DRF's
  `force_authenticate()`.** The latter attaches the user during DRF view
  dispatch, which runs *after* Django middleware, so `TenantMiddleware` would
  still see `AnonymousUser` and resolve no organization. `conftest.authed_client`
  does this correctly.
- **Throttle scopes cannot be removed in test settings.** `ScopedRateThrottle`
  raises `ImproperlyConfigured` when a view's scope is missing from
  `DEFAULT_THROTTLE_RATES`, so `app/settings/test.py` raises the rates instead.
- `base/tests/test_tenancy.py::_synthetic_models` documents an
  `isolate_apps` subtlety worth reading before adding tests there.

<details>
<summary>Original Phase 1 specification</summary>

### 1.1 `base/` — enforcement machinery

`TenantModel(Base)` — abstract, adds `organization` FK (`PROTECT`, indexed),
composite index on `(organization, created_at)`.

`TenantViewSetMixin` — `get_queryset()` scopes to `request.organization`;
`perform_create()` stamps it. Organization is never read from request data.

`TenantModel.clean()` — validates that every FK pointing at another
`TenantModel` shares the same organization. Viewset scoping alone does not catch
cross-org record stitching.

`base/permissions.py` — `IsOrgMember`, `IsOwnerOrAdmin`, `IsDispatcherOrHigher`,
`IsAssignedCleaner`, `IsCustomerSelf`, built from the role tiers already defined
in `users/enums.py`.

### 1.2 Conformance test — `base/tests/test_tenancy.py`

Walks `get_resolver().url_patterns` recursively. For every DRF view, resolves the
model from `queryset` / `serializer_class.Meta.model`; if it subclasses
`TenantModel`, asserts the view inherits `TenantViewSetMixin`.

**Known limitation, handled deliberately:** views that build their queryset only
inside `get_queryset()` cannot be resolved statically. Those go in an explicit
`TENANCY_EXEMPT` frozenset at the top of the test file — every exemption is then
a reviewable line in a diff rather than a silent gap.

Plus a behavioral matrix: two organizations, full CRUD, cross-org reads return
**404 not 403** (403 confirms the record exists), cross-org writes rejected,
cross-org FK assignment rejected.

### 1.3 `app/middleware/tenant.py`

Sets `request.organization`, ordered after `AuthenticationMiddleware`:

1. Anonymous → `None`; permission classes reject
2. Exactly one active `Membership` → that organization
3. Several → `X-Organization` header, validated against the user's memberships
4. Superuser → may pass `X-Organization` for any organization

### 1.4 `organizations/`

`Organization(Base)`: `name`, `slug`, `timezone` (IANA, default
`America/Denver`), `logo`, `primary_color`, `phone`, `email`, address fields,
`is_active`, plus parked `stripe_customer_id` / `stripe_subscription_id`.

`timezone` is load-bearing from day one — see ADR-012.

### 1.5 `users/` — membership and auth

`Membership(TenantModel)`: `user`, `organization`, `role`, `is_active`,
`invited_at`, `accepted_at`. Unique on `(user, organization)`.

`MagicLinkToken(Base)`: single-use, hashed, 15-minute expiry, for the customer
portal. Model plus issue/consume endpoints land here against the console email
backend; real delivery is Phase 5.

Endpoints: `login` (delegates to two_factor), `logout`, `session` / `me`.

### 1.6 `two_factor/` — ported and hardened

`TwoFactorCode(Base)` — `code_hash`, `attempts` (cap 5), `consumed_at`,
throttled, `dev_code` only when `LOCAL`. `TrustedDevice(Base)` — hashed token,
30-day expiry — is what makes 2FA workable for cleaners.

Per-role policy per ADR-008. The `login → issue → verify → login()` session flow
carries over from the source repo; it is sound.

### 1.7 Admin

Every model registered with org-scoped `get_queryset()`. The source repo's
`admin.py` files are empty stubs, which leaves real capability unused — for a
one-customer CRM the Django admin *is* the back-office tool long before any
custom UI exists.

**Done when:** conformance test and cross-org matrix green; all three auth flows
work end to end against `docker compose up`; a seed command creates two
organizations with users in each.

</details>

---

## Phase 2 — Customers and service catalog ✅

129 tests passing.

- `customers`: `Customer` (billing address, status, source, optional `user` link
  for portal access) and `ServiceLocation` (service address, sqft/beds/baths,
  access and pet notes). A check constraint requires at least one of first name,
  last name, or company name.
- `catalog`: `Service` with flat / hourly / per-sqft pricing and a
  `quote_cents()` that rounds once at the end (ADR-009). `POST
  /api/catalog/services/{id}/quote/` exposes it.
- Gate codes, alarm codes, and key locations are encrypted at rest (ADR-014).
- Permissions differ by role: cleaners can read the catalog but not change
  prices; dispatchers manage customers but not prices; customers see neither.

Two things surfaced while building:

- **`TenantModel.save()`'s cross-org guard was returning 500, not 400.** Caught
  by the location-attachment test. Fixed globally with a DRF exception handler
  (ADR-015).
- **Adding `FIELD_ENCRYPTION_KEY` broke the Docker build** before it broke a
  deploy: `production.py` refuses to boot without it, and the image runs
  `collectstatic` under production settings. The Dockerfile now passes a
  throwaway build-time key. This is the settings-validation-at-build-time design
  working as intended.

Verified live: creating a location through the API returns `gate_code` as
`4821#`, while `SELECT gate_code FROM customers_servicelocation` returns
`gAAAAABqcjNG...`.

---

## Phase 2.5 — Access audit trail ✅

149 tests passing. Full rationale in ADR-016.

`audit` app with append-only `AccessReveal`. Access codes moved behind
`POST /api/customers/locations/{id}/reveal-access/`, became `write_only` on the
location serializer, and were removed from the Django admin (which logs changes
but not views).

**Carried into Phase 3:**

1. Add `job = FK("scheduling.Job", null=True)` to `AccessReveal` and set it on
   reveal, so a reveal binds to the specific visit rather than just a person
   and a time.
2. Write the end-of-day Celery beat task that sets `evaluated_at`,
   `is_flagged`, and `flag_reason` — flagging reveals more than 1–2 hours
   outside their appointment window, in the organization's own timezone.
   `AccessReveal` already carries all four fields, unset.
3. The buffer (1h before / 2h after) should be configurable per organization
   rather than hardcoded.

**Frontend contract:** the confirmation copy is served by the backend
(`audit.models.ACCESS_WARNING`, returned in the 400 when `acknowledged` is
missing). Render that string rather than hardcoding it, so the two cannot
drift.

---

## Phase 3 — Scheduling ← next

Split in two. **3a** is the backend and ships first. **3b** is the first
frontend slice and is built against 3a's real endpoints, so that the job list
and assignment endpoints get shaped by an actual consumer before they harden.
Stack decisions for 3b are in ADR-018 and ADR-019; do not re-open them here.

Work through the checkpoints in order. Each one ends with the full suite green
and `ruff check . && ruff format .` clean. Do not start a checkpoint's API
before its models and services have tests.

### 3a.0 — Prep

1. Add dependencies to `pyproject.toml` (ADR-013 — nowhere else):
   `python-dateutil>=2.9,<3` (already installed transitively via Celery; declare
   it because we now import it directly) and `drf-spectacular>=0.28,<1`.
2. Wire drf-spectacular (ADR-019): `DEFAULT_SCHEMA_CLASS`, `SPECTACULAR_SETTINGS`
   with a title, version, and `SERVE_INCLUDE_SCHEMA: False`. Routes:
   `GET /api/schema/` (YAML/JSON) and `GET /api/docs/` (Swagger UI). Both keep
   the global `IsAuthenticated` default — no `AllowAny`. Add a test that
   generates the schema and asserts zero spectacular warnings; every viewset
   added later must keep that test green (use `@extend_schema` for actions
   whose request/response is not the viewset serializer).
3. Decorate `users.views.SessionView.get` with `ensure_csrf_cookie`. Today
   nothing sets the `csrftoken` cookie before the first POST, so a fresh
   browser's login attempt fails CSRF. Add a test that a GET to the session
   endpoint sets the cookie.

### 3a.1 — Organization: business hours and reveal buffers

New fields on `Organization`, one migration, all with defaults so existing rows
need no backfill:

```
business_hours_start          TimeField, default 07:00
business_hours_end            TimeField, default 19:00
working_days                  JSONField list of ISO weekday ints (1=Mon .. 7=Sun),
                              default [1,2,3,4,5]; validate members and uniqueness
reveal_buffer_before_minutes  PositiveIntegerField, default 60
reveal_buffer_after_minutes   PositiveIntegerField, default 120
```

Add `Organization.is_within_business_hours(dt: datetime) -> bool` that converts
`dt` to the organization's timezone and checks weekday and time. Expose the
fields on `OrganizationSerializer`, writable by admin+ only. Test the helper
across midnight-adjacent times and a non-working day.

### 3a.2 — `scheduling` app: models

New app `scheduling`, registered in `INSTALLED_APPS`, routed at
`/api/scheduling/`. Every model is a `TenantModel` with `class Meta(TenantModel.Meta)`.
Enums live in `scheduling/enums.py`.

```
JobStatus   SCHEDULED, EN_ROUTE, IN_PROGRESS, COMPLETE, CANCELLED, NO_ACCESS
```

`NO_ACCESS` is not padding — "we couldn't get in" is a distinct outcome from
"cancelled", it happens regularly, and it needs its own billing treatment.
`TERMINAL_STATUSES = (COMPLETE, CANCELLED, NO_ACCESS)`.

```
RecurringPlan
    customer            FK Customer, PROTECT
    location            FK ServiceLocation, PROTECT
    service             FK Service, PROTECT
    rrule               TextField — RFC 5545 RRULE body only, e.g.
                        "FREQ=WEEKLY;BYDAY=TU". No DTSTART inside; starts_on and
                        preferred_start_time supply it. Validated by parsing with
                        dateutil.rrule.rrulestr in clean() and in the serializer.
    starts_on           DateField (local date of the first possible occurrence)
    ends_on             DateField, null — inclusive
    preferred_start_time TimeField (local wall-clock time)
    duration_minutes    PositiveIntegerField, defaults from
                        service.default_duration_minutes at creation
    price_override_cents PositiveIntegerField, null — when set, every
                        materialized job snapshots this instead of a fresh quote
    default_assignees   M2M CustomUser, blank — auto-assigned at materialization
    is_active           BooleanField, default True, db_index
    notes               TextField, blank

    clean(): location.customer_id == customer_id; ends_on >= starts_on;
             rrule parses and contains no DTSTART/UNTIL/COUNT that conflicts
             with starts_on/ends_on (reject DTSTART outright; allow COUNT).

Job
    customer            FK Customer, PROTECT
    location            FK ServiceLocation, PROTECT
    service             FK Service, PROTECT
    plan                FK RecurringPlan, null, SET_NULL
    plan_occurrence     DateTimeField, null — the UTC instant this occurrence was
                        ORIGINALLY planned for. Immutable after creation. This,
                        not scheduled_start, is the idempotency key: a job that
                        gets rescheduled must not be re-created at its old slot.
    scheduled_start     DateTimeField (UTC)
    scheduled_end       DateTimeField (UTC)
    status              CharField JobStatus, default SCHEDULED, db_index
    status_changed_at   DateTimeField, null
    price_cents         PositiveIntegerField — snapshot, see below
    notes               TextField, blank — dispatcher instructions for the visit,
                        distinct from JobNote (what the cleaner reports back)
    cancellation_reason CharField 255, blank

    constraints:
      CheckConstraint scheduled_end > scheduled_start
      UniqueConstraint (plan, plan_occurrence)
          condition plan not null and deleted_at is null
          name unique_job_per_plan_occurrence
    indexes: (organization, scheduled_start), (organization, status, scheduled_start)
    ordering: scheduled_start

JobAssignment
    job                 FK Job, CASCADE, related_name assignments
    user                FK CustomUser, PROTECT, related_name job_assignments
    assigned_by         FK CustomUser, null, SET_NULL
    assigned_at         DateTimeField, auto_now_add
    accepted_at         DateTimeField, null
    UniqueConstraint (job, user) where deleted_at is null

    NOTE: user is a CustomUser, not a TenantModel, so TenantModel's
    cross-organization check does not cover it. The service that creates an
    assignment must verify the user holds an active Membership in the job's
    organization with a role in STAFF_ROLES. Test that assigning a rival
    organization's cleaner is rejected with 400.

TimeEntry
    job                 FK Job, CASCADE
    user                FK CustomUser, PROTECT
    clock_in            DateTimeField
    clock_out           DateTimeField, null
    CheckConstraint clock_out is null or clock_out > clock_in
    UniqueConstraint (job, user) where clock_out is null and deleted_at is null
        name one_open_time_entry_per_job_user
    property duration_minutes

JobNote
    job, user (FK), body TextField

JobPhoto
    job, user (FK), image ImageField upload_to "job_photos/%Y/%m/", caption CharField blank
```

**`price_cents` is snapshotted onto the Job**, never read live from `Service`.
Raising your prices must not retroactively change what last month's completed
jobs were worth. At materialization: `plan.price_override_cents` if set, else
`service.quote_cents(square_feet=location.square_feet, hours=duration/60)`.
For a job created directly through the API, `price_cents` is optional in the
payload; when omitted the serializer computes the same quote. `quote_cents`
raises `ValueError` when the pricing model's input is missing (e.g. a per-sqft
service on a location with no `square_feet`); surface that as a 400 naming the
field, never as a 500.

Register everything in the admin. Job and plan admin get `raw_id_fields` for
the FKs. Add a `scheduling/tests/factories.py` with factory-boy factories for
every model; the existing tests build rows by hand and that will not scale to
jobs with four FKs.

**Tests for this checkpoint:** `base/tests/test_models.py` Meta enforcement
passes; `test_tenancy` conformance passes once viewsets exist; a `Job` pointing
at another organization's `ServiceLocation` is rejected on save; the
`plan_occurrence` unique constraint holds; an open second `TimeEntry` for the
same job and user is rejected.

### 3a.3 — Recurrence: expansion and materialization

`scheduling/services.py`. Write the DST test **before** the implementation.

```python
MATERIALIZATION_HORIZON = timedelta(days=56)   # ~8 weeks, ADR-012

def expand_occurrences(plan, *, window_start: date, window_end: date) -> list[datetime]:
    """
    Local wall-clock occurrences of `plan` in [window_start, window_end],
    returned as aware UTC datetimes.
    """

def materialize_plan(plan, *, today: date | None = None) -> int:
    """Create missing Job rows out to the horizon. Returns count created."""

def regenerate_plan(plan) -> dict:
    """After a plan edit: drop untouched future jobs and re-materialize. See ADR-020."""
```

**The DST trap, which is the whole reason `Organization.timezone` exists:**
build `DTSTART` as a *naive* local datetime from `starts_on` and
`preferred_start_time`, expand the RRULE naive, then attach
`ZoneInfo(organization.timezone)` to each occurrence and convert to UTC.
A plan of "every Tuesday 9am" must stay 9am local across the March and
November transitions. Expanding in UTC silently shifts every job by an hour for
half the year.

The DST test, concretely: organization in `America/Denver`, plan
`FREQ=WEEKLY;BYDAY=TU` at 09:00 starting 2027-03-02. Occurrences on 2027-03-09
(MST) and 2027-03-16 (MDT) must both read 09:00 when converted back to Denver
time, and their UTC hours must be 16 and 15 respectively. Repeat across
2027-11-07 in the other direction. A wall-clock time that does not exist on the
spring-forward day (02:30) resolves forward via `fold=0`; assert and document
that rather than leaving it to chance.

`materialize_plan`:

- Window is `[max(today, starts_on), min(today + horizon, ends_on or ∞)]` in
  the organization's local date. `today` defaults to the organization's
  current local date, and is a parameter so tests can pin it.
- Builds `Job` rows with `plan_occurrence = scheduled_start = occurrence`,
  `scheduled_end = occurrence + duration`, price snapshot as above, `notes`
  copied from the plan, then `bulk_create(ignore_conflicts=True)`. The unique
  constraint on `(plan, plan_occurrence)` is what makes this idempotent; the
  task will run daily and must not duplicate jobs it already created.
  `bulk_create` bypasses `TenantModel.save()`; that is acceptable here only
  because every FK is copied from the plan, which was validated on its own
  save. Say so in a comment.
- Creates a `JobAssignment` per `default_assignees` entry on each new job.
- Inactive plans materialize nothing.

`regenerate_plan` (ADR-020): future jobs (`scheduled_start > now`) from this
plan that are **untouched** — status `SCHEDULED`, `scheduled_start ==
plan_occurrence`, and no time entries — are soft-deleted and re-created from
the plan's current definition. Every other future job is kept as-is and stays
linked. Returns `{"regenerated": n, "kept": m}` so the API can tell the
dispatcher what happened. Deactivating a plan runs the same "drop untouched
future jobs" step without the re-create.

**Tests:** the DST pair above; idempotency (materialize twice, count
unchanged); horizon respected; `ends_on` respected; `price_override_cents`
wins over the quote; default assignees are assigned; regenerate keeps a
rescheduled job and an assigned-by-hand job but replaces untouched ones;
deactivation drops untouched future jobs and nothing else.

### 3a.4 — Beat tasks

`scheduling/tasks.py` and `audit/tasks.py`. Every task takes `organization_id`
explicitly (ADR-004). Fan-out tasks iterate `Organization.objects.filter(is_active=True)`
and `.delay()` the per-organization task.

```
scheduling.materialize_all_organizations      beat: daily 02:00 UTC
scheduling.materialize_organization(organization_id)

audit.evaluate_access_reveals_all             beat: hourly
audit.evaluate_access_reveals(organization_id, local_date=None)
```

Register the schedule as `CELERY_BEAT_SCHEDULE` in `settings/base.py`;
`DatabaseScheduler` imports that dict on startup, so no data migration is
needed. Test settings already run tasks eagerly.

`evaluate_access_reveals` evaluates every unevaluated reveal whose
organization-local date is **before** the organization's current local date.
Running hourly, that is "once per day after close" for every timezone without
per-organization cron. Passing `local_date` re-evaluates that day's reveals
(flagged or not) for a manual re-run. Rules, evaluated in the organization's
timezone:

- Reveal has a `job`: flagged if `created_at < scheduled_start - buffer_before`
  or `created_at > scheduled_end + buffer_after`, using the organization's
  buffer fields. `flag_reason = "outside_job_window"`.
- Reveal has no `job` (only possible for dispatcher+ after 3a.6): flagged if
  `not organization.is_within_business_hours(created_at)`.
  `flag_reason = "outside_business_hours"`.
- `evaluated_at` is set on **every** row processed, flagged or not, so "not yet
  evaluated" and "evaluated and fine" stay distinguishable.

**Tests:** a reveal at 08:30 for a 09:00–11:00 job is fine; 07:30 is flagged;
a dispatcher reveal at 22:00 local with no job is flagged; a reveal made at
23:30 local is not evaluated until the next local day even though it is already
"yesterday" in UTC; re-running is a no-op for evaluated rows.

### 3a.5 — Jobs API

All under `/api/scheduling/`, all viewsets inherit `TenantViewSetMixin`. Wrap
every non-standard action with `@extend_schema` so 3a.0's schema test stays
green.

**`plans/`** — `RecurringPlanViewSet`, `ModelViewSet`, `IsDispatcherOrHigher`.
Filters: `customer`, `location`, `is_active`. Actions:

- `GET {id}/preview/?count=6` — the next N occurrences as local ISO datetimes
  plus their UTC equivalents, computed without persisting. This is what lets
  the UI show "this means Tue Sep 16, Sep 23…" while the user types an RRULE.
- `POST {id}/materialize/` — run `materialize_plan` now; returns the count.
- `update`/`partial_update` call `regenerate_plan` when any of `rrule`,
  `starts_on`, `ends_on`, `preferred_start_time`, `duration_minutes`,
  `location`, `service`, `price_override_cents`, or `is_active` changed, and
  include `{"regenerated": n, "kept": m}` in the response.
- `perform_create` materializes immediately so the schedule fills in without
  waiting for beat.

**`jobs/`** — `JobViewSet`, `ModelViewSet`. Queryset is role-scoped on top of
the tenant scope:

| Role | Sees |
|---|---|
| Dispatcher and above | every job in the organization |
| Cleaner | jobs with a `JobAssignment` for them |
| Customer | jobs whose `customer.user` is them |

Write permissions: create, update, destroy are `IsDispatcherOrHigher`.
`destroy` is refused (409) for a job with a time entry — cancel it instead.

Filters (`django-filter` FilterSet): `date_from` / `date_to` as
**organization-local dates**, converted to a UTC `scheduled_start` range in the
filter (the frontend never does timezone math); `status` (multi); `customer`;
`location`; `assignee` (user id); `plan`; `mine` (bool — assigned to the
caller). Ordering: `scheduled_start` default, `status`, `created_at`. Cap
`date_to - date_from` at 62 days; 400 beyond that.

The serializer nests read-only summaries so a cleaner never needs the customer
endpoints, which stay dispatcher+:

- `location`: id, label, one_line_address, access_notes, parking_notes,
  has_pets, pet_notes, has_access_codes. **Never the codes.**
- `customer`: id, display_name, phone, preferred_contact_method.
- `service`: id, name, default_duration_minutes.
- `assignments`: id, user id, user full_name, accepted_at.
- `open_time_entry`: the caller's open entry for this job, or null.

Writes take `customer`, `location`, `service` as ids. `price_cents` optional
(see 3a.2). Customers see the same shape minus `assignments`, `notes`, and
`customer.phone`.

Actions (`scheduling/services.py` holds the logic; views stay thin):

- `POST {id}/assign/ {"user": id}` and `POST {id}/unassign/ {"user": id}` —
  dispatcher+. Validates membership as in 3a.2. Assigning to a job in a
  terminal status is a 409.
- `POST {id}/status/ {"status": ..., "reason": ""}` — transitions enforced
  server-side in `services.transition_job`:

  ```
  SCHEDULED   -> EN_ROUTE, IN_PROGRESS, CANCELLED, NO_ACCESS
  EN_ROUTE    -> IN_PROGRESS, SCHEDULED, CANCELLED, NO_ACCESS
  IN_PROGRESS -> COMPLETE, NO_ACCESS
  terminal    -> SCHEDULED    (dispatcher+ only: reopen)
  ```

  Cleaners may call this only on jobs they are assigned to
  (`IsAssignedCleaner`, below) and may not set `CANCELLED`. `reason` is
  required for `CANCELLED` and `NO_ACCESS`, stored in `cancellation_reason`.
  An invalid transition is a 409 with the allowed next states in the body.
- `POST {id}/clock-in/` — creates the caller's `TimeEntry`; 409 if one is
  already open. Moves `SCHEDULED`/`EN_ROUTE` to `IN_PROGRESS` as a side
  effect. `POST {id}/clock-out/` closes it; 409 if none open. Both require
  assignment or dispatcher+.

**`time-entries/`** — read for staff (cleaners see their own), `PATCH` for
dispatcher+ to correct times. Filter by `job`, `user`, `date_from`/`date_to`.

**`job-notes/`** and **`job-photos/`** — `ModelViewSet` filtered by `job`.
Create: assigned cleaner or dispatcher+; `user` stamped from the request, never
from the payload. Delete: the author or dispatcher+. Photos are multipart;
`image` required. Customers cannot reach either.

`IsAssignedCleaner` lives in `scheduling/permissions.py` (not `base` — `base`
must not import `scheduling`). Object-level: `has_object_permission` accepts
a `Job` (assignment exists for the caller) or a `ServiceLocation` (the caller
has an assignment on a job at that location that is not in a terminal status
and whose window `[scheduled_start - 24h, scheduled_end + 24h]` contains now).
The 24h band is deliberately coarse: the permission is a gate against
"no relationship at all", the end-of-day evaluator is the fine instrument
(ADR-016, ADR-017). `has_permission` returns True for any staff member —
DRF's `|` composition only consults `has_object_permission` on operands whose
`has_permission` passed, so a cleaner must clear the first gate to reach the
object check. Superusers and dispatcher+ are admitted by the other operand.

**Tests, one file per concern** (`test_jobs_api.py`, `test_plans_api.py`,
`test_status.py`, `test_time_entries.py`, `test_notes_photos.py`):
cross-organization read is a 404; every role's list scope from the table
above; `date_from`/`date_to` converted in the organization's timezone
(a job at 23:30 Denver on the 14th appears under `date_from=14&date_to=14`
even though it is the 15th in UTC); each transition row above, plus one
illegal one returning 409; cleaner cannot cancel; clock-in twice is a 409;
customer cannot see assignments or notes; per-sqft service on a location
without square footage returns 400 naming `square_feet`.

### 3a.6 — Finish the access audit trail

`AccessReveal` already carries `evaluated_at`, `is_flagged`, `flag_reason`,
and the review trio, all unset. This completes ADR-016 under the tiering in
ADR-017.

1. `AccessReveal.job = FK("scheduling.Job", null=True, blank=True, on_delete=PROTECT)`.
   PROTECT, not SET_NULL: the row is evidence and jobs are soft-deleted anyway.
2. `ServiceLocationViewSet.get_permissions()` returns
   `[IsDispatcherOrHigher() | IsAssignedCleaner()]` for `reveal_access` instead
   of `[IsStaff()]`.
3. `RevealRequestSerializer` gains optional `job` (uuid). Resolution in
   `audit.services.record_reveal`:
   - a cleaner's reveal binds to the job that satisfied `IsAssignedCleaner`
     (if several match, the one whose `scheduled_start` is nearest now);
   - a dispatcher+ reveal binds to the supplied `job` after checking it
     belongs to this location and organization (400 otherwise), else to the
     single non-terminal job at this location within the 24h band if there is
     exactly one, else `null`.
4. The evaluator from 3a.4 now has real jobs to compare against.
5. `AccessRevealSerializer` exposes `job` and the job's `scheduled_start`.

**Tests** (extend `audit/tests/test_reveal.py`): cleaner with no assignment at
the location → 403, no row written; cleaner assigned to a job there today →
200, row's `job` set; cleaner assigned to a job there three days from now →
403; dispatcher with no job → 200, `job` null; dispatcher passing another
organization's job id → 400; the evaluator flags and clears correctly against
`job` windows.

### 3a.7 — Seed and docs

`seed_demo` grows to give each organization: three customers with a location
each (one with `square_feet`, one with pets, one with access codes), three
services (one per pricing model), two active plans (one weekly, one
biweekly), the materialized jobs, and the cleaner assigned to one plan by
`default_assignees`. Then it runs `materialize_organization` for each. Keep the
two-organization shape — tenant isolation bugs are invisible with a single
tenant.

Update `PLAN.md` (this section's status, test count, anything that surprised
you) and `CLAUDE.md`'s layout block. Regenerate the schema and skim
`/api/docs/` once against `docker compose up`.

**3a exit criteria:** full suite green, schema test green, deploy audit clean,
seed runs, and a manual pass in Swagger UI as the seeded dispatcher: create a
plan, see the preview, see the jobs appear, assign the cleaner, sign in as the
cleaner, clock in, reveal codes, clock out, complete.

### 3b — Frontend slice

Fresh scaffold in a top-level `ui/` directory (ADR-018). Do **not** copy the
source repo's `ui/` tree; port `src/plugins/axios.js` and the shape of
`src/store/user.js` by hand, nothing else.

**Scaffold**

- `npm create vuetify@latest` with the TypeScript + Pinia + Router preset,
  Node 22, dev server on port 3000 (matches `CORS_ALLOWED_ORIGINS` and
  `FRONTEND_BASE_URL` defaults). `VITE_API_BASE_URL` in `ui/.env.example`
  pointing at `http://localhost:8000`.
- Add a `ui` service to `docker-compose.yml` running the Vite dev server with
  `--host`, bind-mounting `../ui`. Production image for the UI is deferred with
  the deploy target (ADR-006).
- `npm run api:types` runs `manage.py spectacular --file` into
  `ui/openapi.yaml` then `openapi-typescript` into `ui/src/api/schema.d.ts`.
  Both files are committed; the type file is the contract the UI is written
  against (ADR-019). Regenerate whenever the backend changes and commit the
  diff with the backend change.
- Root `.pre-commit-config.yaml` gains an `eslint` hook scoped to `ui/`.
  Add `ui/` to `CLAUDE.md`'s layout and a "Frontend" command block.

**API client** (`ui/src/api/client.ts`)

- Axios instance, `withCredentials: true`, `X-CSRFToken` from the `csrftoken`
  cookie on every unsafe request, `X-Organization` from the session store on
  every request when the user has more than one membership.
- 401/403 on any call while a session is believed active → clear the store,
  route to login. 404 is **not** treated as "wrong organization"; the API
  returns 404 for cross-tenant reads by design.
- Typed helpers over the generated schema for every endpoint the slice uses.

**Auth flow** — the sequence the backend already implements:

1. App boot: `GET /api/users/session/`. Empty object → not signed in (and this
   GET is what sets the CSRF cookie, see 3a.0). Otherwise populate the store
   with the user, memberships, `current_organization`, `current_role`.
2. Login page: `POST /api/auth/login/`. `202` → route to the code page;
   `200` → session established (trusted device or no 2FA required).
3. Code page: `POST /api/auth/verify/ {code}`; on success re-fetch the session.
   The trusted-device cookie is set by the server; the UI never sees it.
4. More than one membership and no `current_organization` → organization
   picker; the choice is held in the store and sent as `X-Organization`.
5. `POST /api/users/logout/`.

**Screens, in build order.** Each is done when it works end to end against the
seeded data in Docker as the role named.

1. **Login / Verify / Organization picker** — every role.
2. **Schedule** (dispatcher+): a week view, Monday-first, in the
   organization's timezone, listing jobs per day from `jobs/?date_from&date_to`.
   Filter chips for status and assignee. Click through to job detail.
3. **Job detail** (dispatcher+ and assigned cleaner): status with the allowed
   next transitions rendered from the 409 body when a transition is refused;
   assignees with assign/unassign; location summary; notes list and add;
   photos grid and upload; **reveal codes** button that opens a confirmation
   whose copy is the `detail` string from the 400 response when
   `acknowledged` is missing (`audit.models.ACCESS_WARNING` — never
   hardcoded in the UI, so the two cannot drift), then shows the codes once.
4. **My Day** (cleaner): today's assigned jobs from `jobs/?mine=true`,
   sorted by start, with clock-in / clock-out and En route / Complete /
   No access buttons. This is the phone screen; build it at 400px first.
5. **Customers and locations** (dispatcher+): list, detail, create, edit.
   Access code fields are write-only inputs with a "codes on file" indicator
   from `has_access_codes`.
6. **Services** (admin+ edit, staff read): list and edit.
7. **Recurring plans** (dispatcher+): list, create, edit. The form calls
   `plans/{id}/preview/` (or, for a new plan, a client-side preview is
   acceptable only if it shows the same next-six list once saved) so the RRULE
   is never entered blind. Show the `regenerated`/`kept` counts after an edit.

**Not in 3b:** customer portal, billing, notifications, offline support,
push, a production build pipeline.

**Tests:** Vitest for the API client interceptors (CSRF header attached,
`X-Organization` attached only when multi-membership, 401 clears the store)
and for the session store's boot sequence. No browser automation in this phase.

**3b exit criteria:** the 3a exit-criteria walkthrough, performed in the UI
instead of Swagger, as the seeded dispatcher and then the seeded cleaner, in
both organizations, with the browser at desktop and 400px widths.

---

## Later phases

**Phase 4 — billing.** `Invoice`, `InvoiceLine`, `Payment`, `PaymentMethod`.
Ports the source repo's Stripe webhook pipeline nearly verbatim (ADR-007).
Organization subscription models come over renamed but unwired.

**Phase 5 — notifications.** Email/SMS reminders, invoice delivery, magic-link
delivery.

Open decisions are listed under **Start here** at the top of this file.
