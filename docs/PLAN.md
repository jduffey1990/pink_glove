# Build plan

Rationale for the decisions referenced here lives in `docs/DECISIONS.md`.
Invariants that hold across all phases are in `CLAUDE.md`.

---

## Start here

Everything through Phase 4c is built and gated: 4b and the staging fixes
are on `main`; **4c (the platform subscription) is on
`phase-4c-platform-subscription`, gated, waiting for Jordan to merge** --
see "Phase 4c as built" at the very end of this file, and ADR-028.
**Staging is up** at `https://pink-glove-staging.fly.dev` and CI deploys
`main` to it; Stripe Connect is configured there per `docs/DEPLOY.md`
step 9, and the subscription will be once Jordan follows step 10. Next is
**Phase 5** (notifications and the customer portal), outlined under "Later
phases": branch from `main` once 4c is merged, write its spec section in
full before code, as 4a, 4b and 4c did. Work on a branch, never on
`main`; run the phase gate in `CLAUDE.md` at the end of each phase. Read
"Phase 3b as built" before touching `ui/`, and "Phase gate — baseline": it
records what was fixed and decided before Phase 4, and a backlog to fold
in as files are touched.

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

Expect **2185 passing** in `app/`, and **427** in `ui/` (`cd ui && npm test`).
Read `CLAUDE.md` first — it has the invariants and the
testing gotchas that will otherwise cost you an hour each.

**Decisions still open, listed where they bite:**

1. ~~Deploy target~~ — **decided**: Fly.io, one image, the `ui/` build served
   from the API's origin (ADR-027). Staging is Jordan's to stand up from
   `docs/DEPLOY.md`; 4b's webhook URL is `https://<staging app>.fly.dev`.
2. ~~Stripe Connect~~ — **decided**: Connect with direct charges, plus a
   platform subscription (ADR-024). Payments are a ledger and Stripe is one
   method (ADR-025); an invoice is a snapshot (ADR-026).
3. **4c's three numbers** — still Jordan's: the monthly price (a Price in
   the Stripe dashboard, never in code), the trial length
   (`PLATFORM_TRIAL_DAYS`, built with 30) and the grace after coverage
   ends (`PLATFORM_GRACE_DAYS`, built with 14). Set in Stripe and in Fly's
   secrets per `docs/DEPLOY.md` step 10; nothing in code changes.

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, settings, Docker, test harness | **Done** |
| 1 | Tenancy core + identity | **Done** |
| 2 | Customers + service catalog | **Done** |
| 2.5 | Access audit trail | **Done** (flagging pass landed in 3a) |
| 3a | Scheduling backend, audit evaluator, OpenAPI | **Done** |
| 3b | Frontend slice (`ui/`), built against 3a | **Done** |
| 4a | Invoices, recorded payments, invoice email — no Stripe | **Done** |
| D | Deploy target, staging, CI (ADR-027) — before 4b | **Done** (staging stand-up is Jordan's; runbook written and rehearsed) |
| 4b | Stripe Connect: tenants take card payments | **Done** (merged 2026-09-23, with the staging fixes) |
| 4c | Platform subscription: tenants pay for pink_glove | **Done** (built on `phase-4c-platform-subscription` 2026-10-09, merged) |
| 5 | Notifications (reminders, SMS) + customer portal | Not started |

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

## Phase 3 — Scheduling

**3a is done** (454 tests). What the build actually turned up is recorded in
"Phase 3a as built" below, immediately after the 3a spec — read that before
3b, because four things differ from what the spec assumed.

Split in two. **3a** is the backend and shipped first. **3b** is the first
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

---

## Phase 3a as built

Delivered as specified, 149 → 454 tests. Four things differed from the spec,
and one dev-environment trap is worth knowing about.

**1. `IsDispatcherOrHigher() | IsAssignedCleaner()` does not work.** DRF
defines `|` on permission *classes* (via `OperandHolder`), not instances. The
spec's spelling raises `TypeError` at request time, not import time, so it
looks fine until the endpoint is called. The correct form, used everywhere:

```python
return [(IsDispatcherOrHigher | IsAssignedCleaner)()]
```

**2. The zero-warning schema test only means something if it reads both
caches.** drf-spectacular files "unable to guess serializer" under *error*,
not warning. A test asserting on `GENERATOR_STATS._warn_cache` alone passes
vacuously over exactly the views that need `@extend_schema`. `app/tests/
test_schema.py` reads `_warn_cache` and `_error_cache` both.

Two follow-ons: `SERVE_PERMISSIONS` has to be spelled out because spectacular
defaults to `AllowAny` rather than inheriting `DEFAULT_PERMISSION_CLASSES`;
and `ENUM_NAME_OVERRIDES` needs an entry per colliding choice set (`Role`,
plus `JobStatus`/`CustomerStatus`, which both surface as a field called
`status`).

**3. A UTC-stamped `UNTIL` in an RRULE is rejected, in favour of `ends_on`.**
Expansion is deliberately naive local wall-clock so 9am survives a DST change,
and dateutil refuses to mix a naive DTSTART with an aware UNTIL. Beyond the
library's objection, "until this UTC instant" is ambiguous against a series
defined in local time. A floating `UNTIL=20271231T000000` is accepted.

**4. Soft-deleting a job does not suppress the visit; cancelling does.**
`materialize_plan` checks *live* rows for an existing occurrence, because the
unique constraint is itself partial on `deleted_at is null` and regeneration
works by soft-deleting untouched jobs and rebuilding them (ADR-020). The
consequence is that a deleted job reappears on the next nightly run, while a
CANCELLED one does not — it is a live row at that occurrence, and it keeps the
reason on the record. This is why `destroy` refuses a job with recorded time
and tells the caller to cancel instead. Documented in `scheduling/services.py`
where it would otherwise look like a bug.

**The dev-environment trap:** `EncryptedTextField` raises when
`FIELD_ENCRYPTION_KEY` has changed since a row was written, which turns a
single stale row into a 500 on any list that includes it. If
`/api/customers/locations/` returns a 500 in a long-lived dev database, that
is the cause — the message names the field. Re-seed, or delete the row.

**Not carried out:** the live cross-tenant HTTP check in the exit-criteria
walkthrough hit the `two_factor_issue` throttle (5/hour) after repeated
sign-ins. The behaviour is covered by the suite
(`scheduling/tests/test_jobs_api.py::TestTenantIsolation`) and by the
structural conformance test; only the manual repeat was skipped.


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

## Phase 4 — Billing

Decisions are in ADR-024 (two Stripe relationships), ADR-025 (payments are a
ledger) and ADR-026 (an invoice is a snapshot). Do not re-open them here. Split
in three, each ending with the phase gate:

- **4a** — invoices and recorded payments. No Stripe anywhere. A tenant who
  never connects Stripe gets a complete product from this alone.
- **4b** — Stripe Connect. Needs a public URL for webhooks, so **Phase D comes
  first**.
- **4c** — the platform subscription.

Same rules as 3a: checkpoints in order, each ending with the full suite green,
ruff clean, and the schema regenerated in the same commit as any serializer or
view change. No checkpoint's API before its models and services have tests.
Every new endpoint gets a cross-tenant test and a test per role on each side of
its permission line — for update, delete and actions, not only list and
retrieve; that was the gap the baseline gate found everywhere.

### 4a.0 — Prep

1. New app `billing`. Enums in `billing/enums.py`, rules in
   `billing/services.py`, rows for tests from `billing/tests/factories.py`
   (an invoice needs a customer, jobs, lines and payments that all agree about
   the tenant — by hand that is a test about fixtures).
2. Fold in two backlog items this phase will otherwise trip over:
   `Organization.today()` / `local_day_bounds()` (currently computed three
   ways; invoice dates are organization-local dates), and handling
   `ConflictError` once in `app/exceptions.py` (billing raises it too).

### 4a.1 — Settings the rules read

One migration each, all defaulted so existing rows need no backfill.

```
Organization
    tax_rate_percent        DecimalField(6,3), default 0          # 8.250 = 8.25%
    no_access_fee_type      CharField NoAccessFeeType: NONE | FLAT | PERCENT, default NONE
    no_access_fee_value     DecimalField(10,3), default 0         # cents if FLAT, percent if PERCENT
    invoice_prefix          CharField(8), default "INV"
    invoice_terms_days      PositiveSmallIntegerField, default 14
    invoice_footer          TextField, blank                       # "Make checks payable to ..."

Service
    is_taxable              BooleanField, default False
```

Admin+ writes them (they are on `OrganizationSerializer` and
`ServiceSerializer` already gated that way). `Organization.no_access_fee_cents(
job_price_cents) -> int` does the arithmetic, rounding once, half up. Test:
NONE is 0, FLAT ignores the price, PERCENT rounds a half-cent up.

### 4a.2 — Models

```
InvoiceStatus   DRAFT, ISSUED, VOID
                # PAID / PARTIALLY_PAID / OVERDUE are NOT statuses. They are
                # derived from the ledger and the due date (ADR-025), exposed
                # as `payment_state`. Storing them would be a second source of
                # truth that a voided payment silently falsifies.
PaymentMethod   CASH, CHECK, MONEY_ORDER, ZELLE, CARD, OTHER
LineKind        VISIT, NO_ACCESS_FEE, ADJUSTMENT

Invoice(TenantModel)
    customer            FK Customer, PROTECT
    number              CharField, null until issued; unique per organization
    status              InvoiceStatus, default DRAFT, db_index
    issued_on           DateField, null          # organization-local date
    due_on              DateField, null
    # --- snapshot, written at issue, never recomputed (ADR-026) ---
    bill_to_name, bill_to_email, bill_to_address   (Char/Text)
    tax_rate_percent    DecimalField(6,3)
    subtotal_cents, tax_cents, total_cents         PositiveIntegerField
    notes               TextField, blank          # shown to the customer
    voided_at / voided_by / void_reason
    sent_at             DateTimeField, null
    indexes: (organization, status), (organization, customer), (organization, due_on)

InvoiceLine(TenantModel)
    invoice             FK Invoice, CASCADE, related_name="lines"
    kind                LineKind
    job                 FK Job, PROTECT, null     # null only for ADJUSTMENT
    description         CharField                 # snapshot: "Standard clean -- Tue 14 Oct"
    amount_cents        IntegerField              # negative allowed for ADJUSTMENT only
    is_taxable          BooleanField              # snapshot of Service.is_taxable
    position            PositiveSmallIntegerField
    constraint: a job appears on at most one line of a non-VOID invoice

Payment(TenantModel)
    invoice             FK Invoice, PROTECT, related_name="payments"
    method              PaymentMethod
    amount_cents        PositiveIntegerField      # applied to the invoice; > 0
    tip_cents           PositiveIntegerField, default 0   # never settles the invoice
    received_on         DateField
    reference           CharField(255), blank     # check no., Zelle confirmation
    recorded_by         FK CustomUser, PROTECT, null      # null = written by a provider
    provider            CharField, blank          # "" | "stripe"  (4b)
    provider_reference  CharField, blank, db_index
    voided_at / voided_by / void_reason
    constraint: unique (provider, provider_reference) where provider != ""

InvoiceSequence(TenantModel)
    next_number         PositiveIntegerField, default 1   # one row per organization
```

`Meta(TenantModel.Meta)` on all four (invariant 6). `Payment.delete()` and
`Invoice.delete()` on a non-draft raise, as `AccessReveal` does.

**The one-line-per-job rule has the soft-delete trap.** A partial unique
constraint sees soft-deleted lines and lines on void invoices. Enforce it in
`services`, under `select_for_update` on the job, and test: a job whose invoice
was voided can be invoiced again; a job on a live invoice cannot.

### 4a.3 — Services (`billing/services.py`)

- `billable_jobs(organization, customer=None)` — COMPLETE jobs, plus NO_ACCESS
  jobs when the organization charges a fee, that are on no live invoice line.
  This is the dispatcher's "ready to invoice" list.
- `draft_invoice(customer, jobs, actor)` — one line per job. VISIT lines take
  `job.price_cents` and the service's `is_taxable`; NO_ACCESS_FEE lines take
  `organization.no_access_fee_cents(job.price_cents)` and are skipped at 0.
  Refuses jobs of another customer, jobs already invoiced, jobs not billable.
- `reprice_line_from_time_worked(line)` — hourly services only: sum of closed
  `TimeEntry` minutes across the crew x `hourly_rate_cents`, floored at the
  service minimum like `quote_cents`. Draft only. Never automatic (ADR-026).
- `totals(invoice)` — subtotal, tax (taxable subtotal x rate, rounded once, half
  up), total. Pure; used live for drafts and once at issue.
- `issue_invoice(invoice, actor)` — takes the next number under
  `select_for_update` on `InvoiceSequence` (two dispatchers issuing at once get
  consecutive numbers, never the same one), writes the snapshot, sets
  `issued_on` to the organization's today and `due_on` from the terms. An
  invoice with no lines, or a total of 0 or less, is refused.
- `void_invoice(invoice, actor, reason)` — refused while it has live payments
  (void those first, so money never points at a void document). Frees its jobs.
- `record_payment(invoice, ...)` — issued invoices only. `amount_cents` may not
  exceed the balance; the excess is either a tip or a mistake and the caller
  must say which. `void_payment(payment, actor, reason)`.
- `payment_state(invoice)` — UNPAID | PARTIAL | PAID from live payments, and
  `is_overdue` from `due_on` against the organization's today.
- `send_invoice(invoice, actor)` — plain, readable email: lines, tax, total,
  balance, terms, the footer. Bill-to email required. Sets `sent_at`. Through a
  Celery task taking `organization_id` and `invoice_id`. No PDF in 4a; in 4b
  the same email gains a pay link.

Test the money hard: three taxable lines whose per-line tax would round to a
different sum than the invoice-level tax; a percent no-access fee landing on
half a cent; an adjustment taking the taxable subtotal below zero (tax floors
at 0); two concurrent issues; payment + void + payment.

### 4a.4 — API (`/api/billing/`)

Dispatcher and above throughout; cleaners and customers get 403 (the customer's
view of their own invoices is Phase 5).

- `invoices/` — list (filters: `customer`, `status`, `payment_state`,
  `overdue`, `issued_from`/`issued_to` as organization-local dates), retrieve,
  create (`{customer, jobs: [...]}` -> `draft_invoice`), PATCH (draft only:
  notes), DELETE (draft only).
- `invoices/{id}/lines/` — add an ADJUSTMENT, edit or remove a line. Draft only.
- Actions, each with `@extend_schema` (the schema test now fails without it):
  `issue`, `void {reason}`, `send`, `lines/{id}/reprice`.
- `invoices/billable-jobs/?customer=` — the ready-to-invoice list.
- `payments/` — list (filters: `invoice`, `method`, `received_from/to`), create,
  `{id}/void {reason}`. No PATCH, no DELETE.
- The serializer publishes `payment_state`, `balance_cents`, `is_overdue` and
  `available_actions`, so the pages draw their buttons from the server's rules
  rather than a copy (ADR-023; the same shape as `Job.next_statuses`).
- A state refusal (issuing twice, paying a draft, voiding a paid invoice) is a
  409 with `detail`; a bad amount is a 400 naming the field.

### 4a.5 — Frontend

Regenerate the contract first. Then: **Billing** (ready-to-invoice grouped by
customer; drafts; issued with balance and overdue chips), **Invoice detail**
(lines, totals, payments, the actions the server offers, record-payment dialog
with method / amount / tip / date / reference), and an **Invoices** card on the
customer page. Organization settings gain tax rate, no-access fee, prefix,
terms and footer; the service form gains "taxable". Money goes in through one
`dollarsToCents` helper in `src/lib/` with a spec — the per-square-foot rounding
bug came from doing this inline. Vitest for the helper and for any page logic
that is more than rendering.

### 4a.6 — Seed, docs, gate

`seed_demo` gains, per organization: a tax rate on one and not the other, a
no-access fee, a few issued invoices (one paid by check, one part-paid in cash
with a tip, one overdue), and a draft. Update `CLAUDE.md` (layout, any new
invariant — likely "payments and issued invoices are never edited, only
voided"), write "Phase 4a as built", run the phase gate and record it.

**4a exit criteria:** a dispatcher can take a week of completed visits for a
customer, invoice them, email the invoice, record a check against it, void a
mistaken payment and see the balance come back — and the same actions in the
other organization touch nothing in the first.

### Phase D — Deploy target, staging, CI (before 4b)

ADR-006 deferred this on purpose; 4b ends the deferral, because Stripe cannot
deliver a webhook to localhost. Decide the target, stand up a staging
environment, serve the `ui/` build, set `NUM_PROXIES` to the real proxy depth,
configure private media (the storage options are already in `base.py`), and
add a GitHub Actions workflow running pytest, ruff and the three `ui/` checks
on every push — today pre-commit is the only gate and it depends on each
checkout having installed it. Production remains Jordan's alone (`CLAUDE.md`).

### 4b — Stripe Connect

Decided in ADR-024 (Connect, direct charges, Stripe-hosted onboarding, an
application fee that ships at 0%) and ADR-025 (a Stripe payment is a
`Payment` row a webhook wrote). Do not re-open them here. Staging is up
(2026-09-22, `https://pink-glove-staging.fly.dev`), so the webhook has a
public URL. Same rules as 4a: checkpoints in order, suite green and schema
regenerated at each, tests before the API, a cross-tenant test and a test
per role on each side of every permission line.

**Stripe account type: Standard.** The tenant owns a real Stripe account
with its own dashboard, its own payouts and Stripe's own support; the
platform holds an OAuth-style link to it and nothing else. This is what
"the tenant's own Stripe dashboard" in ADR-024 means, and it is the only
type on which a direct charge leaves the platform out of the chargeback.
Express would look smoother and put every dispute back on the platform.

**Stripe is optional on the server as well as per tenant.** With
`STRIPE_SECRET_KEY` unset the product is exactly 4a: the connect button is
absent, the pay endpoints answer 503, the webhook answers 503, and nothing
imports the `stripe` package at startup. Staging deployed before there was
a Stripe account, and production must be able to as well.

#### 4b.0 — Prep

1. Dependency `stripe` (pin the current major -- the source repo is on 11
   and the legacy `stripe.error.*` namespace; use the modern
   `stripe.SignatureVerificationError`; `STRIPE_API_VERSION` pinned in
   settings and set on the client, so a Stripe dashboard upgrade cannot
   change payload shapes underneath us). Settings, all from env, none
   required: `STRIPE_SECRET_KEY`, `STRIPE_CONNECT_WEBHOOK_SECRET`,
   `STRIPE_APPLICATION_FEE_PERCENT` (Decimal, default 0), and the derived
   `STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)`. Document all four in
   `.env.example`. Production refuses to start with a key but no webhook
   secret -- a half-configured Stripe accepts payments it never hears about.
2. `billing/connect.py` -- the one module that talks to Stripe (accounts,
   account links, checkout sessions, balance transactions), every call taking
   `stripe_account=` explicitly. `billing/webhook.py` -- the endpoint, the
   ledger write, and the dispatch table. Nothing else imports `stripe`.
   (Not `billing/stripe.py`: it would shadow the package.) The dependency
   direction holds -- `billing` still imports `organizations` and
   `scheduling`, nothing imports `billing`.
3. Test fixtures in `billing/tests/stripe_fixtures.py`: event payloads
   built from Stripe's documented shapes, and a `signed(payload, secret)`
   helper that computes the `Stripe-Signature` header the way the SDK
   does. Tests never reach the network: `stripe` is patched at the
   `billing.connect` boundary and the webhook is fed signed bytes.

#### 4b.1 — Models

```
Organization
    stripe_account_id         CharField(64), blank, unique where != ""
    stripe_charges_enabled    BooleanField, default False   # mirrors Account.charges_enabled
    stripe_details_submitted  BooleanField, default False   # mirrors Account.details_submitted
    stripe_connected_at       DateTimeField, null
    # Written only by billing.connect and the account.updated handler --
    # never from request data. On OrganizationSerializer read-only, as one
    # nested `stripe: {connected, charges_enabled, details_submitted}` so the
    # settings page has one thing to look at.

StripeEvent(Base)                       # NOT TenantModel -- see below
    event_id        CharField(255)
    account         CharField(64), blank  # "" for a platform event (4c)
    type            CharField(100), db_index
    organization    FK Organization, null, PROTECT
    payload         JSONField
    status          StripeEventStatus: RECEIVED | PROCESSED | IGNORED | FAILED
    error           TextField, blank
    processed_at    DateTimeField, null
    unique (event_id, account); index (organization, type)

Payment (additions)
    fee_cents       PositiveIntegerField, null   # from the balance transaction; null until known
    disputed_at     DateTimeField, null
    dispute_status  CharField(32), blank         # Stripe's word: needs_response, won, lost, ...
```

`StripeEvent` is the one model in the phase that is not a `TenantModel`,
deliberately: an event for an account no organization owns must be ledgered
and dropped (ADR-024), so `organization` is nullable. It is never served by
an API -- admin only, read-only -- so there is no view to scope. Record it
in the gate as the invariant-1 exception it is.

The `Payment` unique constraint on `(provider, provider_reference)` from 4a
is the webhook's idempotency for money; the `StripeEvent` unique constraint
is the idempotency for events. Both are needed: Stripe retries an event
under the same id, and a `payment_intent.succeeded` and a
`checkout.session.completed` can each describe the same PaymentIntent.

#### 4b.2 — Connect services (`billing/connect.py`)

- `start_onboarding(organization, *, actor) -> str` -- creates the Standard
  account on first call (`stripe.Account.create(type="standard",
  email=organization.email, metadata={organization_id})`) and stores
  `stripe_account_id`; every call returns a fresh Account Link URL
  (`type="account_onboarding"`, `refresh_url` and `return_url` under
  `FRONTEND_BASE_URL/billing/settings`). Refused (409) when charges are
  already enabled. Owner only: connecting binds the business to Stripe's
  terms, which an admin does not sign.
- `refresh_account(organization) -> Organization` -- fetches the Account
  and copies `charges_enabled` / `details_submitted`; stamps
  `stripe_connected_at` the first time charges are enabled. Called on
  return from onboarding and by the `account.updated` handler, so the
  status is right whether the webhook or the browser arrives first.
- `pay_token(invoice) -> str` / `invoice_from_pay_token(token) -> Invoice`
  -- `django.core.signing` with its own salt and a 120-day `max_age`
  (terms are 14 to 30 days; the link in an email must outlive a slow
  payer). Carries the invoice id only; everything else is looked up. The
  invoice is the customer's own, and the amounts on it are what the email
  already said, so a token in an email is no wider a disclosure than the
  email.
- `payable(invoice) -> str | None` -- why an invoice cannot be paid by
  card right now, or `None`: Stripe not enabled on the server, the
  organization not charges-enabled, invoice not issued, balance zero. The
  pay page shows the reason; the email omits the button on any of them.
- `create_checkout_session(invoice) -> str` -- on the connected account
  (`stripe_account=`), `mode="payment"`, one line item named
  "Invoice {number} from {organization}", `unit_amount` = the balance at
  this moment, `currency="usd"`, `customer_email` = bill-to, `expires_at`
  30 minutes out (the shortest Stripe allows), `client_reference_id` =
  invoice id, `metadata={organization_id, invoice_id}`,
  `payment_intent_data.metadata` the same (the PaymentIntent is what the
  refund and dispute events name, and it does not inherit the session's
  metadata), `application_fee_amount` from
  `STRIPE_APPLICATION_FEE_PERCENT` rounded half up and omitted at 0,
  `success_url` / `cancel_url` back to the pay page with `?paid=1` /
  `?cancelled=1`. Returns the session URL. Never stored: a session is
  30 minutes of intent, not a record, and the webhook carries everything.
- `fee_cents_for(payment_intent_id, *, stripe_account) -> int | None` --
  the charge's balance transaction's `fee`, read from Stripe; never
  computed from a published rate (the outline's rule).

Test each with `stripe` patched: the account is created once and never
twice; the token round-trips and a tampered or expired one is refused;
`payable` returns each reason; the session carries the metadata on both
objects and the fee is absent at 0% and rounded at 2.9%.

#### 4b.3 — The webhook (`billing/webhook.py`, `billing/tasks.py`)

The source repo's pipeline was read before this was written
(`app/billing/tasks.py` and `views.py` there, uncommitted): the shape to
keep is *verify, enqueue, 200, everything else in the worker*. The rest is
not to be copied -- its ledger has no migration, ships the whole payload
over Redis, marks an event processed before handling it (so a handler
failure plus a retry is a permanent silent drop), reads the secret at
import time, and has no tests, no Connect and no refund handling.

`POST /api/billing/stripe/webhook/` -- a plain Django `View`, CSRF-exempt,
outside DRF and the schema (nothing in `ui/` calls it; list it in the gate
as the deliberate public route it is), so `request.body` reaches
`stripe.Webhook.construct_event` as the raw bytes that were signed. The
secret is read from settings inside the request, never at import, so a
test can override it and 4c can add a second endpoint with its own. A bad
signature is a 400, logged without the body. Then, in the request and in
one transaction:

1. Insert the `StripeEvent` on `(event_id, account)`; if it already exists,
   answer 200 and stop -- Stripe is retrying, and the first delivery owns
   it.
2. Resolve `event.account` to the organization with that
   `stripe_account_id`. None: status `IGNORED`, 200, stop. Never guessed at.
3. A type with no handler: `IGNORED`, 200, stop. (Register only what is
   handled; Stripe sends dozens of types.)
4. `process_stripe_event.delay(organization_id, stripe_event.id)`; 200.

Everything after the 200 happens in the task, so Stripe's 30-second
timeout is never near. The task takes `organization_id` (invariant 3),
loads the event scoped by it, dispatches on `type`, and writes
`PROCESSED` or `FAILED` with the error text. A `FAILED` event is retried by
Celery three times with backoff, then left `FAILED` for a person -- the
admin list is the queue. Handlers, each idempotent because both ledgers
are:

| Event | Effect |
|---|---|
| `checkout.session.completed` (payment_status `paid`) | `record_payment(method=CARD, provider="stripe", provider_reference=payment_intent, amount=amount_total, received_on=organization.today(), reference=charge id)`, then `fee_cents` from the balance transaction. A second event for the same PaymentIntent hits the unique constraint and is a no-op. |
| `charge.refunded` | Fully refunded: `void_payment(reason="Refunded in Stripe: {refund id}")`. Partially: void it and record a new payment for the net amount with `provider_reference="{pi}:{refund id}"` -- the ledger cannot shrink a row (ADR-025) and must not lie about the balance. |
| `charge.dispute.created` | `disputed_at`, `dispute_status`. Nothing else: the money is held by Stripe, not gone. |
| `charge.dispute.closed` | `dispute_status`; lost → `void_payment(reason="Dispute lost: {dispute id}")`. |
| `account.updated` | `refresh_account`, which re-fetches the Account. Not from the event's object: Stripe does not promise order, and a stale `account.updated` applied last would switch charges off after they came on. |

A `FAILED` event is re-run from the admin (a "replay" action that re-queues
the task); the ledger row and its payload are what make replay possible,
which is why the payload is stored in the request and never shipped over
the broker. The task is handed ids only.

**Overpayment by card is possible and is recorded as it happened.** The
session charged the balance at creation; a check recorded in the thirty
minutes after makes the card an overpayment. `record_payment`'s balance
check is for people typing amounts. The provider path takes what Stripe
says was paid, `balance_cents` already floors at zero, and the invoice
gains `overpaid_cents` so the page can say "overpaid by $40 -- refund from
your Stripe dashboard". Refusing the row would be a ledger that disagrees
with the bank.

Test the pipeline end to end with signed bytes: replayed event → one
payment; unknown account → `IGNORED`, no payment, 200; bad signature →
400, no ledger row; an event whose account belongs to organization B never
touches organization A's invoice even when the metadata claims it (the
organization comes from `account`, the invoice is then looked up *within*
it, and a mismatch is `FAILED` with a clear error); each handler's effect;
`FAILED` leaves the event for retry and the payment unwritten.

#### 4b.4 — API and the email

- `POST /api/billing/stripe/connect/` (owner) → `{url}` from
  `start_onboarding`. `POST /api/billing/stripe/refresh/` (admin+) →
  the organization's `stripe` block after `refresh_account`.
- `GET /api/billing/pay/{token}/` (`AllowAny`, throttled as the magic-link
  endpoints are) → `PublicInvoiceSerializer`: organization name, number,
  issued/due dates, lines, subtotal, tax, total, paid, balance,
  `payable_reason`. Nothing about the customer beyond the bill-to name.
  `POST /api/billing/pay/{token}/checkout/` → `{url}`; 409 with the
  reason when not payable.
- `InvoiceSerializer` gains `pay_url` (staff copy it into a text message)
  and `overpaid_cents`; `PaymentSerializer` gains `fee_cents`,
  `disputed_at`, `dispute_status`. `available_actions` is unchanged: a
  Stripe payment is voided by a refund event, not a button, so the void
  action stays hidden for `provider != ""` -- the server owns that rule.
- The invoice email gains a "Pay online" button when `payable(invoice)` is
  `None`, linking to `FRONTEND_BASE_URL/pay/{token}`. Otherwise the email
  is byte-for-byte 4a's.
- Every new `@action` carries `@extend_schema`; regenerate the contract.

#### 4b.5 — Frontend

- **Billing settings** gains a "Card payments" card: not connected →
  "Connect with Stripe" (owner only; others see who can); connected but
  not charges-enabled → "Finish setting up with Stripe" (same call, new
  link); enabled → "Connected", the application fee if non-zero, and a
  link to `dashboard.stripe.com`. On return from Stripe (`?stripe=return`)
  the page calls refresh once and shows the result. Owner-only is one more
  place the role tiers are mirrored; add it to the coupling list.
- **Invoice detail**: "Copy pay link"; the payments list marks a card
  payment with its fee and a dispute chip; an overpaid invoice says so.
- **`/pay/:token`** -- `PayInvoicePage.vue`, `access: 'public'`, no
  session, no organization header. Shows the public serializer's summary
  and one button, "Pay {balance} by card", which posts for a session and
  sets `window.location`. Back with `?paid=1` it polls the GET every two
  seconds for up to thirty, because the webhook races the redirect, and
  then says "Payment received" or "Your payment is processing -- you will
  get a receipt from Stripe". `?cancelled=1` just shows the invoice again.
  Money through `src/lib/money.ts`; dates through `datetime.ts` -- but the
  public page has no session, so the serializer sends the dates already
  formatted in the organization's zone rather than the page guessing.
- Vitest for the poll (stops on paid, stops at thirty seconds) and for the
  settings card's three states.

#### 4b.6 — Deploy, seed, docs, gate

- `docs/DEPLOY.md` gains a Stripe section: create the platform account,
  turn on Connect, register the webhook endpoint
  (`https://<app>.fly.dev/api/billing/stripe/webhook/`, **"events on
  connected accounts"**, the five types above), set the two secrets with
  `fly secrets set`, and test with `4242 4242 4242 4242` against the
  seeded overdue invoice. Staging and production are different Stripe
  accounts (test mode and live mode of the same one is acceptable) with
  different webhook secrets. Locally: `stripe listen
  --forward-connect-to localhost:8000/api/billing/stripe/webhook/`.
- `seed_demo` changes nothing: there is no Stripe account to seed, and a
  fake `stripe_account_id` would make the settings page lie.
- `CLAUDE.md`: `billing/connect.py` and `webhook.py` in the layout; the
  owner-only tier in the coupling list; the `StripeEvent` exception under
  invariant 1. Write "Phase 4b as built", run the gate, record it.

**4b exit criteria:** on staging, a dispatcher issues and emails an invoice;
the customer opens the email, pays with the test card, and within seconds
the dispatcher's page shows it paid with the real Stripe fee beside the
payment; a refund from the tenant's Stripe dashboard puts the balance back;
and the other organization, which never connected Stripe, sees no button,
no link, and no payment.

### 4c — Platform subscription

Decided in ADR-024: Stripe Billing on the **platform** account, a separate
webhook endpoint with its own secret, and a lapse that ends in
`Organization.is_active = False`, which is already read-only (baseline gate,
decision 2). Do not re-open those. This section decides the rest: how a
tenant comes to be covered, what a lapse is, how long the grace is, and
what the people inside a lapsed organization see. Same rules as 4a and 4b:
checkpoints in order, suite green and schema regenerated at each, tests
before the API, a cross-tenant test and a test per role on each side of
every permission line.

**The shape, in one paragraph.** A new organization starts on an
*application-side* trial with no card: `trial_ends_at` is stamped when the
row is created, and nothing in Stripe exists yet. The owner subscribes
through Stripe Checkout in subscription mode on the platform account, which
creates the Stripe Customer and the Subscription; everything after that --
the card, the receipts, cancelling -- is Stripe's Customer Portal, not pages
here. The organization mirrors the subscription's state (status, the end
of the last *paid* period, cancel-at-period-end), written only by
`billing/subscription.py` and the platform webhook, and always by
re-reading the Subscription rather than copying from an event. From those
fields one rule computes `covered_until`; `PLATFORM_GRACE_DAYS` after it
passes, a daily task sets `is_active = False`, and a subscription that
comes good turns it back on. The server publishes one word for the state
and one sentence for the banner (ADR-023), and the owner can still reach
the subscribe and portal endpoints while the organization is read-only --
the one deliberate hole in the read-only rule, because someone has to be
able to pay.

**Decisions this section makes, with what was rejected.**

- **A flat monthly price per organization, held in Stripe.** The amount
  is a Price in the dashboard and `STRIPE_PLATFORM_PRICE_ID` names it;
  the code never knows the number. *Rejected -- per-seat pricing:* a
  cleaning company's headcount moves weekly and a one-person platform
  does not want to police seats. *Rejected -- the price in code or
  settings:* changing it would be a deploy, and Stripe already versions
  prices.
- **The trial is ours, not Stripe's.** `trial_ends_at` on the row, set at
  creation to `PLATFORM_TRIAL_DAYS` out (default 30), extended by hand in
  the admin when a pilot needs longer. A trial with no card and no Stripe
  object is what ADR-024 means by "needed before anyone is charged, not
  before a pilot tenant can use the product". *Rejected -- Checkout with
  `trial_period_days`:* it puts the card form in front of a pilot on day
  one, and a trial that ends in Stripe but was never started here is a
  lapse the app cannot explain.
- **Coverage is a date, and the grace runs from it.** `covered_until` is
  the latest of the trial end and the end of the last period Stripe was
  *paid* for; `PLATFORM_GRACE_DAYS` (default 14) after it the organization
  is read-only. One rule for every lapse: an expired trial nobody
  subscribed after, a renewal whose card failed, a subscription the owner
  cancelled. *Rejected -- deactivating on Stripe's `past_due` or
  `canceled` directly:* Stripe's retry schedule and its "what to do when
  retries fail" setting are dashboard configuration, and `past_due`
  arrives the morning a card fails, which is no time to lock a dispatcher
  out of the day's schedule. *Rejected -- computing read-only-ness per
  request from the dates:* `is_active` is already the chokepoint
  `IsOrgMember` reads with no extra query, and the admin flag already
  means "read-only" to the rest of the product; a second meaning for the
  same state is a bug factory.
- **Deactivation by billing is reversible only by billing; deactivation
  by hand is not touched.** `deactivated_for_billing_at` records which
  it was. A subscription coming good reactivates an organization *that
  billing deactivated*; one Jordan switched off in the admin stays off
  whatever Stripe says.
- **Stripe's Customer Portal, not our own card pages.** Updating a card,
  reading receipts, cancelling, and un-cancelling before the period ends
  are the portal's. The product gets two buttons and no PCI surface.
- **A deactivated tenant's customers can still pay it.** The pay page and
  its checkout are public, by token, and the webhook writes the payment
  regardless of `is_active`: the money is the tenant's, not ours, and an
  invoice already sent is a document their customer is entitled to settle.
  Cash and checks are recorded through a write the tenant cannot make
  until they are current -- that is the read-only rule working as
  intended, and the banner says so.
- **Billing is optional on the server, like Connect.** With no
  `STRIPE_PLATFORM_PRICE_ID` nobody is ever deactivated for billing, the
  subscription card is absent, and the banner has nothing to say: staging
  deploys without a price, and the product is exactly 4b. Production
  refuses to start with a price but no platform webhook secret, for 4b's
  reason -- a subscription whose lapse is never heard about is a free
  product with a card form.

#### 4c.0 — Prep

1. Settings, all from env, none required: `STRIPE_PLATFORM_PRICE_ID`,
   `STRIPE_PLATFORM_WEBHOOK_SECRET`, `PLATFORM_TRIAL_DAYS` (int, 30),
   `PLATFORM_GRACE_DAYS` (int, 14), and the derived
   `PLATFORM_BILLING_ENABLED = STRIPE_ENABLED and bool(STRIPE_PLATFORM_PRICE_ID)`.
   Document them in `.env.example` under their own heading. `production.py`
   refuses a price id without the platform secret, beside the 4b check.
   `bin/deploy-audit.sh` needs nothing: nothing new is required.
2. `billing/subscription.py` -- the one module that talks to Stripe on the
   platform's own behalf: customers, subscription Checkout, portal
   sessions, and reading a Subscription back. It reuses `connect.client()`
   and `connect.stripe_errors()` and never passes `stripe_account=` --
   that absence is the whole difference between the two modules, and a
   comment at the top says so. (Not `billing/platform.py`: it would
   shadow the standard library's `platform`, and `ruff` would say so.)
   The dependency direction holds -- `billing` imports `organizations`;
   nothing imports `billing`. The coverage rule itself lives in
   `organizations/services.py`, because `OrganizationSerializer` has to
   publish the result and `organizations` must not import `billing`.
3. `ADR-028` in `docs/DECISIONS.md`: the five decisions above, with their
   rejections, so the next person who wants to "simplify" the trial into
   Stripe's reads why it is not there.
4. Fixtures in `billing/tests/stripe_fixtures.py`: platform event payloads
   (`customer.subscription.*`, `invoice.paid`, `invoice.payment_failed`,
   a subscription-mode `checkout.session.completed`) with no `account`
   key, a Subscription object in the pinned API version's shape (the
   period is on the *item* -- see 4c.2), and the existing `signed()`
   helper with the platform secret. `billing/tests/conftest.py` gains
   `platform_billing_on` beside `stripe_on`.

#### 4c.1 — Models

```
Organization
    stripe_customer_id            CharField(255), blank (exists; gains a partial unique
                                  constraint where != "", like stripe_account_id)
    stripe_subscription_id        CharField(255), blank (exists)
    subscription_status           CharField(32), blank      # Stripe's word, verbatim
    subscription_paid_through     DateTimeField, null       # end of the last PAID period
    subscription_cancel_at_period_end  BooleanField, default False
    trial_ends_at                 DateTimeField, null       # stamped on create; admin may extend
    billing_exempt                BooleanField, default False  # comped; admin only, never on the API as writable
    deactivated_for_billing_at    DateTimeField, null       # set by the coverage rule, cleared by it
```

`trial_ends_at` is stamped in `save()` on first save, like `slug`, when it
is null and the row is not exempt: `created + PLATFORM_TRIAL_DAYS`. The
migration gives every existing row a trial from the migration's own
moment, so staging's demo organizations and anything created before 4c are
covered for a month from the deploy and nobody wakes up read-only. The
`stripe_customer_id` constraint matches the account one: a platform event
resolves its `customer` to exactly one tenant, and the database refuses
two.

The Connect and subscription fields stay on `Organization` rather than a
`Subscription` model. There is one subscription per tenant, the rule reads
four fields, and a join for four fields is a join for its own sake; the
day there are two products or a history worth keeping, the table appears
with a migration that copies these over.

`organizations/enums.py` gains `BillingState` -- the one word the pages
switch on:

| state | meaning |
|---|---|
| `exempt` | comped, or platform billing is not configured on the server |
| `trial` | the trial is running and there is no live subscription |
| `active` | Stripe says `active` or `trialing`; paid through a future date |
| `past_due` | Stripe says `past_due`: a renewal failed and Stripe is retrying |
| `lapsed` | coverage has ended and the grace is running |
| `deactivated` | `is_active` is false, for billing or by hand |

`organizations/services.py` (new) holds the rule, pure functions of an
organization and a `now`, so every path -- the serializer, the task, the
webhook -- gets the same answer:

- `covered_until(organization) -> datetime | None` -- `None` means
  indefinitely (exempt, or billing not enabled); otherwise the later of
  `trial_ends_at` and `subscription_paid_through`.
- `grace_ends_at(organization)` -- `covered_until + PLATFORM_GRACE_DAYS`,
  or `None`.
- `billing_state(organization, now) -> BillingState`.
- `billing_notice(organization, role, now) -> (level, text) | None` --
  the banner's sentence, or nothing. `level` is `info`, `warning` or
  `error`. Nothing during a trial until seven days remain ("Your free
  trial ends on {date}. Subscribe to keep scheduling after that."), then
  warning; `active` with cancel-at-period-end is info ("Your subscription
  ends on {date}."); `past_due` and `lapsed` are warning, and name the
  grace end ("Subscribe by {date} to keep scheduling"); `deactivated` is
  error, and says whether it was billing ("read-only because its
  subscription lapsed on {date}") or not ("This organization has been
  deactivated." -- the sentence the baseline gate owed). A non-owner's
  sentence ends "Ask the organization owner to ..." rather than telling
  them to do something they cannot. Dates are organization-local, through
  `Organization.tz`.
- `available_actions(organization) -> list[str]` -- `subscribe` when
  billing is enabled, the organization is not exempt and there is no live
  subscription (status not in `active`, `trialing`, `past_due`); `manage`
  when a Stripe customer exists. The role is the permission layer's
  business, as it is for Connect.
- `apply_coverage(organization, now) -> bool` -- the one place `is_active`
  moves for billing. Past the grace and still active: `is_active = False`,
  `deactivated_for_billing_at = now`. Covered and
  `deactivated_for_billing_at` set: `is_active = True`, clear the stamp.
  Returns whether anything changed. Never touches an organization that is
  inactive with the stamp null -- that one was switched off by a person.
  Saves only the fields it changed.

Test the rule by table: each state from each combination of the fields;
the grace boundary on both sides of midnight in the organization's zone;
exempt and not-enabled never lapse; a manual deactivation survives a paid
subscription; the stamp round-trips; the notice per role.

#### 4c.2 — Subscription services (`billing/subscription.py`)

- `start_checkout(organization, *, actor) -> str` -- under the
  organization's row lock, creates the Stripe Customer on first call
  (`name`, `email`, `metadata={organization_id}`) and stores
  `stripe_customer_id`; then a Checkout Session on the platform account:
  `mode="subscription"`, `customer=` the stored id, one line item of
  `STRIPE_PLATFORM_PRICE_ID` quantity 1, `client_reference_id` =
  organization id, `metadata={organization_id}` on the session and in
  `subscription_data.metadata` (the Subscription is what later events name,
  and it does not inherit the session's), `success_url` and `cancel_url`
  to `FRONTEND_BASE_URL/billing/settings?subscription=return|cancelled`.
  Returns the URL. Refused with a 409 when a live subscription exists --
  the portal is the door then. Owner only: subscribing binds the business
  to a recurring charge.
- `portal_url(organization) -> str` -- a Billing Portal session for the
  stored customer, `return_url` the settings page. 409 with no customer.
  Owner only. The portal's features (update card, cancel at period end,
  invoice history; no plan switching, there is one plan) are dashboard
  configuration, written down in the runbook.
- `refresh_subscription(organization) -> Organization` -- re-reads the
  Subscription (`expand=["latest_invoice"]`) and copies four things:
  `subscription_status`; `subscription_cancel_at_period_end`;
  `subscription_paid_through`, which is the item's `current_period_end`
  when the latest invoice is `paid` or the status is `trialing`, and the
  item's `current_period_start` otherwise -- **on the pinned API version
  the period lives on `items.data[0]`, not on the Subscription**, and a
  renewal Stripe is still trying to collect has already advanced the
  period, so "the period end" alone would hand a failed card a free
  month; and `stripe_subscription_id` when the row has none yet (the
  checkout event races the subscription events). Then `apply_coverage`,
  so a payment that comes good reactivates in the same call that learned
  of it. With no subscription id, a no-op. Called on return from
  Checkout, by every platform handler, and by the daily task.

Test each with `stripe` patched: the customer is created once; the
session carries the metadata on both objects and the price from settings;
a 409 when live; the four fields from a paid, a failed-renewal, a
trialing and a cancel-at-period-end Subscription, read off the item; the
reactivation happens inside `refresh_subscription`.

#### 4c.3 — The platform webhook and the daily task

`POST /api/billing/stripe/platform-webhook/` -- a second instance of the
4b view with the platform secret and a different resolver, not a second
copy of it: `StripeWebhookView` gains `secret_setting` and `resolve` class
attributes, and `receive()` takes the resolver. Everything else -- raw
bytes to `construct_event`, the 400 without the body logged, the ledger
insert on `(event_id, account)`, IGNORED for what nobody owns or handles,
enqueue ids only, 200 -- is the same code. The platform resolver reads
`data.object.customer` and finds the organization with that
`stripe_customer_id`; an event with an `account` at this endpoint, or with
no customer, or with a customer nobody holds, is IGNORED, never guessed at.
The Connect endpoint already ignores an event with no account; now the
two are mirror images and a test sends each endpoint the other's event.

`process()` picks the table by the row: `account == ""` is a platform
event and dispatches on `PLATFORM_HANDLERS`; otherwise `HANDLERS` as
before. `checkout.session.completed` is in both tables with different
handlers, and this is why the dispatch keys on the row and not the type.
Every platform handler is the same two lines -- `refresh_subscription`,
which re-reads and applies -- because Stripe does not promise order and
the Subscription is the truth:

| Event | Effect |
|---|---|
| `checkout.session.completed` (`mode == "subscription"`) | store `session.subscription` as `stripe_subscription_id` if the row has none, then refresh |
| `customer.subscription.created` / `updated` / `deleted` | refresh |
| `invoice.paid` / `invoice.payment_failed` | refresh (the handler is what makes a card coming good reactivate within seconds rather than at 3am) |

Six types, registered on the platform endpoint ("events on your account",
not connected accounts), listed in the runbook and nowhere else.

`billing.enforce_platform_billing_all` runs from beat daily at 01:00 UTC,
an hour before the materializer so a reactivated tenant is filled the same night,
and fans out `billing.enforce_platform_billing(organization_id)` for
every organization, inactive ones included (reactivation is its job too).
Each re-reads the subscription from Stripe when there is one -- a call per
tenant per day, so a webhook lost the way 4b's first payment was lost
costs a day, not a tenant -- and then `apply_coverage`. With billing not
enabled it does nothing. The sweep is unchanged; platform rows are swept
like Connect rows because the sweep reads the ledger, not the type.

**What deactivation stops.** `scheduling.materialize_all_organizations`
and `audit.evaluate_access_reveals_all` already skip inactive
organizations: a read-only tenant stops materializing jobs and stops
evaluating reveals (it cannot make any). Reactivation resumes both at the
next beat, and the materializer's horizon catches up on its own. Say so in
a comment where each filters on `is_active`.

Test the pipeline end to end with signed bytes against the platform
secret: a Connect-secret signature is a 400 here; a replay is one row;
an event with an `account` is IGNORED; a customer nobody holds is
IGNORED; organization B's customer never touches organization A even when
the metadata names it; each handler refreshes; a payment event on a
billing-deactivated organization reactivates it, and on a hand-deactivated
one does not. The task: `.apply()`ed once, skips without a subscription,
is a no-op with billing disabled.

#### 4c.4 — API

- `OrganizationSerializer` gains `billing`, read-only, from a
  `PlatformBillingSerializer` in `organizations/serializers.py`: `state`,
  `level`, `notice`, `trial_ends_at`, `covered_until`, `grace_ends_at`,
  `status` (Stripe's word), `cancel_at_period_end`, `exempt`,
  `available_actions`. `OrganizationSummarySerializer` -- the one on the
  session payload and the membership list -- gains `is_active` and a
  `billing` block of `state`, `level` and `notice` only, so the shell can
  draw the banner from the session it already has. Both tailor `notice` to
  the caller's role from the serializer context (the membership is on the
  request; on the membership list it is the row's).
- `POST /api/billing/subscription/checkout/` (owner) → `{url}`.
  `POST /api/billing/subscription/portal/` (owner) → `{url}`.
  `POST /api/billing/subscription/refresh/` (admin+) → the `billing` block
  after `refresh_subscription`; the settings page calls it on return from
  Checkout. Views in `billing/views.py` beside the Connect views, reaching
  `organizations.serializers.PlatformBillingSerializer` the narrow way
  `StripeStatusSerializer` is reached.
- **All three carry `allowed_while_inactive = True`**, a view attribute
  `IsOrgMember` reads before refusing an unsafe method on a deactivated
  organization. It is the only exemption, it is explicit on the view, and
  the gate lists every view that sets it. Without it the owner of a
  lapsed organization could see the bill and not pay it.
- Every new view carries `@extend_schema`; regenerate the contract.

Tests: each endpoint per role on both sides (owner / admin / dispatcher /
cleaner / customer / anonymous), the 503 with Stripe off, the 409s, and
the three endpoints answering on an inactive organization while a
`PATCH /api/organizations/current/` beside them is still refused.

#### 4c.5 — Frontend

- **The banner**, the thing the baseline gate left undone. `App.vue`
  renders one `v-alert` above the router view when
  `session.organization.billing.notice` is set, coloured by `level`, with
  a "Billing settings" link for admin and above. The copy is the server's
  sentence, never composed here (ADR-023). A pure
  `src/lib/billingBanner.ts` decides "show, with what colour and link"
  from the summary and the role, with a spec. The session store exposes
  `isActive` and refreshes after the settings page's refresh call, so the
  banner changes in the same navigation that fixed it.
- **Billing settings** gains a "Your pink glove subscription" card that
  switches on `billing.state`, built by `src/lib/subscriptionCard.ts` the
  way `stripeCard.ts` builds the Connect card: title, text, which of
  `subscribe` / `manage` to offer from `available_actions` and
  `session.isOwner`, with the owner-only note for everyone else. The
  Subscribe button posts for a checkout URL and sets `window.location`;
  Manage does the same with the portal. On `?subscription=return` the page
  calls refresh once, then `session.refresh()`, and drops the query, as
  the Connect card does on `?stripe=return`. `?subscription=cancelled`
  shows the card unchanged. The card is absent when `state` is `exempt`
  and billing is not configured, and says "Complimentary" when it is
  exempt by hand.
- Nothing is added to the router; the query keys join the coupling list
  beside Connect's.
- Vitest: `subscriptionCard.spec.ts` for every state and both roles;
  `billingBanner.spec.ts`; the session store's `isActive`.

#### 4c.6 — Deploy, seed, docs, gate

- `docs/DEPLOY.md` step 10: in the same sandbox as step 9, create the
  Product and its monthly Price and copy the `price_…` id; register a
  *second* endpoint at `/api/billing/stripe/platform-webhook/` listening
  to **events on your account** (step 9's is on connected accounts; the
  header on each endpoint's page says which) with the six types; set
  `STRIPE_PLATFORM_PRICE_ID` and `STRIPE_PLATFORM_WEBHOOK_SECRET`;
  configure the Customer Portal once (Settings → Billing → Customer
  portal: update payment method, cancel at period end, invoice history;
  no plan changes) and the subscription retry setting (Settings → Billing
  → Subscriptions: when all retries fail, *cancel the subscription* -- the
  app's grace is independent of it, and `unpaid` would be a status with
  no end). Locally, `stripe listen --forward-to localhost:8000/…` for
  this endpoint and `--forward-connect-to` for the other; both at once
  is one command with both flags. Test cards: `4242…` subscribes;
  `4000 0000 0000 0341` attaches and then fails the renewal, which the
  dashboard's clock can be advanced to force. A "when the banner is wrong"
  section, in the shape of 4b's "when a payment does not show up": is the
  event there, did it arrive, did the worker apply it, and what the daily
  task would do at 01:00 regardless.
- `seed_demo`: the two organizations differ here as they do in tax -- one
  is `billing_exempt` (the banner never shows), the other on a trial that
  ends in five days (the banner shows, warning, with the date). A rule
  read from the wrong tenant shows up as the wrong banner. No Stripe
  objects are seeded, for 4b's reason.
- `CLAUDE.md`: `billing/subscription.py` in the layout beside
  `connect.py` and `webhook.py` as the third and last Stripe caller; the
  coupling list gains the six platform event types (`PLATFORM_HANDLERS`
  and the runbook), the `?subscription=return|cancelled` keys, and
  `allowed_while_inactive`; invariant 4's gate record lists the platform
  webhook as the second deliberate public route. Write "Phase 4c as
  built", run the gate, record it.

**4c exit criteria:** on staging, the seeded trial organization shows the
trial banner; its owner subscribes with the test card and, back on the
settings page, sees Active with the renewal date and no banner; the
portal opens from Manage and a cancellation there puts "ends on" in the
banner within seconds; with `trial_ends_at` and `subscription_paid_through`
moved back in the admin, the daily task makes the organization read-only
and the banner says why, the schedule still loads, a job cannot be
edited, and Subscribe still works and reactivates it; and the other
organization, exempt, sees none of it.

---

## Later phases

**Phase 5 — notifications and the customer portal.** Visit reminders by email
and SMS; the customer's own view of their visits and invoices, reached by
magic link (already built and restricted to customers). Invoice email is *not*
here — it moved into 4a, because an invoice nobody receives is not a feature.

Open decisions are listed under **Start here** at the top of this file.

---

## Phase 3b as built

Delivered as specified: all seven screens, the API client, the session store,
and the generated contract. 65 Vitest tests over the interceptors, the session
boot sequence, and the timezone helpers. The backend grew to 466.

**Vuetify 4, not 3 (ADR-022).** ADR-018 named the then-current major. Node 22
is a hard requirement — the lint toolchain calls `Object.groupBy`, absent
before Node 21, and crashes rather than degrading on Node 20.

**Two things the generated contract caught that review would not have.**

1. Access codes are write-only, so they are absent from the *response* type.
   Write paths now take the split request types (`ServiceLocationRequest`),
   which makes "a code must never appear on a read payload" a compile error
   rather than a convention.
2. Building the assignee picker surfaced that `MembershipSerializer` never
   exposed a user id, so the UI could list colleagues but not assign any of
   them. Fixed in the backend with the regenerated schema in the same commit —
   which is the feedback loop 3a and 3b were split to produce.

**One real bug, worth knowing about because the pattern recurs.** A dialog
rendered behind a `v-if` that becomes true in the same tick as its `v-model`
mounts with the model *already* true, so a plain `watch` never sees a change.
`RevealCodesDialog` opened blank — no warning text, because the fetch never
ran. It needs `{ immediate: true }`.

**Local throttle rates raised** in `app/settings/local.py`. Five sign-ins an
hour is right for a deployed environment and unworkable for building a login
screen, and the throttle is keyed on IP so every browser profile on the
machine shares one bucket. Production keeps the real rates.

**Verified in a real browser** (Chromium via Playwright, driven from a
scratchpad — no browser automation was added to the repo, per the 3b spec):
26 checks as the seeded dispatcher at 1440px and the cleaner at 400px, in both
organizations. Sign-in through the 2FA challenge, the week grid in each
organization's own timezone, job detail, the customer screens, the reveal
dialog showing the backend's own warning and then the codes, and the cleaner's
clock-in → clock-out → complete at 400px with no horizontal overflow.

**Resolved since:** the stale-`FIELD_ENCRYPTION_KEY` row noted under "Phase 3a
as built" had its three unreadable fields blanked in the dev database, so both
organizations' customer screens work. The trap itself still applies to any
database that outlives a key change — the message names the field.

**Not in 3b, unchanged:** customer portal, billing, notifications, offline
support, push, a production build pipeline for `ui/`.

---

## Phase gate — baseline (2026-09-17)

The gate in `CLAUDE.md` was adopted after Phase 3b, so this first run covers
everything through 3b rather than one phase. Branch `phase-gate-baseline`.
Three reviewers ran in parallel (coverage; DRY / modularity / orthogonality;
authentication and authorization); every finding acted on was reproduced with a
failing test before it was fixed. Backend 466 → 552 tests, frontend 65 → 77.

### Fixed

**Security**

- **Notes and photos could be edited by anyone who could see them.** A full
  `ModelViewSet` with guards on create and delete and none on update: a cleaner
  could rewrite a dispatcher's note, or re-point one at another job. Edits are
  now author-or-dispatcher and `job` is fixed after creation.
- **Soft-deleted assignments and jobs kept granting access.** The soft-delete
  manager filters only the model being queried, never a join. A cleaner taken
  off a job still listed and opened it (address, access notes, customer phone)
  and its notes; a cleaner on a *deleted* job could still reveal its codes,
  logged against no job at all. `scheduling.models.assigned_to()` now carries
  the rule for every join. **Any new join across a soft-deletable relation has
  the same trap.**
- **Magic links signed in staff.** ADR-008 makes them the customer flow; the
  view issued one to any account, so an owner's mailbox alone was an owner
  session with no password and no 2FA. Now "a customer and nothing more",
  checked at issue and again at consume.
- **The sign-in throttles did not hold.** `NUM_PROXIES` unset means DRF keys on
  the raw `X-Forwarded-For`, so varying the header bought a fresh bucket per
  request; counters sat in per-process memory (x4 workers, reset on recycle).
  `NUM_PROXIES` is explicit (0; 1 in production; env-overridable), counters are
  in Redis, login has a per-account throttle. First tests that a 429 can
  happen. ~~Set `NUM_PROXIES` to the real proxy depth when ADR-006 is
  decided.~~ Done in Phase D: `1` in `deploy/fly.toml` (ADR-027).
- **2FA attempts were counted on the instance**, so a stale copy wrote its own
  count back: parallel guesses cost one attempt. Conditional `UPDATE`s now, for
  attempts, code consumption and magic-link consumption.
- **`Customer.user` accepted any account in the system**, another
  organization's included (`CustomUser` is not a `TenantModel`, so the FK guard
  never saw it). Narrowed to customer members of the caller's organization.
- `/health/ready/` no longer returns raw driver errors; `client_ip` honours
  `NUM_PROXIES` and drops junk instead of handing an inet column a 500; Celery
  defaults to production settings like wsgi/asgi.

**Contract and frontend**

- **Three `@action`s had the wrong schema and nothing noticed** — spectacular
  does not warn on a viewset action, it borrows the viewset's serializer.
  Annotated; `test_every_action_carries_extend_schema` closes the blind spot;
  the UI's hand-written response types are now generated aliases.
- **Any 403 signed the user out client-side**, though the API returns 403 to
  signed-in users by design (ADR-017). The client now re-reads the session and
  signs out only if it is gone.
- **The pages kept their own copies of the job state machine**, contrary to
  what `CLAUDE.md` claimed, and My Day's had drifted (`no_access` was not
  "finished"). `Job` now publishes `next_statuses` (per caller, with
  `reason_required`) and `is_terminal` from the same function
  `transition_job` enforces through; a test checks published == accepted for
  every status x role x target.
- A $0.07/sq ft rate could not be saved (`0.07 * 100` is not `7`).
- The deploy-audit command in `CLAUDE.md` reported two warnings as written.
  `pre-commit` was configured but **not installed** in this checkout, so no
  hook had been running; it is now, and all hooks pass.

### Decided by Jordan, 2026-09-17, and implemented

1. **Django admin requires the 2FA flow.** `app/admin.py` replaces the default
   site: no login form, and only a session `VerifyView` has marked as verified
   is admitted. Rejected: lockout alone (a stolen password is still a
   cross-tenant session), disabling admin in production, deferring to network
   controls.
2. **A deactivated organization is read-only.** Unsafe methods are refused in
   `IsOrgMember`; reads still work so a suspended tenant can see what they have
   and, from Phase 4, what they owe. Superusers exempt. Rejected: blocking
   entirely. *Not done:* the UI shows the server's message on a refused write
   but has no standing "this organization is deactivated" banner, and
   `is_active` is not on the session payload to drive one.
3. **No self-review of reveals except by the owner; reviews are final** (409 on
   a second). `audit.services.mark_reviewed`. The owner exception exists so a
   one-person office can clear its own flags.
4. **Cleaners see staff rows only** in the membership directory.
5. **Uploads capped** (10 MB photos, 2 MB logo, on the model fields) **and
   cloud media is private**, read through 5-minute signed URLs. A page left
   open longer than that will need a refetch to show a photo it had not yet
   loaded.
6. **The 2FA code is in the email body only.**

### Coverage gaps left open

Isolation and role tests exist for list/retrieve almost everywhere, and for
little else. Still untested, highest value first: cross-organization
update/delete/actions on `ServiceLocation`, `Service`, `RecurringPlan`
(`regenerate` is never called over HTTP; plan DELETE is never tested), `Job`
(`status`, `clock-in/out`, `unassign` against a rival job) and `TimeEntry`;
the `review` action's 403/404 paths; a dispatcher PATCHing the organization;
photo delete. Frontend: the router guard, `listAssignableStaff`, and
`RevealCodesDialog` have logic and no spec.

### DRY / modularity backlog — fold in as files are touched

- Role-scoped `get_queryset` is written three times in `scheduling/views.py`,
  and `membership → role` is extracted five times. Wants a helper in
  `base/permissions.py` and a mixin in `base/viewsets.py`.
- ADR-020's "which edits regenerate" rule lives in `RecurringPlanViewSet.update`,
  so an edit from admin, a task or the seed does not regenerate. Wants
  `services.update_plan()`. Same for preview windowing and job deletion.
- `try/except ConflictError` x5 and `except ValueError → {"service": ...}` x4
  in `scheduling/views.py`; handle once in `app/exceptions.py`.
- The ±24h assignment window filter is written three times in
  `scheduling/permissions.py`, two of them queries that are not permissions
  (and are lazy-imported by `audit`).
- `clock_in` sets status by hand though `transition_job`'s docstring names it
  as a caller.
- Local-day bounds are computed two different ways (`scheduling/filters.py`
  half-open, `audit/tasks.py` `time.max`); "organization's today" three times.
- Customer/location and date-order rules exist in both model `clean` and
  serializer `validate`, with identical strings.
- UI: field-error flattening is copy-pasted across four pages (use
  `src/api/errors.ts`); the `timeZone` computed is repeated on four pages; auth
  paths are called from the store rather than `endpoints.ts`; `LoginPage`
  hardcodes the invalid-credentials string instead of rendering `detail`.
- Tests: ~15 sites build `JobAssignment`/`TimeEntry` rows by hand instead of
  using the factories.
- `IsCustomerOfJob` is a permission class defined in a views module and
  near-duplicates `base.permissions.IsCustomer`.
- Cross-app serializer imports: `customers.views → audit.serializers`,
  `two_factor.views → users.serializers`.

### Couplings found (now listed in `CLAUDE.md`)

Role tiers are mirrored in four places in `ui/`; `scheduling/serializers.py`
chooses a 400's field by reading the text of a `catalog` error message;
`customers` lazy-imports `scheduling.permissions` to avoid an import cycle.

### Production guard

All work is on `phase-gate-baseline`; nothing was committed to `main` after the
branch was cut. There is no production.

---

## Phase 4a as built

Delivered as specified: the `billing` app, the four tables, the rules in
`billing/services.py`, `/api/billing/`, three screens, the invoice email, and a
seed that has already billed a book of business. Backend 552 → 818, frontend
77 → 118. Branch `phase-4-billing`.

**Two deviations from the 4a spec**, both deliberate:

1. `Invoice.number` is `default=""` rather than nullable. `ruff`'s DJ001 is in
   the selected rule set and every other `CharField` in the repo reads that
   way; the partial unique constraint keys off `number != ""` instead.
2. `Payment.delete()` raises unconditionally rather than only for a non-draft.
   A payment has no draft state to be in.

**Two things the spec did not ask for and the work needed.**

*Closing the past.* The materializer has no business inventing history, so a
seeded board had a fortnight of visits all still "scheduled" — a business that
never turned up, and nothing billable at all. `seed_demo` now walks them
through the real state machine, and turns the most recent into a no-access so
the fee has something to price. `seed_demo --force` also applies the billing
settings to an organization it *found*, not only one it created; without that,
a run against a pre-Phase-4 database built INV-prefixed invoices at 0% tax and
reported success.

*A billing settings screen.* 4a.5 says "organization settings gain tax rate,
no-access fee, prefix, terms and footer". There was no organization settings
screen, so there is one now, at `/billing/settings`, admin-and-above to save.

**Found by driving it in a browser, not by tests.** Pressing Email did nothing
visible. The mail went out — the worker log had the subject line — but
`sent_at` is stamped by that worker *after* the send, and the 202 returns
first, so the "Emailed" line the page keys off was never yet true. The page
now says "Queued to ...", and "Emailed" still means `sent_at`.

**Not in 4a, unchanged:** Stripe of any kind (4b, 4c), the customer's own view
of their invoices (Phase 5), a PDF, statements, credit notes as a first-class
object (an adjustment line is the mechanism), partial refunds.

### Phase gate — Phase 4a (2026-09-17)

Three reviewers ran in parallel (coverage; DRY/modularity/orthogonality;
authentication and authorization), plus `/security-review` on the phase diff.
Every finding acted on below was reproduced before it was fixed.

**1. Test coverage.** Backend 552 → 818, frontend 77 → 118 (counts updated
under "Start here"). Every new endpoint has a cross-tenant test on update,
delete and actions as well as list and retrieve, and a test per role on each
side of its permission line — including the *allow* side for owner and admin,
which the baseline gate's own tests only covered for dispatcher. Added after
review: the payment filters (all of them were untested, including the
`voided=true` double negative), the invoice date and customer filters, the
reprice endpoint's happy path, the email task's failure branch, and
cross-tenant detail reads on lines and payments. Two tests were rewritten for
overclaiming: one asserted `sent_at` on a 202 response, which only holds
because Celery runs eagerly under test and contradicts the endpoint's own
documented contract; another was named for recording who issued an invoice and
asserted only the amounts — `actor` was accepted by two services and used by
neither, so `created_by` and `issued_by` now exist and are recorded.
*Gap left open:* no page component has a vitest spec, here or anywhere in the
repo. The risky logic was extracted to `src/lib/` instead — `money.ts` (the
no-access fee, which means cents when flat and a percentage when not) and
`billable.ts` (the grouping and its totals) — and spec'd there.

**2. DRY.** Folded in from the backlog: `ConflictError` handled once in
`app/exceptions.py` (five try/excepts and a `destroy` override gone);
`Organization.today()` / `local_day_bounds()` replacing three hand-rolled
versions; `lib/money.ts` replacing inline `Math.round(x * 100)`; `errorDetail`
adopted in the three pages this phase touched; `session.timeZone` replacing the
same computed on six pages; `session.isAdminOrHigher` replacing the admin tier
written out twice. New this phase and folded in before the gate closed:
`InvoiceListItem` for the row two pages drew, and `INVOICE_STATUS_OPTIONS` /
`PAYMENT_STATE_OPTIONS` derived from the chip labels rather than re-typed.
*Still open, in files this phase touched:* `except ValueError → {"service": …}`
×4, role-scoped `get_queryset` ×3 and `membership → role` ×5, all in
`scheduling/views.py`; ADR-020's regeneration rule still inside
`RecurringPlanViewSet.update`; `IsCustomerOfJob` still defined in a views
module.

**3. Modularity.** No page reaches the backend except through `ui/src/api/`;
no `fetch` anywhere. All billing rules live in `billing/services.py`, which the
views, the seed and the Celery task all call. Billing reaches other apps
through models and their methods (`Organization.no_access_fee_cents`,
`Service.quote_cents`, `Job.time_entries`), never their views.

**4. Orthogonality.** `CustomerSummarySerializer` moved from `scheduling` to
`customers`, the app that owns the model — billing had imported it from
scheduling, which would have been a third cross-app serializer import. New
couplings, now listed in `CLAUDE.md`: three more `ENUM_NAME_OVERRIDES` rows
(`InvoiceStatus`, and `PaymentMethod`/`LineKind`, which otherwise generate as
`MethodEnum` and `KindEnum`); and `billing` imports `scheduling` and `catalog`
while nothing imports `billing`, so that dependency must stay one-way.
`available_actions` was going to be a fourth: it is now published as a schema
enum, so the frontend's `InvoiceAction` is generated rather than hand-kept.

**5. Security.** Five real holes, all closed, all with tests:

- **An invoice could go void with live money against it.** `void_invoice`
  checked for payments off an unlocked row. The money then vanished from every
  screen (the balance short-circuits on a non-issued invoice) while the visits
  were freed and billed again.
- **Two dispatchers issuing at once burned an invoice number.** Only the
  sequence was locked. Both passed the draft check, both took a number, and one
  of them held a number that existed on no document — the gap ADR-026 says the
  sequence does not have. Both fixed by `_locked_status()`.
- **A visit line could be moved onto another customer's invoice.** `clean()`
  refused it; DRF never calls `full_clean()`, and `TenantModel.save()` only
  compares organizations, so within one tenant it went through — and the
  description printed in the other customer's invoice email.
- **A visit line could be rewritten in place**, silently, contradicting the
  viewset's own docstring: a $150 taxable visit set to one cent and untaxed
  read as though it always had been. Corrections are adjustment lines now.
- **`billable_jobs` joined to `customer` without filtering `deleted_at`** — the
  same trap that cost the baseline gate three bugs, one join further out.

Also: the invoice-line serializer's `invoice` field is scoped to the caller's
organization, so another tenant's id gets "does not exist" rather than a 400
describing the invoice; `InvoiceAdmin.status` is read-only; `draft_invoice`'s
`select_for_update` no longer locks a row in every joined table.
`/security-review` on the phase diff afterwards returned **no HIGH or MEDIUM
findings**. Deploy audit clean. Nothing is `AllowAny`; all three viewsets state
`IsDispatcherOrHigher`.

*Known and accepted:* `send` is unthrottled and does not de-duplicate, so a
dispatcher can mail a customer the same invoice repeatedly — email rate
limiting belongs with Phase 5's notifications. The `payment_state` and
`overdue` filters evaluate in Python over the tenant's whole invoice table,
which is linear in its size; revisit if a tenant's invoice count makes it bite.

**6. Branch and production guard.** All of Phase 4a is on `phase-4-billing`.
Nothing was committed to `main`, nothing was pushed, and there is no production.

**Verified in a real browser** (Chromium via Playwright, driven from a
scratchpad — no browser automation was added to the repo, per the 3b spec): 27
checks at 1440px across both seeded organizations, re-run after the gate's
refactors. The invoice lifecycle end to end (draft → adjustment → issue →
email → payment → void → balance returns), the server-owned action buttons, the
per-tenant prefixes and tax rate, a cross-tenant invoice URL refused, and the
cleaner's 403 with no Billing link at all.

---

## Phase D as built

Branch `phase-d-deploy`, on top of `phase-4-billing`. Backend 818 → 890,
frontend 118 → 121. What the spec asked for, and what each turned into:

**Decide the target → Fly.io, ADR-027.** One `deploy/fly.toml` with `web`,
`worker` and `beat` as process groups of one image and the migration as the
release command; Fly Postgres, Upstash Redis and a Tigris bucket attached as
secrets. Staging and production are two apps from the same file, named on the
command line. Render, Cloud Run, a VPS and a separately hosted frontend are
each rejected in the ADR with the cost that decided it.

**Stand up staging → written and rehearsed, not executed.** Production and
staging are Jordan's alone (`CLAUDE.md`), so `docs/DEPLOY.md` is the
stand-up, step by step, with every command and the two secrets CI needs.
What *was* executed here is the rehearsal it tells Jordan to run first:
`deploy/docker-compose.rehearsal.yml` runs the production image under
production settings behind Caddy (TLS, one proxy deep, so `NUM_PROXIES=1`
and `X-Forwarded-Proto` are real) with MinIO as the bucket. Seeded, then
verified: readiness green; `/schedule` served with `no-cache` and a hashed
asset with a one-year immutable cache; an unknown `/api/` path a 404 and not
the page; http redirected to https; a dispatcher signed in over TLS by curl
(CSRF cookie → 202 → code from the log → verify → invoices), and an owner
signed in by headless Chromium (login → verify → `/schedule`, a deep link to
`/billing` reloaded and still signed in, no console errors or failed
requests); a private object written through the S3 backend, fetched with its
signature (200) and without (403).

**Serve the `ui/` build → from the API's origin.** The Dockerfile moved to
the repository root and gained a Node stage; `docker build` and the compose
build context are now the root. Whitenoise serves Vite's output at the root
URL, a middleware subclass teaches it Vite's hash shape so `assets/` is
immutable, and `app.spa.spa_index` answers the SPA's routes with the index
(GET and HEAD, `Cache-Control: no-cache`), fenced off from every prefix the
API owns. A production build calls relative URLs; the dev server is
unchanged. Consequence taken deliberately: with nothing cross-origin,
`CORS_ALLOWED_ORIGINS` is no longer required in production and the cookies
are `SameSite=Lax`; setting an origin loosens them again, from the one
setting.

**`NUM_PROXIES` → 1, in `deploy/fly.toml`,** with the reasoning beside it and
"a CDN makes it 2" in `CLAUDE.md`'s coupling list.

**Private media → configured and refused when it is not.** The S3 and GCS
storage options now carry the bucket, endpoint, region and credentials from
the environment (the names `fly storage create` sets), path-style addressing
and SigV4 for Tigris and MinIO, private ACL, expiring signed URLs, no
overwrite. The image installs the `s3` extra. Production refuses
`MEDIA_BACKEND=filesystem` outright (nothing serves `/media/` outside
`DEBUG`), a cloud backend without a bucket, a missing frontend build unless
`UI_DIST_DIR` is set empty on purpose, and a `FRONTEND_BASE_URL` that is
missing or not https. Thirty-four tests import the production module under
controlled environments, isolated from any developer's `app/.env`, and pin
each refusal.

**GitHub Actions → `.github/workflows/ci.yml`.** On every push and pull
request: backend (ruff, ruff format, pytest against a Postgres service, the
committed `openapi.yaml` regenerated and diffed, the deploy audit with
`--fail-level WARNING`), frontend (eslint, vue-tsc, vitest, the committed
`schema.d.ts` regenerated and diffed, `build-only`), then the image built
with the GHA cache and not pushed. A final job deploys `main` to staging with
`flyctl`, skipped until Jordan sets the `FLY_STAGING_APP` variable and a
`FLY_API_TOKEN` scoped to that one app. Nothing deploys production.

**Found on the way.** `npm run type-check` was failing on `phase-4-billing`
before this phase touched anything: `billable.spec.ts` uses `toSorted()`
because the lint config demands it, and `@vue/tsconfig`'s lib stops at
ES2020, so lint and type-check could not both pass. `tsconfig.app.json` now
sets `lib` to ES2023. The MinIO images left Docker Hub in 2025; the rehearsal
pulls from quay.io. `seed_demo` refuses to run with `DEBUG` off, which the
rehearsal (and staging) need `--force` for. The sign-in code is mailed from
the request, not the worker, so a rehearsal reads it from the web log; the
mail backend is now env-selectable so a rehearsal can print it at all.

**Not done here, on purpose:** running `fly` against anything; creating the
GitHub environment, variable or secret; a `FIELD_ENCRYPTION_KEY` rotation
path (noted in the runbook as not built); a production Postgres topology
beyond the recommendation in the runbook.

### Phase gate — Phase D (2026-09-17)

Three reviewers ran in parallel (coverage; DRY/modularity/orthogonality;
authentication, authorization and deploy hardening), plus `/security-review`
on the phase diff. Everything below marked fixed was reproduced first.

**1. Test coverage.** Backend 818 → 890, frontend 118 → 121. Fixed after
review: the production-settings tests could pass or fail on a developer's
`app/.env` (python-decouple falls through to it for anything a test leaves
unset) — the fixture now installs an empty repository per load, and a test
plants a contrary `.env` to prove it; `API_BASE_URL`'s three cases (relative
in a production build, the dev-server port under Vite, the override) have a
spec; the `EMAIL_BACKEND` override, the GCS bucket fallback and GCS options,
the HSTS knobs and the https requirement on `FRONTEND_BASE_URL` are each
pinned; the fence has a test that walks every top-level route in
`urlpatterns` and asserts none resolves to the SPA, plus the bare prefixes,
the look-alikes (`/apifoo` is a page), and `/api/schema/` and `/api/docs/`;
the whitenoise hash regex is pinned to Vite's exact eight characters with
five near-misses; a missed index read is no longer cached for the process
life, and a test shows a build appearing later is served. *Gap left open:*
no page component has a vitest spec, so `LoginPage.vue`'s origin-free copy
when `API_BASE_URL` is empty is untested — same gap as 4a, same reason. The
SameSite-follows-CORS rule is exercised only through the production reload,
since `test.py` and `local.py` pin `Lax`.

**2. DRY.** The deploy audit was the same command written by hand in
`CLAUDE.md`, `README.md` and `ci.yml`; it is `app/bin/deploy-audit.sh` now
and all three call it. CI re-implemented `app/bin/check-schema.sh`; the script
takes `PYTHON` and CI calls it. The SPA fence was a hand-kept list of prefixes
in `urls.py` mirrored by a hand-kept list in the test and in `CLAUDE.md`; it
is derived from `urlpatterns` plus `STATIC_URL`/`MEDIA_URL` now, and the test
walks the real routes. Three stale ADR-006 references corrected. `README`'s
restated env semantics replaced by a pointer to `.env.example`.
*Accepted:* the three process commands in three orchestrators, the
Postgres/Redis healthchecks in both compose files, the Python version in
three places, and `/ui/dist` agreed by construction — all now in `CLAUDE.md`'s
coupling list. CI's `schema.d.ts` diff step restates one line of
`npm run api:types`; left inline. `pip install ".[dev]"` has no lockfile, so
a drf-spectacular release could fail the schema diff for a reason unrelated
to the change; noted, not addressed.

**3. Modularity.** `app/app/spa.py` and `app/app/middleware/static_files.py`
are root-URL concerns with no domain semantics and sit in the project
package beside `urls.py` and the tenant middleware; settings import no app.
`docs/DEPLOY.md` is the one home of operator steps; `README` and `CLAUDE.md`
point at it.

**4. Orthogonality.** New couplings recorded in `CLAUDE.md`: the derived
fence (a new top-level route needs only its `path()`, declared before the
catch-all), the Python version in three files, the built frontend's path
agreed by construction across the Dockerfile, settings and gunicorn, the
process commands in three orchestrators, and `fly.toml`'s dockerfile path
being relative to the toml while the build context is the working directory.
The cookie `SameSite` rule is now documented beside `CORS_ALLOWED_ORIGINS` in
`.env.example`, where the operator reads it.

**5. Security.** No HIGH findings. Two MEDIUM, both fixed:

- **`/index.html` was served by whitenoise, which runs no middleware below
  it** — so the SPA's own document went out without `X-Frame-Options` and
  with a sixty-second cache. Whitenoise now skips `index.html` (it always
  goes through `spa_index`: `no-cache`, and the clickjacking header), and
  `XFrameOptionsMiddleware` moved above whitenoise so the files it does
  serve carry the header too. Tested both ways. Exploitable only in the
  opt-in cross-origin configuration (Lax cookies make a cross-site frame
  anonymous), which is why `/security-review` scored it LOW.
- **The staging deploy job used a mutable `@master` ref with the deploy
  token in scope**, and interpolated a repository variable into the shell
  line. Pinned to a commit (tag 1.6); the variable now arrives through `env`.

LOW, fixed: the fence matched string prefixes, so `/api` and `/admin` without
a slash were pages (now segments: `(?:/|$)`); the hash regex accepted eight
*or more* characters; `FRONTEND_BASE_URL` was not required and defaulted to
the dev server, so a forgotten secret would have mailed a customer's sign-in
token in a plain-http localhost link (required, and must be https); HSTS
`includeSubDomains`/`preload` were hardcoded (env-controlled, default on,
and `DEPLOY.md` says to use a subdomain of its own); filesystem media with a
volume passed the import check but nothing serves `/media/` outside `DEBUG`
(refused outright); the rehearsal published its ports on every interface
(loopback now); workflow-level `cancel-in-progress` could cut off a running
`flyctl deploy` after the release command had migrated (per-job for the
checks only). *Accepted and noted:* `gunicorn_conf.py`'s
`forwarded_allow_ips="*"` — gunicorn is reachable only through Fly's proxy
(or the rehearsal's Caddy), and `force_https` at the edge makes a forged
`X-Forwarded-Proto` worthless. Nothing is `AllowAny` that was not already;
`spa_index` is a plain Django view, deliberately public, and is the one
addition. Deploy audit clean via the shared script. The rehearsal was rebuilt
and re-probed after every fix above.

**6. Branch and production guard.** All of Phase D is on `phase-d-deploy`,
branched from `phase-4-billing`. Nothing was committed to `main`, nothing
was pushed, nothing was run against Fly or GitHub settings, and there is no
production or staging yet — both are Jordan's to stand up from
`docs/DEPLOY.md`.

### Found on the first staging deploy (2026-09-22)

Jordan stood staging up from the runbook on 2026-09-18. The deploy built,
migrated and started `web` and `beat`, then hung: `web` never passed its
check, so Fly's proxy routed nothing to it and the deploy stopped before
creating `worker`. `fly checks list` showed why -- the probe got a `400`.
**Fly probes a machine by its private IP** (`Host: 172.19.75.162:8000`),
which differs per machine and can never be in `ALLOWED_HOSTS`, so
`CommonMiddleware` refused every probe. The rehearsal had not caught it
because Caddy forwards `Host: localhost` and the compose healthcheck called
`localhost` too.

Fixed on `fix-fly-health-probe`: `app/middleware/health.py` sits first in
`MIDDLEWARE` and dispatches the two `health` routes before anything reads
`Host`; nothing else is exempt, and a test walks `/`, an API path and two
look-alikes to prove it. The rehearsal's healthcheck now probes the way Fly
does (readiness, with a private-IP `Host`), so this class of bug fails
there first. Backend 890 → 895. `docs/DEPLOY.md` step 6 now says what
`fly status` should show (including `beat`'s stopped standby, which is
normal) and the recovery table reads the check's reply before guessing.
Also from the same session: the seed guaranteed only three finished visits
on a Monday or Tuesday, so CI's seed test failed on the first push to
`main`; it starts three weeks back now.

---

## Phase 4b as built

Branch `phase-4b-stripe-connect`, on top of `main` after Phase D and the
first staging deploy. Backend 895 → 1027, frontend 121 → 131. What the spec
asked for, and what each turned into:

**Standard accounts, spelled in Stripe's controller form.** `stripe.Account`
`type="standard"` is the legacy spelling; the SDK's current form is
`controller: {stripe_dashboard: full, fees: payer account, losses: payments
stripe, requirement_collection: stripe}`, which is the same account. Every
one of those is the ADR-024 choice (the tenant's dashboard, the tenant's
fees, the tenant's losses), so `connect.py` says them out loud rather than
relying on a default.

**Stripe optional on the server.** `STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)`;
production refuses a key without `STRIPE_CONNECT_WEBHOOK_SECRET`; every
entry point without a key is a 503 through a new `ServiceUnavailableError`
in `app/exceptions.py`, rendered beside `ConflictError`. The `stripe`
package is imported inside `connect.client()` and inside the webhook view,
nowhere at module level, so a deployment without a key never loads it.
Staging deployed on this branch's parent without a key and still does.

**The pay link is `django.core.signing`, salt `billing.pay`, 120 days.** It
carries the invoice id and nothing else. A token for a deleted invoice is a
404; a token under another salt or past its age is a 404; the public
serializer sends the organization name, number, dates, lines and totals --
no email, no address, no ids -- and `payable_reason`, the server's own
sentence for why the button is absent.

**The webhook is a plain Django view, not DRF.** `request.body` reaches
`construct_event` untouched; the verified raw body (`json.loads` of the
same bytes) is what the ledger stores, not the SDK's object model. The
secret is read per request. Ledger, resolve the account to its
organization, mark IGNORED what nobody owns or handles, enqueue ids only,
200. The task runs the handler and the PROCESSED mark in one transaction
and marks FAILED outside it before re-raising for Celery's three retries.
Replaying a FAILED event is `process_stripe_event.delay(org_id, row_id)`
from a shell -- the admin "replay" action the spec mentioned is not built;
the admin lists FAILED rows read-only and the shell is one line. Open.

**The ledger is the queue of record (added 2026-09-23).** The first
staging payment was ledgered, enqueued, and lost when the kernel killed the
worker under it: the default early acknowledgement had already taken the
message off Redis, and a process killed raises nothing for Celery's retry
to catch. The row sat RECEIVED for a day. Two layers now: the task
acknowledges late and is put back when a worker child dies
(`acks_late`, `reject_on_worker_lost`); and `billing.sweep_stripe_events`
runs from beat every five minutes and re-queues any row still RECEIVED
after ten, which also covers a lost worker *machine* (with a Redis broker
an unacknowledged message otherwise returns only after the hour-long
visibility timeout). A row still RECEIVED an hour on is marked FAILED with
a note, so a task that kills its worker every run lands in the admin's
queue instead of looping. Ten minutes and an hour are margins, not knobs:
the handler runs in milliseconds and the retries finish inside a minute.
The same sweep fills in a card payment's fee when the checkout event
landed before Stripe had attached the balance transaction to the charge
(one second apart on staging): a Stripe payment with no fee is re-read
each pass for an hour, then left as it is.

**Overpayment is recorded as it happened.** `record_provider_payment` has
no balance check and is idempotent on `(provider, provider_reference)`
including void rows; `overpaid_cents` is published and the invoice page
says "refund from your Stripe dashboard". A partial refund voids the
original and writes the net under `pi:refunded-<cumulative>`, so a replay
repeats the reference and a further refund changes it; the handler skips
itself when the replacement already exists.

**`can_void` on a payment.** The spec said the void action "stays hidden
for `provider != ''`"; hiding is not a permission, so the server publishes
`can_void` and `PaymentViewSet.void` refuses a provider payment with a 409
(`services.assert_voidable_by_hand`). The page draws the button from the
field.

**Dates on the pay page.** The spec had the serializer formatting dates in
the organization's zone; `issued_on`/`due_on` are already organization-local
`DateField`s, so they go out as plain dates and the page formats them with
`formatDayLabel`, which takes an ISO date and no zone. Nothing to convert.

**What is not built, on purpose:** the application-fee display on the
settings card (the fee ships at 0 and the card would say "0%" to everyone);
tips on card payments; `checkout.session.async_payment_succeeded` (bank
debits) -- a paid session is `payment_status: paid` or it is ignored;
disconnecting a Stripe account; the admin replay action (above).

**Changed by the gate (below), and worth knowing before touching this:**
the settings card switches on `stripe.state`, a word the server publishes
(`Organization.stripe_state`), not on the flags; Checkout is pinned to
`payment_method_types: ["card"]` because a bank debit settles through an
event that is not handled; and `charge.refunded` applies only a cumulative
refund larger than the one already on the ledger, because Stripe does not
promise order.

### Phase gate — Phase 4b (2026-09-22)

Three reviewers ran in parallel (coverage; DRY/modularity/orthogonality;
authentication, authorization and money), plus `/security-review` on the
phase diff. Everything below marked fixed was reproduced first.

**1. Test coverage.** Backend 895 → 1027, frontend 121 → 131. Every new
model, service, endpoint, handler and task has direct tests; cross-tenant
is covered at the webhook's resolve step, in the checkout handler, and --
added by the gate -- for the refund and dispute handlers (org B's account
reporting org A's PaymentIntent touches nothing). Fixed after review:
`StripeRefreshView` had no owner, anonymous or 503 case; the throttle test
asserted only the scope string (now proves `ScopedRateThrottle` is on both
views and the rate resolves in the running settings); the rollback test
failed before anything was written (now fails at the fee read, after the
payment row, and proves no row survives); the deleted-invoice test used a
hard delete (now a soft delete, which is what the default manager must
hide); the Celery task body was never invoked (now `.apply()`ed once);
`can_void` for an already-void payment, a platform event at the endpoint,
`session.isOwner`, and `connect.client()` on the pinned version were each
untested. *Gaps left open:* no page component has a spec (same as every
phase): `refreshAfterReturn`, `copyPayLink`, `settleAfterReturn` and the
pay page's 409 reload are exercised only by hand; `StripeEventAdmin`'s
refusals are untested, like every other admin.

**2. DRY.** `pay_url`-if-payable was computed in the serializer and again
in the email context: one `connect.pay_url_if_payable` now. The `stripe`
block was built by hand beside a serializer whose fields had no `source`:
`StripeStatusSerializer` reads the model directly and both the organization
payload and the refresh view use it. The three Stripe test modules each
defined `stripe_on` and `connected`: both live in `billing/tests/conftest.py`
now, opted into with `usefixtures`, and `ACCOUNT_ID` is the one name for
the fixture account. The pinned API version was a second literal in a test
fixture (reads `settings.STRIPE_API_VERSION`); "No handler for this event
type" was spelled twice; a `Q` import was lazily inside a function for no
reason. *Accepted:* `create_checkout_session` checks `enabled()` and then
`payable()` checks it again -- deliberate, 503 before 409; the
`overpaid` copy names Stripe while `Payment.provider` is generic -- right
while there is one provider, becomes `overpaid_reason` on the server if a
second arrives.

**3. Modularity.** `billing/views.py` serialized the whole organization to
pluck `["stripe"]`: it imports `StripeStatusSerializer` alone now, a one-way
reach that is recorded in `CLAUDE.md`. The Connect views stay in `billing`
so `organizations` never imports `billing`. `stripe` is imported in
`billing/connect.py` and `billing/webhook.py` only, both lazily. Nothing
under `app/*/` imports `billing` except `seed_demo`, which predates the
phase and is a command, not an app module -- `CLAUDE.md` now says so.

**4. Orthogonality.** Recorded in `CLAUDE.md`: `isOwner` in the mirrored
role tiers; `STRIPE_API_VERSION` beside the SDK pin; the throttle scopes in
three settings modules (`local.py` was missing them -- the pay page 500'd
in development, found by the security reviewer); the `/pay/<token>` and
`/billing/settings` paths *and their query keys* (`?stripe=return|refresh`,
`?paid=1`, `?cancelled=1`) shared between `connect.py` and the router; the
five handled event types in `HANDLERS` and the runbook. ADR-023: the
settings card re-derived its three states from the flags -- the server
publishes `state` now and the page switches on it; the pay page had one
sentence of its own copy for the paid state -- it renders the server's
`payable_reason` instead.

**5. Security.** `/security-review`: no HIGH or MEDIUM findings. The gate
reviewer found two MEDIUM, both fixed and both about money agreeing with
the bank:

- **Out-of-order `charge.refunded` events could put money back.** Two
  partial refunds delivered later-first left the ledger at the smaller
  cumulative refund, under-billing the customer. The handler now reads the
  cumulative amount already applied off the live replacement's reference
  and ignores any event that refunds no more than that. Tested with the
  events reversed.
- **A non-card method the tenant enables in their own dashboard would
  complete a session `unpaid`** and settle days later through
  `checkout.session.async_payment_succeeded`, which is not handled: the
  money would move and the ledger never hear. Checkout is pinned to
  `payment_method_types: ["card"]`; widening it means handling that event
  first, and the comment says so.

LOW, fixed: a validly signed body with no `id` or `type` was a 500 (Stripe
retries a 500 for days) -- a 400 now, nothing ledgered. Documented rather
than changed: the pay token is a 120-day bearer credential that lands in
access logs and cannot be revoked short of voiding the invoice, and
rotating `SECRET_KEY` kills every outstanding link (the comment beside
`PAY_TOKEN_MAX_AGE` says so; `SECRET_KEY_FALLBACKS` is the answer if it
matters); a signed-in staff member with no CSRF cookie gets a 403 from the
checkout POST rather than a broken page (comment on the view).
*Accepted:* `start_onboarding` holds the organization's row lock across
two Stripe calls, and the checkout handler reads the fee inside the
PROCESSED transaction -- a slow Stripe response blocks that one tenant's
settings PATCH for its duration; availability, one tenant, and the lock is
what makes "create the account once" true. `AllowAny`, deliberate and
listed: `PayInvoiceView` (GET) and `PayInvoiceCheckoutView` (POST), both
throttled; `StripeWebhookView` is a plain Django view, `csrf_exempt`,
POST-only, signature-verified. Invariant 1's one exception, `StripeEvent`,
is served by no API (`ui/openapi.yaml` has no reference) and read-only in
the 2FA-gated admin. Deploy audit clean. No secrets in git or the bundle;
`.env.example` has placeholders only.

**6. Branch and production guard.** All of Phase 4b is on
`phase-4b-stripe-connect`, branched from `main` after the staging fixes.
Nothing was committed to `main` and nothing was pushed by an agent.
Staging exists and was not touched: no `fly` command was run, no secret
set, and the Stripe secrets are not set there -- staging runs this branch
with Stripe off until Jordan follows `docs/DEPLOY.md` step 9.

### Found on the first Stripe payment on staging (2026-09-22)

The exit test ran on staging the same day. The card was charged; the news
of it was lost twice, for reasons the rehearsal cannot reach:

- **Test mode and sandboxes are different worlds.** The `sk_test` key in
  Fly came from the platform's sandbox; the webhook endpoint was first
  created in the account's test mode. Every event fired with
  `pending_webhooks: 0` and nothing arrived, with no error on either side.
  The runbook now says: one sandbox, and the switcher must say *sandbox*.
- **The worker VM was too small.** 256MB held Celery until a task touched
  the Stripe SDK; the kernel killed the child mid-payment
  (`WorkerLostError`, then `Out of memory: Killed process (celery)`). The
  worker is its own 512MB `[[vm]]` now, beat stays at 256MB; the task is
  `acks_late` and `reject_on_worker_lost`, safe because both ledgers make
  a second run a no-op.
- **Resend was refused as a duplicate.** The row from the killed task sat
  in RECEIVED, and the unique constraint answered Stripe's Resend with a
  200 and nothing. A resent event whose row is RECEIVED or FAILED is now
  re-queued -- Resend is the replay button -- and the runbook's "when a
  payment does not show up" walks the four checks in order.
- A Stripe refusal from `accounts.create` was a bare 500 (Connect was not
  yet enabled on the account); every call into Stripe now surfaces
  Stripe's own sentence as a 503.

Backend 1027 → 1035.

---

## Phase 4c as built

Branch `phase-4c-platform-subscription`, on top of `main` after the
fee-backfill fix (2026-09-23). Backend 1049 → 1234, frontend 131 → 146.
What the spec asked for, and what each turned into:

**The coverage rule lives in `organizations/services.py`, as specified,
and it is the only thing that moves `is_active` for billing.** Pure
functions of the row and a moment: `covered_until`, `grace_ends_at`,
`billing_state`, `billing_notice`, `billing_summary`, `available_actions`,
`apply_coverage`. `billing/subscription.py` re-reads Stripe and then calls
`apply_coverage`; the webhook handlers and the daily task call
`refresh_subscription`; nothing else touches the fields. The trial is
stamped in `Organization.save()` on first save, and migration 0006 gave
every existing row one from the deploy.

**`paid_through` reads the subscription item, and never moves forward on
an unpaid read.** On the pinned API version the period is on
`items.data[0]`; the item's `current_period_end` counts only when the
latest invoice is `paid` (or Stripe is running a trial of its own),
otherwise its `current_period_start` -- and, added by the gate, never
later than what the row already says, because a subscription left to
cycle unpaid advances its period every month. The read also refuses a
Subscription whose `customer` is not the organization's own.

**One webhook view, two endpoints.** `StripeWebhookView.as_view(platform=
True)` verifies with the platform secret, resolves by `data.object.customer`,
and ledgers with `account == ""`, which is what `process` keys the
dispatch on (`checkout.session.completed` is in both tables). Every
platform handler is one function: adopt the subscription id the
organization lacks (or whose own is no longer live), then re-read. One
thing the spec did not ask for: `_redeliver` lets the *right* endpoint
take over a row the wrong one ledgered IGNORED and unowned -- a platform
endpoint misconfigured to hear connected accounts would otherwise make the
Connect delivery of the same event a dropped replay. The same path makes
Stripe's Resend apply an event that arrived before the organization held
its customer.

**The banner and the card compose nothing.** The server publishes `state`,
`level`, `notice` (the banner, null when quiet) and -- added by the gate --
`summary` (the card, always a sentence), each written for the reader's
role: the owner is told what to do, everyone else to ask the owner, and
the tenant's own customers nothing at all (no notice, no banner, the block
blank but for the state word). `available_actions` is what the endpoints
will accept; the card draws its buttons from it and `session.isOwner`.

**The one hole in the read-only rule** is `allowed_while_inactive = True`
on exactly `SubscriptionCheckoutView`, `SubscriptionPortalView` (owner)
and `SubscriptionRefreshView` (admin+), honoured in `IsOrgMember` after
the membership check and before the role check, so the role line still
holds on a deactivated organization. An organization deactivated *by
hand* is refused checkout with a 409: billing would never turn it back on,
so the money would buy nothing.

**The daily task runs at 01:00 UTC, an hour before the materializer**, so
a tenant it reactivates is filled the same night; it covers inactive
organizations too, re-reads each subscription, and applies the rule from
what is on file when Stripe is unreachable.

**Deviations from the spec, all deliberate:** no `stripe_customer_id`
uniqueness test was in the spec's list -- there is one now; the card's
"Past invoices" secondary button is labelled "Billing portal", which is
what it opens; `billing_enabled` is one function in `organizations.services`
that `billing.subscription` re-exports rather than a second copy; the
admin can edit `deactivated_for_billing_at`, because clearing it is how
an operator keeps a lapsed organization off even if it pays.

**What is not built, on purpose:** per-seat or usage pricing; a Stripe-side
trial; card management pages (the portal); plan changes (one plan); a
throttle on the three owner endpoints (consistent with the Connect ones;
each is a single Stripe call by an authenticated owner); an admin action
to replay a FAILED platform event (the shell one-liner from 4b applies).

### Phase gate — Phase 4c (2026-10-09)

Three reviewers ran in parallel (coverage; DRY/modularity/orthogonality;
authentication, authorization and money), plus `/security-review` on the
phase diff. Everything below marked fixed was reproduced first.

**1. Test coverage.** Backend 1049 → 1234, frontend 131 → 146. Every new
field, service function, handler, task, endpoint, serializer and frontend
helper has a direct test; the coverage rule is tabled by state with the
clock held; the Stripe module is tested at the client boundary with real
`StripeObject` trees so the dict-subclass gotcha around `.items` is
exercised; the webhook runs signed bytes against both secrets in both
directions; each endpoint has a test per role on each side, plus the
inactive-organization exemption with a PATCH and the Connect views beside
it still refused. Fixed after review: the `stripe_customer_id` constraint
had no `IntegrityError` test; the migration backfill was untested; the
inactive-organization role check ran only the dispatcher; the same-endpoint
take-over after onboarding and the older `invoice.subscription` shape were
untested; one test name claimed the block was absent when it is `exempt`.
*Gaps left open:* no page component has a spec (same as every phase):
`settleReturn`, `subscriptionAction` and the `App.vue` alert are exercised
by hand; `OrganizationAdmin`'s fieldsets are untested, like every admin;
no explicit "owner of A with B's header" test on the three endpoints
(`TenantMiddleware`'s own tests cover it).

**2. DRY.** Fixed: the card composed dated sentences on the client for
the quiet trial and active states and carried three unreachable fallbacks
(ADR-023) -- the server publishes `summary` now and the card renders it;
`_settings_url` was built twice (`connect.settings_page_url` serves both);
`enabled()` twice; the two return-from-Stripe handlers on the settings
page were one shape (`settleReturn`); three inline `api.post<{url}>`
(`postForUrl`); the owner/non-owner call to action spelled four times
(`_tell`). *Accepted:* `OrganizationSummarySerializer` stays in
`users/serializers.py` by history, and `ORGANIZATION_INACTIVE` in
`base/permissions.py`; moving either is a rename for its own sake.

**3. Modularity.** The dependency direction holds: `billing` imports
`organizations`; nothing outside `billing/` imports it but `seed_demo`
(pre-existing, recorded). `organizations/services.py` imports
`base.permissions` (one string) and `users.enums`; `users/serializers.py`
imports `organizations/serializers.py`; a fresh-interpreter import of each
module first finds no cycle. `stripe` is imported in `connect.py`,
`subscription.py` (through `connect.client`) and `webhook.py` only.

**4. Orthogonality.** Recorded in `CLAUDE.md`: the six platform event
types (`PLATFORM_HANDLERS` and runbook step 10); the settings page's path
and `?subscription=` keys (`connect.settings_page_url`, the page);
`allowed_while_inactive` (three views, `base/permissions.py`);
`ENUM_NAME_OVERRIDES` gains the two `state` enums and `NoticeLevel`; and
the three readers of the pinned API version's object shapes
(`paid_through`, `_subscription_id_in`, the fixtures), which a version
bump must re-read. The enforcer's hour is now before the materializer's,
and the comment says why.

**5. Security.** `/security-review`: no findings above its bar. The gate
reviewer found one MEDIUM and three LOW, all fixed:

- **MEDIUM -- the tenant's customers were served its billing state.** The
  notice, banner and full block went to `customer`-role members through
  the session and `GET /api/organizations/current/`. `billing_notice` and
  `billing_summary` answer nothing for a non-staff role, and the serializer
  blanks the block but for the state word. Tested per role on both routes.
- **LOW -- `refresh_subscription` did not check the Subscription's
  `customer`.** It refuses to mirror one that is not the organization's.
- **LOW -- `paid_through` crept forward across unpaid cycles.** An unpaid
  read never moves the stored date forward. Tested with a second unpaid
  cycle, and a paid read still moving it.
- **LOW -- a billing-deactivated organization could not be kept off by
  hand.** The admin can clear the stamp; the description says when to.

Also fixed from `/security-review`'s notes: the owner of a hand-deactivated
organization could buy a subscription that would never restore it (409
now). *Accepted:* no throttle on the three owner endpoints (as 4b's).
Invariants: `organization` and every subscription field are written only
from the resolved tenant, a re-read of Stripe, or the admin (`is_active`,
`billing_exempt`, `trial_ends_at`, `deactivated_for_billing_at`); nothing
in the block is writable through the API (tested); both tasks take
`organization_id`; every new view declares its permission; the platform
webhook is a plain Django view, `csrf_exempt`, POST-only, verified with
its own secret, the body never logged. `AllowAny`, unchanged from 4b:
`PayInvoiceView`, `PayInvoiceCheckoutView`, the health probes, the auth
views. `csrf_exempt`: `StripeWebhookView`, now mounted twice
(`/api/billing/stripe/webhook/`, `/api/billing/stripe/platform-webhook/`).
`allowed_while_inactive`: the three subscription views, and no other.
Deploy audit clean. No secrets in git, `.env.example` or the bundle.
Local rehearsal: the image rebuilt with the gate fixes, the stack up under production
settings behind Caddy, seeded; readiness `ok`, `/schedule` 200 with
`no-cache`, an API 404 is Django's, http 301s to https, an asset is
immutable; a sign-in over TLS as the seeded trial owner (202, the code
from the web log, verify 200) reads the billing block and the session
summary as `exempt` with the trial stamped five days out, because the
rehearsal has no price configured -- the documented "nothing to show"
shape -- and checkout, portal and the unsigned platform webhook each
answer 503 "Subscriptions are not configured on this server." Torn
down with `-v`.

**6. Branch and production guard.** All of Phase 4c is on
`phase-4c-platform-subscription`, branched from `main` at the fee-backfill
merge. Nothing was committed to `main` and nothing was pushed by an agent.
Staging exists and was not touched: no `fly` command was run, no secret
set; the platform price and secret are not set there, so staging runs
this branch with the subscription off until Jordan follows
`docs/DEPLOY.md` step 10.

---

## Customer import as built

Branch `feat/customer-import`, 2026-11-16, ahead of the work trial.
The decisions and what was rejected are ADR-031; this is what exists.

**The column set is a best guess.**
No real export from the business was available when this was written.
The header-to-field table is `app/customers/import_columns.py` and it is the only place a header is named, so the first real file is expected to change that table and nothing else.
The template, the required-column check and the unknown-column report are all built from it.

**Backend.**
`POST /api/customers/customers/import/`: multipart, a `file` and a `dry_run` that defaults to true, owner and admin only.
`GET /api/customers/customers/import-template/`: the template CSV as text, each column with whether it is required and a hint, and the limits, as JSON.
`customers/services.py` reads the file, plans every row, and writes the plan only for a real import with no error row.
Rows are validated by `CustomerSerializer` and `ImportedLocationSerializer` (a `ServiceLocationSerializer` whose `customer` is supplied by the importer); required columns are read off those serializers.
A real import plans and writes in one transaction behind a lock on the organization's row.
No migration: no model changed.

**Columns.**
Customer: `first_name`, `last_name`, `company_name`, `email`, `phone`, `preferred_contact_method`, `status`, `source`, `customer_notes`, `billing_line1`, `billing_line2`, `billing_city`, `billing_state`, `billing_postal_code`.
Location: `location_label`, `address_line1`, `address_line2`, `city`, `state`, `postal_code`, `square_feet`, `bedrooms`, `bathrooms`, `parking_notes`, `access_notes`, `gate_code`, `alarm_code`, `key_location`, `has_pets`, `pet_notes`.
Required: `address_line1`, `city`, `state`, `postal_code`, and at least one of the three name columns per row.
Headers match without case, with spaces and hyphens read as underscores.

**Blank cells.**
A blank cell is "not given", so the field's own default applies.
One exception, decided by Jordan on 2026-11-16: a blank `status`, or a file with no `status` column, imports as `active`, because a customer book is customers and not leads.
The API's own default for a customer created the normal way is still `lead`; the model and `CustomerSerializer` were not touched.
It is one attribute, `when_blank`, on the `status` row of the column table in `customers/import_columns.py`.
The value goes through the serializer like a typed one, a status that is given is kept as written, a bad one is still a row error, and a customer already on file is never made active.
The template's hint for the column tells the person filling it in: "Blank is imported as active."

**Matching.**
One key per customer: the email without case, or with no email the whole name plus the phone's digits.
A customer on file is never edited; they gain a location only at an address (line 1, line 2, postal code) they do not already have.
A row that matches two stored customers is an error.
Importing the same file twice adds nothing.

**Limits.**
1 MB and 2000 rows, UTF-8 with or without a byte order mark.
Measured once on a laptop at 2000 rows with access codes: 2.8 s to preview, 6.2 s to import, 2.7 s to re-import, a 330 KB response.
The worker timeout is 60 s.

**A read-only organization (Phase 4c, ADR-028).**
The branch was rebased onto `main` at `6ef6f78`, which carries Phase 4c, on 2026-11-17.
4c makes an organization read-only by setting `is_active` false, either by hand or from `organizations.services.apply_coverage` when a trial or subscription has run past its grace, and `IsOrgMember` then refuses every unsafe method with `ORGANIZATION_INACTIVE`.
The import needed no change to obey that: both actions state `IsAdminOrHigher`, which inherits `IsOrgMember`, and neither sets `allowed_while_inactive`.
A real import and a dry run are both refused with a 403 whose body is the same as the one creating a customer by hand gets; the template, a GET, is still served.
A dry run is refused too because it is a POST and the rule is by method, and that is the right answer: a preview of a write that cannot happen would end at an Import button the server refuses.
Inside the grace period, with the organization still active, an import works like any other write.
One thing was added. The permission check reads `is_active` off the row the request loaded, and a real import then waits for the organization's row lock, which 4c's checkout holds across calls to Stripe; the platform webhook can switch the organization off in between.
The row is read again under the lock, and an organization that was active when the request was let in and is not now is refused with the same sentence.
The lock itself does not fight 4c: the import takes that one row lock and nothing 4c holds, so the webhook's update of the row waits a few seconds for the import to commit and neither can deadlock the other.

**Frontend.**
An Import button on the Customers page for owner and admin opens `components/CustomerImportDialog.vue`: choose a file, preview, confirm, result, and the list reloads.
The file is uploaded, never parsed in the browser.
Under the file picker a closed "What goes in each column" disclosure lists the server's hint for each column that has one, which is where the uploader reads that a blank status is imported as active.
The flow is `useCustomerImport` in `lib/customerImport.ts`; the template is saved by `lib/download.ts` from the text the API returns, because a plain link could not carry `X-Organization`.

**Not looked at in a browser.**
Nothing in this section was seen in a running browser: not the Import button, not the dialog, not the column hints disclosure, not the download, not 390px.
The dialog has a mounted spec that proves the markup is wired to the flow and shows the server's wording; it says nothing about layout.

Backend 1234 → 1349. Frontend 146 → 198.
Both are counted from `main` at `6ef6f78`; before the rebase, on `7843a71`, they were 1049 → 1154 and 131 → 183.

### Phase gate

**1. Test coverage.**
`customers/tests/test_import.py`, 115 tests: each role on each side of the line for both endpoints (owner and admin in; dispatcher, cleaner, customer and anonymous out, for a dry run as well as a real import), a read-only organization (lapsed through 4c's own `apply_coverage` and switched off by hand, real import and dry run, the refusal compared with the create path's, the grace period, reactivation, and the recheck under the row lock), the dry-run default with row counts before and after, all-or-nothing including a failure half way through the write, the import's status default (blank and missing are `active`, a given status is kept, the preview and the import agree, an existing customer is untouched, the create path still says `lead`), matching, re-import, the tenant boundary (an import in A creates nothing in B, matching never sees B, a forged `organization` in the form or as a column is inert), access codes absent from the response and the captured logs and present through the model, the raw column and the audited reveal, the file hygiene cases, the template (which must import as it stands), and two identical runs compared byte for byte.
Each safeguard was broken on purpose once to confirm a test goes red: the dry-run default, the transaction, the tenant filter in matching, writing on a dry run, writing despite an error row, logging a row, echoing a code, reading `organization` from the form, removing the status default, and letting that default override a status that was given.
Frontend: `lib/customerImport.spec.ts` (the flow and the arranging), `lib/download.spec.ts`, `api/endpoints.spec.ts` (what `dry_run` puts on the wire), `components/CustomerImportDialog.spec.ts` (mounted).
*Gaps left open:*
no browser has seen any of the UI, at any width;
`CustomersPage.vue` has no spec, like every page, so the role gate on the Import button and the reload after an import are untested;
the organization row lock is untested, because two imports at once need two connections and the suite runs each test in one transaction;
the 2000-row timing was measured by hand and is not asserted;
a superuser importing through the API is untested (the service is, for a deactivated organization);
the recheck under the row lock is tested at the service with a stale organization object, not with two real connections racing;
the real multipart request from a browser was never sent to a running API (the spec pins the form, the backend tests use DRF's encoder);
a file of exactly 1 MB is untested (over it is).

**2. DRY.**
Validation is the create path's serializers and nothing else: "required" and the column hints are read off them, and a test compares an import error with the serializer's own message.
The header table is one tuple; the test file imports it rather than restating it.
The customer and address shown for a row come from the models' `display_name` and `one_line_address`.
The upload size cap reuses `base.validators.MaxFileSize`.
The UI's failure message goes through `api/errors.errorDetail`.
*Accepted:* the three group headings in the dialog ("Needs fixing", "Will be imported", "Already on file") are the UI's own words beside the enum's labels; every sentence about a row and every error is the server's.

**3. Modularity.**
The view parses the upload form and returns the result; everything else is `customers/services.py`.
The page reaches the API through `api/endpoints.ts` only, and the dialog's logic is in `lib/`.
`customers/services.py` imports `organizations.models` for the row lock; `organizations` imports nothing from `customers`, so the dependency runs one way.

**4. Orthogonality.**
Couplings this adds, for `CLAUDE.md`:
`ENUM_NAME_OVERRIDES` gains `CustomerImportActionEnum`, which would otherwise generate as `ActionEnum`;
`lib/customerImport.ts` groups rows by the `ImportAction` values, so a new action touches the enum, the schema and `groupRows`;
the Import button on `CustomersPage.vue` is one more mirror of the admin tier (`session.isAdminOrHigher`);
a spec that mounts a Vuetify component needs `server.deps.inline: ['vuetify']` in `vite.config.mts` and stand-ins for `visualViewport` and `ResizeObserver`.
A change to a serializer rule needs no edit here, which a test asserts.

**5. Security.**
Both actions state `IsAdminOrHigher`; nothing is `AllowAny`.
Neither sets `allowed_while_inactive`, so a deactivated organization cannot import (ADR-028), and a test pins that.
`organization` comes from `get_organization()` and is stamped in `_write` (invariant 2); no model was added (invariant 1) and no task (invariant 3).
No column maps to `user`, `organization` or `customer`, and a test pins that.
Access codes go from the cell to the serializer to the encrypted field; a row's outcome has no field for one, the log line is counts, and an existing location's codes are never overwritten.
`/security-review` on the branch diff: no finding at high or medium.
It did turn up one correctness bug, fixed: an error on a field with no column was dropped, so the row previewed as importable and the real import answered 500 (nothing was written).
*Accepted (the first two are in ADR-031):* a file with shifted columns can have a misplaced cell shown back to the person who uploaded it; a cell starting with `=` is stored as text and would be a formula to a future unguarded CSV export; the import holds the organization's row lock for its duration, which blocks that one tenant's settings writes for a few seconds.
No error tracker that captures local variables is configured; if one is added, `customers/services.py` holds plaintext codes in locals while it runs and needs scrubbing there.
The deploy audit and rehearsal were not run: no setting that production reads, no image or `deploy/` file changed (the one settings edit is a schema enum name).

**6. Branch and production guard.**
All of it is on `feat/customer-import`, cut from `7843a71` and rebased once onto `main` at `6ef6f78` (2026-11-17, at the orchestrator's instruction, before anything was pushed).
Nothing was committed to `main`, nothing was pushed or merged, no `fly` command was run, and no migration was made or applied.

---

## Visit booking as built

Branch `feat/visit-booking`.
A dispatcher can now book a one-off visit from the schedule and edit, move or delete a visit from its page.
Before this the only way onto the board was a recurring plan, and a visit could not be moved.

**Backend, first.**
`JobViewSet` already had create, update and delete; no page called them, so what they accepted had never been exercised.

- `Job.available_actions` (`edit`, `delete`) is published per caller and enforced by the same two functions in `scheduling/services.py` (ADR-032).
  A finished visit answers 409 to PATCH and PUT until it is reopened.
  DELETE is for a one-off visit that is still scheduled and has no recorded time; a plan visit is cancelled instead.
- `assignees` on job create: a write-only list of user ids, assigned in the same transaction through `assign_user`.
  Refused on update.
- `regenerate_plan` re-keys each kept visit to the new definition's occurrence on the local day it was planned for, so a plan time change no longer adds a second visit to a day that already has a moved or cancelled one (ADR-032).
- Each plan visit is stamped with what the plan wrote (`planned_price_cents`, `planned_end`, `planned_notes`), and a visit is untouched only while its price, end and note still equal the stamp (ADR-032, amending ADR-020). A price set by hand, a longer slot or a note now survives a plan edit. Migration `scheduling/0003_plan_stamps` adds the columns, backfills every existing plan visit in one UPDATE, and corrects the help text on `plan_occurrence`.

**On top of Phase 4c.**
The branch was rebased onto `main` after the platform subscription landed (ADR-028).
A deactivated or lapsed organization is read-only, and booking, moving, editing, deleting and reopening a visit are refused there with the same 403 as every other write, because every one of them sits behind `IsOrgMember`.
The job also stops offering them: `next_statuses` and `available_actions` are empty in a deactivated organization for everyone but a superuser, who is the one caller the gate lets through.
The nightly materializer skips such an organization; when it comes back the horizon catches up with stamped visits, and visits kept or cancelled before the lapse still account for their days.

**Frontend.**
`VisitDialog` is used by both pages: New visit on `SchedulePage` (and a plus on each day), Edit visit and Delete visit on `JobDetailPage`.
What the form means lives in `src/lib/visitForm.ts`; `src/lib/datetime.ts` gained `zonedInstant` and `addMinutes`.
The dialog and both pages are mounted in vitest (jsdom), which needed `server.deps.inline: ['vuetify']` in `vite.config.mts`.

**Answers the brief asked for.**

1. A one-off job is stamped with the caller's organization whatever the payload says; another organization's customer, location or service is a 400; a cleaner or customer gets 403 on create, update and delete.
2. A plan visit moved by hand stays linked to the plan, survives `regenerate` and a plan edit, and its old slot is not refilled.
   The plan-edit case was broken for a time change and is fixed (above).
   A hand edit that leaves the start alone (price, length, note) was lost on the next plan edit and is now kept, by the stamp.
   A hand-added assignee is still not kept, which is ADR-020's accepted cost.
3. DELETE soft-deletes. On a plan visit it only hid the visit until the nightly run, and on a cancelled plan visit it un-cancelled it. Both are now 409.
4. A finished visit could be moved, repriced, handed to another customer or deleted, including one on an issued invoice. Now 409 until reopened.
   A visit that is not finished stays editable with recorded time on it.
5. Double booking is accepted silently on create, assign and move. No rule was added.

**Not verified in a browser.**
No browser has looked at `VisitDialog`, the New visit button and per-day plus on the schedule, the booked notice, or Edit visit, Delete visit and the delete confirmation on the job page.
That includes the 390px layout and the full-screen dialog.
The jsdom specs cover which controls exist and what they send, not how anything looks.

### Phase gate

1. **Test coverage.** Backend 1234 (main with Phase 4c) to 1399 (+165; it was 1049 to 1201 before the rebase, and 13 were added for the deactivated organization in `test_inactive_organization.py`): `test_job_actions.py`, `test_booking.py`, `test_rescheduling.py`, `test_plan_edit_days.py`, `test_plan_stamps.py`, `test_migration_plan_stamps.py` (the backfill run for real: rewind the migration, run it forward, read the rows), and one seed test. Frontend 146 to 243 (+97): `datetime`, `errors`, `jobStatus`, `visitForm`, `VisitDialog`, `JobDetailPage`, `SchedulePage` specs. Cross-tenant tests for job create (customer, location, service, assignee), update, delete, and the plan `regenerate` action; role tests on each side of the line for create, update, delete and `available_actions` (owner, admin, dispatcher, cleaner, customer). Gaps: no test drives the customer autocomplete or any Vuetify menu (jsdom does not do them faithfully, the two picks are made through the handlers); the 250ms customer-search debounce and its stale-response guard have no spec; `removeNote` and the rest of `JobDetailPage` remain as untested as before; no test covers `_rekey_kept_jobs` for a rule that fires more than once a day; the backfill test is the first transactional test in the suite and the only one that runs migrations; nothing was run in a browser.
2. **DRY.** Date and time conversion is only in `lib/datetime.ts`; money only through `lib/money.ts`; one dialog serves create and edit; `JobSerializer._caller` replaced a block that `available_actions` would have duplicated from `next_statuses`. The spec files repeat a small jsdom setup (ResizeObserver and visualViewport stubs, a session patch) three times; left, pending a shared test helper.
3. **Modularity.** Pages reach the backend through `src/api/endpoints.ts` only. The rules are in `scheduling/services.py`; the view and serializer call them. `JobSerializer.create` assigns through `services.assign_user`.
4. **Orthogonality.** No new cross-app import. New couplings, noted in code: `FIELD_FOR` in `ui/src/lib/visitForm.ts` maps the API's field names (and the pricing 400's `square_feet`) to form inputs; `JobAction` in `ui/src/api/types.ts` mirrors `JOB_ACTIONS` in `scheduling/services.py`; `zonedInstant` resolves a skipped or repeated wall-clock time the way `expand_occurrences` does (`fold=0`), and the two must keep agreeing.
5. **Security.** No new model, endpoint, task or `AllowAny`. `organization` is still stamped from the tenant, tested with a forged payload. `assignees` resolves user ids outside the tenant scope by necessity (`CustomUser` is not a `TenantModel`) and is admitted only through `assert_can_be_assigned`; an unknown id and another organization's cleaner get the same message. Update and delete are stricter than before, never looser. Edit and Delete are enforced server-side and only drawn from `available_actions`. No secrets, nothing logged, no access-code path touched. Settings, image and `deploy/` untouched, so no deploy audit or rehearsal was due. `/security-review` was NOT run as a skill; the diff was reviewed by hand against this list. Open finding, not fixed: a visit on a live invoice can be reopened and then edited or deleted (ADR-032, left open).
6. **Branch and production guard.** All work is on `feat/visit-booking`, cut from 7843a71 and rebased onto `main` at 6ef6f78 on the orchestrator's instruction. Nothing committed to `main`, nothing pushed or merged, no `--no-verify`. No `fly`, staging or production access, and no `migrate` against the dev database. One migration was added, `scheduling/0003_plan_stamps`, and it has not been run against anything but the test database.

---

## Billed visit guard as built

Branch `fix/billed-visit-reopen`, cut from the tip of `feat/visit-booking` and restacked onto it after that branch was rebased onto `main` with Phase 4c.
Closes the finding the visit booking gate left open: a visit on a live invoice could be reopened through the status action and then edited or deleted.

- `scheduling.services.register_job_claim_check` is a registration hook; `BillingConfig.ready()` registers `billing.services.visit_claims` (ADR-033). Nothing under `scheduling/` imports `billing`, and a test fails if it ever does.
- A visit a live line points at (`live_lines()`: draft or issued, not voided, not deleted) offers no reopen and refuses one with a 409 in billing's words; it refuses edit and delete the same way. Voiding the invoice, deleting the draft or removing the line frees it.
- A draft holds a visit too, because nothing at issue re-checks that a draft's visits are finished: before this, a visit reopened while on a draft was issued and billed while `scheduled`.
- The job list asks about a whole page in one query (`JobListSerializer`); forward moves never ask.
- The platform subscription (Phase 4c, ADR-028) is not a claim. It lives on `Organization`, not in `Invoice`, and `visit_claims` reads invoice lines only; a test walks four subscription states. `BillingConfig.ready()` registers exactly one check, and nothing else in the project has a `ready()`. In a deactivated organization the read-only gate answers first (403), and the claim is still there when it comes back.
- No schema change, no migration, no frontend change beyond two specs: the job page already draws its buttons from `next_statuses` and shows a 409's `detail`.

Checked and left alone: clock-out of an open entry, unassign, a dispatcher's time-entry correction and a new job note still succeed on a billed visit. None changes what the invoice bills. The Django admin bypasses all of it.

### Phase gate

1. **Test coverage.** Backend 1399 to 1469 (+70): `billing/tests/test_visit_guard.py` (the reproduction, draft and issued and voided, both finished statuses, published equals accepted for reopen across four billing states and for edit and delete across every status, the list, query count, cross-tenant) and `scheduling/tests/test_claim_guard.py` (the hook with a stand-in check, nothing registered, one call per page, the import test). Frontend 243 to 245: two `JobDetailPage` specs. Gaps: no test with two real registered apps (only billing exists; two stand-ins are tested); the admin path is untested; nothing was run in a browser.
2. **DRY.** One claim lookup (`job_claims`) feeds `next_statuses`, `available_actions`, both 409s and the `allowed` list. `visit_claims` reuses `live_lines()` rather than restating it.
3. **Modularity.** The rule is in `billing/services.py`, the hook in `scheduling/services.py`; views and serializers call them.
4. **Orthogonality.** One new coupling, by design and recorded as ADR-033: billing registers with scheduling at startup. The dependency still runs one way and a test enforces it.
5. **Security.** No new endpoint, model, task or `AllowAny`. The change only refuses more. The claim check filters on the job's own organization and a test forces a cross-tenant line in underneath `TenantModel.save()` to prove it is ignored. A cleaner is never told about the invoice. `/security-review` was NOT run as a skill; the diff was reviewed by hand.
6. **Branch and production guard.** All work is on `fix/billed-visit-reopen`. Nothing committed to `main`, nothing pushed or merged, no `--no-verify`, no `fly`, no `migrate` against the dev database.

---

## Reports as built

Branch `feat/reports`, cut from `main` at `7843a71` on 2026-11-16 and rebased the next day onto `6ef6f78`, which had gained Phase 4c and the no-mistakes gate.
Three read-only reports for the owner's first two questions, "what do I owe my cleaners this week" and "who owes me".
No model change and no migration.
What each report means is ADR-030; read it before changing a number.

| Report | Endpoint (each has a `csv/` twin) | Takes | Lives in |
|---|---|---|---|
| Timesheet | `GET /api/scheduling/reports/timesheet/` | `date_from`, `date_to` (required, organization-local, inclusive, at most 62 days) | `scheduling/reports.py` |
| Receivables | `GET /api/billing/reports/receivables/` | `as_of` (optional, defaults to the organization's today) | `billing/reports.py` |
| Collections | `GET /api/billing/reports/collections/` | `date_from`, `date_to` (required, at most 366 days) | `billing/reports.py` |

All six are owner and admin only (`IsAdminOrHigher`).
Each is one query whatever the number of staff, customers or payments, and is ordered with a unique tie-break so the same data gives the same bytes.

- **Timesheet.** Per staff member: total seconds, entry count, open-entry count, and the entries.
  An entry belongs, whole, to the local day it was clocked in on; an open entry is listed, flagged, and counts for nothing.
  The bounds are `Organization.local_day_bounds`, so this is not a third way of computing a local day (the baseline gate's complaint).
- **Receivables.** Per customer: outstanding balance in five aging buckets by due date, with totals.
  The balance is `billing.services.balance_cents`; `services.with_paid_cents` lets it be asked of every unpaid invoice in one query, and `paid_cents` reads the carried figure when it is there.
- **Collections.** Per payment method: count, amount, tips, provider fees reported so far, net, and how many card payments have no fee yet.
  A payment is filed under `received_on`; a voided payment is in no period.
- **CSV.** `base/exports.py` is the one writer.
  Text cells that would start a formula are prefixed with an apostrophe and every text cell is quoted; amounts are decimal dollars in columns named `..._usd`; the timesheet carries exact `seconds` beside `hours_decimal`.
- **Frontend.** `/reports` (a new `admin` route tier) with a period picker (this week, last week, this month, last month, custom) reckoned from the organization's today, a Timesheet section, a Money section, totals, and three CSV buttons.
  The page adds nothing up.
  Its logic is in `src/lib/reports.ts`, `loadable.ts`, `latest.ts` and `download.ts`, each with a spec, and `ReportsPage.spec.ts` mounts the page against a mocked API.
- **Seed.** `seed_demo` now records a time entry for each assigned cleaner on each finished visit, as its own additive pass, so the timesheet is not empty on a seeded database and an older seed gains its hours on `--force`.
- **Beside Phase 4c.** Neither meeting point needed a code change, and both are tested.
  A lapsed organization is read-only and reports are reads, so its owner can still read and download all six; none of the report views sets `allowed_while_inactive`, because `IsOrgMember` only refuses unsafe methods.
  The platform subscription is a second money relationship and is in neither money report: platform events write `StripeEvent` rows and fields on the organization, never an `Invoice` or a `Payment`, which are the only tables the reports read.

Also in the branch: the jobs filter's date-range check moved to `base.validators.validate_date_range` unchanged, so the reports did not add a second copy of it.

Backend 1234 → 1470. Frontend 146 → 216 (12 spec files → 17).
Before the rebase, against `7843a71`, the same work read 1049 → 1265 and 131 → 200.

### Phase gate

1. **Test coverage.**
   Every report has pytest tests for each role on each side of the line (owner and admin in; dispatcher, cleaner, customer and anonymous out), for both the JSON and the CSV form, and a cross-tenant test in which the two organizations work different hours and bill differently, so a wrong-tenant read is a wrong number.
   Against Phase 4c: an organization deactivated by the coverage rule itself still reads and downloads all six with the figures it had before, a card payment arriving after the lapse is reported, a lapse lets no extra role in, and every event type in `PLATFORM_HANDLERS` leaves the `Invoice` and `Payment` tables and all four money endpoints byte for byte as they were.
   Also covered: someone who owns one organization and cleans for another is refused the second one's reports, a superuser gets a report only with an organization named, a deactivated organization can still read them, the local day of an entry that crosses midnight or a period boundary, a 23 and a 25 hour day in Denver and a 25 hour day in Auckland, open entries, a corrected entry, every edge of every aging bucket, the ledger agreement test, soft-deleted entries, jobs, customers, invoices, lines and payments, flat query counts, byte-identical repeat responses, and formula injection with `=HYPERLINK("http://x","y")` and a typed `-10`.
   The tests were checked for being able to fail by breaking the code under them: 15 mutants of the money reports (13 caught; one survivor was a redundant `order_by` under a comment that was wrong, both since removed, and the other is the fetch-narrowing filter, which changes no answer by design and says so), the timesheet's tenant, soft-delete, open-entry, day-length and formula rules one at a time (all caught), and 14 mutants of the frontend (all caught).
   **Gaps, all of them:**
   no browser has looked at `/reports` at any width, so the layout, the table scrolling inside its own box at 390px, the expansion panels and a real file landing in a real downloads folder are unverified;
   no CSV has been opened in Excel, Numbers or Sheets, so the apostrophe prefix, the quoting and the byte-order mark are verified against the bytes and not against a spreadsheet;
   the router guard's new `admin` branch has no spec (the guard has none at all, a baseline gap) and neither has the navigation item in `App.vue`;
   the functions in `ui/src/api/endpoints.ts` are covered only through the page spec, which asserts their paths and parameters;
   nothing measures the reports against a large tenant, only that the query count is flat.
2. **DRY.**
   One CSV writer, one date-range check (the jobs filter now calls it too), one balance formula, one definition of a live payment (`services.NOT_VOIDED`).
   Left in place: `read_csv`, a three-line test helper, is written in three test modules; each report has a JSON view and a CSV view that differ in their last line.
   The frontend formats seconds through the existing `formatDuration` and takes method labels from the API rather than `PAYMENT_METHOD_LABEL`.
3. **Modularity.**
   The page reaches the API only through `ui/src/api/endpoints.ts`.
   Report logic is in `<app>/reports.py` rather than `<app>/services.py`, a deliberate departure from the convention: both `services.py` files are long, and a report decides nothing the seed or a task needs.
   `billing.reports` goes through `billing.services` for every money rule.
4. **Orthogonality.**
   `scheduling` still imports nothing from `billing`; both import `base.exports`.
   New couplings, each noted in code:
   the timesheet's period cap is the jobs filter's `MAX_RANGE_DAYS`, so changing one changes the other;
   `services.PAID_ANNOTATION` is a contract between `with_paid_cents` and `paid_cents`, and an annotated invoice must never reach a write;
   the aging field names appear in `billing/reports.py`, `billing/serializers.py` and `AGING_LABEL` in `ui/src/lib/reports.ts`, the last enforced by the type-check;
   a report's download filename is spelled in the view's `Content-Disposition` and in `csvFilename` in `ui/src/lib/reports.ts`;
   the `admin` route tier in `ui/src/router/index.ts` and the navigation item in `App.vue` mirror `users.enums.ADMIN_ROLES`;
   `ui/vite.config.mts` inlines Vuetify for vitest so a spec can mount a page.
5. **Security.**
   No new model.
   The six views are `APIView`s, so the tenant comes from `request.organization` and is passed to the report as an argument; nothing reads it from request data, and each query filters on it.
   Every view states `IsAdminOrHigher`; nothing is `AllowAny`, and nothing sets `allowed_while_inactive` (the list of views that do is still the three subscription views from Phase 4c).
   No authorization lives only in the UI.
   Joins across soft-deletable relations are each decided and tested (ADR-030, point 5).
   No access code is read, nothing is logged, no secret is added.
   `/security-review` was run on the branch diff by a reviewer that had not seen the code: no reportable finding in tenant isolation, authorization, CSV output, data exposure, the `paid_cents` change, the frontend or the seed.
   It noted one thing below its bar, an unquoted `;` or tab letting a formula start a new cell in a spreadsheet that splits on those, which is closed (every text cell is quoted) and tested.
   Settings, the image and `deploy/` are untouched, so the deploy audit and the rehearsal were not re-run.
6. **Branch and production guard.**
   All of it is on `feat/reports`; nothing was committed to `main`, nothing was pushed or merged, and no `fly` command, staging or production access, or migration was run.
   The branch was rebased once, onto local `main` at `6ef6f78`, before anything had been pushed; the two generated schema files were regenerated at each commit that carries them rather than merged by hand.
   Tests ran against a database of their own (`pg_reports`); the development database was not touched.

---

## Team management as built

Branch `feat/team-management`, on `main` at cbe39bd (after Phase 4c, the customer import, visit booking and reports).
Backend 1820 → 2185 (+365), frontend 367 → 427 (+60, 24 spec files → 28).
Decision record: ADR-034.
The problem was that `MembershipViewSet` was read-only, so every new cleaner and every lost password meant the developer in the Django admin.

**How customer memberships come to exist today.**
Only two ways, and neither is an API: the Django admin (`MembershipAdmin`, or the inline on a user), and `seed_demo`.
`CustomerSerializer.user` then links a customer record to a user who already holds an active customer membership in the caller's organization.
Nothing here changes that: `customer` is not a role the team page can invite, grant or act on.

**Invitations.**
`POST /api/users/memberships/` takes an address and a staff role and creates the membership inactive, with `invited_at` set.
Following the emailed link (`/accept-invite/<token>`) activates it and stamps `accepted_at`.
Until then nothing that reads `is_active` counts it, so an invitation changes nothing for its recipient, in this organization or any other.
An address that already has an account gets a membership and nothing else: the account is not written to, its name is not shown until it joins, and the response is the same as for a stranger.
`Membership.state` (invited, active, deactivated) is derived; `deactivated_at` is the one new column.

**The team rules.**
An owner manages anyone; an admin manages dispatchers and cleaners only; nobody changes their own role or deactivates themselves; the last active owner keeps both the role and the membership, checked under a lock.
`users.services._refusal` states them once.
The writes (`PATCH` for the role, `deactivate`, `reactivate`, `resend-invite`) enforce it, and every row is served with the `available_actions` and `assignable_roles` it yields for the caller, with the invitable roles at `memberships/invite-options/`.
There is no PUT and no DELETE.

**The token.**
`users.AccountToken`, a sibling of `MagicLinkToken`: hashed, single use, expiring, with a `purpose` (invitation, 7 days; password reset, 1 hour) required by every lookup.
Issuing withdraws the earlier one for the same thing.

**Accepting.**
`POST /api/users/invitations/inspect/` says what a link opens and whether the account has ever had a password; `POST /api/users/invitations/accept/` joins, and sets a name and password only for an account that never had one.
Neither signs anyone in.

**Staff password reset.**
`POST /api/auth/password-reset/` answers 202 with one body for every address and does the same work for every address: the view enqueues `two_factor.send_password_reset`, and the worker decides and sends.
`POST /api/auth/password-reset/confirm/` sets the password, ends every session, forgets every trusted device, withdraws every outstanding 2FA code and ends any sign-in that was waiting on one.
It signs nobody in.
Active staff only: not customers, not invitees, not platform superusers.

**A fix found on the way.**
The session payload listed a deactivated membership, which the middleware then refused; it now lists only what the middleware would resolve.

**A fix found by the review.**
Withdrawing 2FA codes on reset closed `/api/auth/verify/` and left `/api/auth/resend/` open: a sign-in started with the old password could ask for a fresh code and finish.
`ResendView` now treats a spent code as the end of the sign-in it belonged to.
Reproduced with a test first.

**On top of Phase 4c.**
In a read-only organization the five team writes are refused by `IsOrgMember` with its own sentence, and no row offers an action: the rule function carries the same condition and superuser exemption.
Sign-in, password reset (request and confirm) and accepting an invitation all still work there, because they never reach `IsOrgMember`; refusing them would lock out the owner who has to get in to pay.
`users/tests/test_read_only_organization.py` pins each.

**Frontend.**
`/team` (admin tier, in the nav), `/accept-invite/:token`, `/forgot-password`, `/reset-password/:token`, a "Forgot password?" link on the login page, and `/no-access` for someone signed in but active in no organization.
The team page keeps no role table: menus come from `available_actions`, the role select from `assignable_roles`, the invite dialog from `invite-options`.
No tier changed; the router gained an `admin` access tier that reads the existing `isAdminOrHigher`.

**Not verified.**
No browser has looked at any of these pages, at any width.
The 390px layout is by construction only (a wrapping chip row, truncating names, one menu button per row).
The emails have been rendered in tests and never seen in a mail client.
The `/no-access` redirect is covered through the session store's `hasNoOrganization` and not as a navigation.

### Phase gate

Reviewed by the author, by mutation (each rule broken in turn to see its tests fail), and by one independent `/security-review` pass over the branch diff.

**1. Test coverage.**
Backend 1820 → 2185 (+365), frontend 367 → 427 (+60).
Every new endpoint, service, model method and the Celery task has direct tests.
Each permission rule is tested from every role on both sides of the line (one table of who may manage whom drives invite, role change, deactivate, reactivate and resend), each tenant-scoped endpoint has a cross-tenant test for every verb, and mass assignment is tested on the invite body and the role PATCH.
Token replay and cross-purpose use are tested in all six directions.
*Gaps left open:* the two-owners race is closed by a row lock that is argued and not exercised, since it needs two concurrent transactions.
The router guard has no spec, so the `admin` tier and the `/no-access` redirect are untested as navigation.
`NoAccessPage.vue` and the login page's new link have no spec.
The email templates are asserted by substring, not rendered and looked at.
The reset task is run eagerly in tests and never against a real broker.
`AccountToken` is not registered in the admin and so has no admin test.

**2. DRY.**
One rule function serves both what a row offers and what a write accepts.
`validated_password` is the one way a public endpoint accepts a password.
The three team specs share `ui/src/lib/team.fixtures.ts`.
*Accepted:* the "render, send, log without the link" block exists once in `users/services.py` and once in `two_factor/services.py`, beside the existing one for 2FA codes; and `failureMessage` lives in `lib/team.ts` though the account pages use it too.

**3. Modularity.**
Pages reach the backend through `ui/src/api/endpoints.ts` and the team store; no page calls `fetch` or axios.
Rules live in `users/services.py` and `two_factor/services.py`, not in views or serializers.
`two_factor` imports `users`; `users` imports nothing from `two_factor`.

**4. Orthogonality.**
Couplings this adds, each noted in code:
the five throttle scopes (`team_invite`, `invite_accept`, `password_reset`, `password_reset_account`, `password_reset_confirm`) are listed in `settings/base.py`, `local.py` and `test.py`;
the paths `/accept-invite/<token>` and `/reset-password/<token>` are spelled in `users/services.py`, `two_factor/services.py` and `ui/src/router/index.ts`;
`TeamAction` in `users/enums.py` is mirrored by the `TeamAction` type in `ui/src/api/types.ts` and the labels in `ui/src/lib/team.ts`;
`ENUM_NAME_OVERRIDES` gained `MembershipStateEnum` and `StaffRoleEnum`;
the session payload's membership filter must match `TenantMiddleware`'s;
`Actor.read_only` in `users/services.py` repeats the condition and exemption of `IsOrgMember`;
the role-tier mirror gained an `admin` route tier in `ui/src/router/index.ts` and the nav gate in `App.vue`;
password reset emails need the Celery worker;
`two_factor.tasks.send_password_reset` takes no `organization_id` (ADR-034);
mounting a Vuetify page in a spec needs `vuetify` inlined in `ui/vite.config.mts`.

**5. Security.**
`AllowAny`, each deliberate and throttled: `InvitationInspectView`, `InvitationAcceptView`, `PasswordResetRequestView`, `PasswordResetConfirmView`.
`MembershipViewSet` states its permission per action: `IsStaff` to read, `IsAdminOrHigher` for everything else, including any action added later.
`organization` is stamped from the resolved tenant on invite and is not a field of any body.
No authorization lives only in the UI.
`AccountToken` is a `Base` model like the other sign-in rows; no API serves it.
Nothing sensitive is logged by the application: no token, no link, no password, tested with the mail both succeeding and failing.
`dev_link` is returned only when `LOCAL` is set, tested both ways for the invitation and the reset.
The deploy audit is clean.
`/security-review` found one issue, the resend bypass above, fixed.
*Open, recorded in ADR-034:* with the second factor emailed, a mailbox alone can now take a staff account through reset; and the tokens travel in the page's path, so a deployed server's access log holds them until they are spent.

**6. Branch and production guard.**
All of this is on `feat/team-management`.
Nothing was committed to `main`, nothing was pushed or merged, and no `fly` command or staging or production resource was touched.
The branch was rebased onto `main` at 6ef6f78, at the orchestrator's instruction, and then onto `main` at cbe39bd, before anything had been pushed.
One migration, `users/0003`; none in any other app.

---

## Ops readiness as built

Branch `feat/ops-readiness`, written on top of `7843a71` and rebased onto `main` at `6ad5eb8` once Phase 4c had merged.
Backend 1049 → 1203 as written, and 1234 → 1400 on the rebased tip; frontend untouched, and the schema did not change.
Everything below was recorded before the rebase; what the rebase changed and what was checked again is in "After Phase 4c" at the end.
Production does not exist yet and only Jordan stands it up, so this is everything the repository can provide: code, settings guards, runbook steps as exact commands, and a local rehearsal that proves the ones that can be proved here.
What the brief asked for, and what each turned into:

**Error tracking → Sentry, ADR-029.**
`app/sentry.py`, started only by `settings/production.py` and only when `SENTRY_DSN` is set; `sentry_sdk` is imported inside `init()` and nowhere else.
A DSN without `SENTRY_ENVIRONMENT` is refused at import, and the release is `FLY_IMAGE_REF`.
What may leave is decided twice: the SDK is told not to collect bodies, locals, cookies or the user, and to send error events only; `before_send` removes them again and redacts by name, and redacts tokens carried in URL paths.
The three states the brief names are tested: no DSN (a child process under production settings reports that neither `sentry_sdk` nor `app.sentry` was imported after the URLconf and every task module were loaded), a DSN with a transport that keeps envelopes (a view that raises, a task that raises and an ERROR log line are each reported, and twelve planted values, in the body, a local, a query string, a cookie, two headers, a task argument, a log `extra`, a SQL parameter, two URL paths and the client address, are in none of them), and production settings importing cleanly through `bin/deploy-audit.sh`, which a test now runs.
A control proves the planted values are there to be seen when nothing removes them.
The SDK patches Django for the life of the process, so the suite never starts it; those tests read a child interpreter's report.

**Email that arrives → two refusals and a runbook.**
`production.py` refuses a `DEFAULT_FROM_EMAIL` that is not an address or is on `example.com`/`.net`/`.org`, and refuses the SMTP backend with an empty `EMAIL_HOST_PASSWORD`.
The placeholders went into `bin/deploy-audit.sh`, the Dockerfile's collectstatic layer and the rehearsal compose file, which all import production settings.
`docs/DEPLOY.md`, "Email", has the SendGrid domain authentication (SPF and DKIM by CNAME, DMARC by TXT), the restricted API key, and a verification with `dig` and `sendtestemail` and the three PASS lines to expect.

**Backup and restore → a runbook, a command, and a drill that was run.**
`docs/DEPLOY.md`, "Backup and restore": what must survive, where `FIELD_ENCRYPTION_KEY` lives and why never beside the dump, how often, the `pg_dump` through `fly proxy`, the media copy with a read-only key, the drill, and the order of a real restore.
`manage.py check_field_encryption` is the drill's last step: it decrypts every encrypted column of every row and prints counts, never a code, so the check can be run on real data without becoming a way round the audited reveal.
The drill was run against the rehearsal stack; its output is below.

**Stripe failed-event replay → an admin action.**
"Re-queue the selected FAILED events" on the Stripe events list hands each row to `process_stripe_event` as `(organization_id, stripe_event_id)`.
The rule is `billing.webhook.replay`: FAILED rows that an organization owns, and nothing else; a row stays FAILED until a worker has applied it.
Refusals are counted on the page, a broker that will not take the message is reported on the page, and each replay is written to the admin log.
Superusers only, on top of the admin's own requirement of a session that cleared the second factor.
Driven over TLS in the production image as well as in pytest: refused with a redirect before the second factor, offered after it, queued through Redis, and applied by the worker.

**The rehearsal → run in full, and it found a dead worker.**
A freshly built image passed its health check with `worker` and `beat` exited: `ModuleNotFoundError: No module named 'packaging'` from `kombu/transport/redis.py`.
`celery>=5.6,<6` now resolves to celery and kombu 5.7.0, where kombu moved `packaging` behind its `redis` extra; the image from five weeks ago (kombu 5.6.2) had it, and nothing declared it.
CI could not have seen it: the suite runs tasks eagerly, pytest brings `packaging` with it, and the image job only checks the build finishes.
On Fly the web check would have passed too.
Fixed by depending on `celery[redis]`, and by importing the broker transport as a build step so the next one fails the image build.
That fix was taken off this branch and merged to `main` on its own, ahead of the rest (`2c67938`), so it is no longer one of this branch's commits.
The rehearsal instructions were right as far as they went and blind in one place: nothing in them looked at `worker` or `beat`, and `up -d` reports success when both start and then exit.
`docs/DEPLOY.md` now starts the checks with `ps`, adds an emailed invoice as the one check that goes through Redis to the worker, and says the same about `fly status` after every deploy.

**Also found: the deploy audit failed on a development machine.**
`python-decouple` fills anything the environment leaves out from `app/.env`, and a `.env` with a Stripe key and no Connect webhook secret tripped `production.py` before the audit ran.
CI has no `.env`, so it only passed there.
The script now sets the optional integrations empty.

### Rehearsal, as run (2026-11-16)

Image built from this branch (Python 3.13.18, celery 5.7.0, kombu 5.7.0, packaging 26.3, sentry-sdk 2.75.0), `up -d`, `seed_demo --force`.
The blocks in this section and the next are the output of the scripts that drove the stack with `curl`, `docker compose` and the AWS CLI.
Timestamps, task ids and scratch paths are trimmed, and where a block says condensed, long output was cut down to the fields named.

```
SERVICE   STATUS
beat      Up 28 seconds
db        Up 4 minutes (healthy)
minio     Up 4 minutes (healthy)
proxy     Up 4 minutes
redis     Up 4 minutes (healthy)
web       Up 28 seconds (healthy)
worker    Up 28 seconds
--- health
{"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
--- /schedule
HTTP/2 200
cache-control: no-cache
--- asset
HTTP/2 200
cache-control: max-age=315360000, public, immutable
--- unknown api path
404 text/html; charset=utf-8
--- http -> https
HTTP/1.1 301 Moved Permanently
Location: https://localhost/
--- sign in over TLS as owner@sparkle-clean.test
session (anonymous) 200
login 202
login body keys: ['detail', 'two_factor_required']
dev_code present: False
code read from the web log: <6 digits, found>
verify 200
session (signed in) 200
{'email': 'owner@sparkle-clean.test', 'current_role': 'owner'}
cookie flags:
  sessionid: secure=TRUE httponly=yes
  csrftoken: secure=TRUE httponly=no
local file sha256: 3bca58e912e0494706c7307462badf49d832b7121e915f1f4782084148e25d82
job: 5e55e54a-48df-44db-ab7e-03adbee4ad4b
upload 201
object url : http://s3.localhost:9000/pink-glove-media/job_photos/2026/11/photo.png
signed with: ['X-Amz-Algorithm', 'X-Amz-Credential', 'X-Amz-Date', 'X-Amz-Expires', 'X-Amz-Signature', 'X-Amz-SignedHeaders']
expires in : ['300']
GET with signature    200
fetched sha256:    3bca58e912e0494706c7307462badf49d832b7121e915f1f4782084148e25d82
GET without signature 403
--- bucket listing, anonymous
anonymous list 403
--- email an issued invoice (Celery -> worker log)
send 202
worker-1  | Subject: Invoice SPK-0003 from Sparkle Clean
worker-1  | From: pink glove rehearsal <noreply@pink-glove.invalid>
worker-1  | To: dana.whitfield@sparkle-clean.test
worker-1  | Task billing.send_invoice_email[task id] succeeded in 0.21s: True
```

Error tracking in the same image (condensed: each event was printed as JSON, and the lines below are its fields):

```
== no DSN (the stack as it runs)
SENTRY_DSN set: False | sentry_sdk imported: False | app.sentry imported: False
== a DSN without an environment
ImproperlyConfigured: SENTRY_DSN is set but SENTRY_ENVIRONMENT is not. Set it to 'staging' or 'production' (docs/DEPLOY.md, 'Error tracking'), or unset the DSN.
== a DSN pointing at a listener on the container's own loopback, standing in for Sentry
   (the real HTTP transport; the login view made to raise, with a password and a gate code in the body)
SINK received POST /api/1/envelope/ (11511 bytes after gunzip)
SINK envelope item types: ['event']
  environment: rehearsal   release: registry.fly.io/pink-glove-rehearsal:deployment-LOCAL
  exception: RuntimeError "rehearsal: the login view failed"   transaction: /api/auth/login/
  request.headers: Cookie [Filtered], Fly-Client-Ip [Filtered], Content-Type, Content-Length, Host
  request.query_string: token=[Filtered]      request.data: absent      user: absent
SINK contains 'PLANTED': False
SINK contains '203.0.113.77': False
== the check docs/DEPLOY.md hands to Jordan, in a container with no network at all
ERROR ... ops error tracking check
SINK received POST /api/1/envelope/   item types: ['event']   message: "error tracking check"   environment: staging
== a worker started with a broker transport it cannot load (the shape of the kombu failure)
SINK received POST /api/1/envelope/   item types: ['event']   KeyError: 'No such transport: nosuchtransport'
```

The image was rebuilt from the branch's final application code after the last change to `app/sentry.py` and rechecked in containers with no network: the broker transport imports, `sentry_sdk` is not imported without a DSN, the documented check delivers one event, and the two email refusals fire:

```
== the example.com sender
ImproperlyConfigured: DEFAULT_FROM_EMAIL='support@example.com' is not an address mail can be sent from. Set it to a sender on a domain you have authenticated (docs/DEPLOY.md, 'Email').
== SMTP with no password
ImproperlyConfigured: EMAIL_BACKEND is SMTP but EMAIL_HOST_PASSWORD is empty, so every sign-in code and invoice would be refused by the mail server. Set EMAIL_HOST_PASSWORD (the SendGrid API key), or set EMAIL_BACKEND to the console backend to print mail instead.
== as configured
System check identified no issues (0 silenced).
```

The admin replay, over TLS:

```
rows: [('evt_rehearsal_done', 'processed'), ('evt_rehearsal_failed', 'failed')]
login 202
--- before the second factor, the admin refuses the action
POST action 302 -> https://localhost/admin/login/?next=/admin/billing/stripeevent/
verify 200
--- after it: the list offers the action, and it runs
GET changelist 200
<option value="replay_failed">Re-queue the selected FAILED events
POST action 302
[success] Re-queued 1 event(s). Each stays FAILED until a worker has applied it: reload in a few seconds.
[warning] 1 event(s) not re-queued: only FAILED events that an organization owns can be replayed (selected: processed).
--- the worker
worker-1  | Task billing.process_stripe_event[task id] received
worker-1  | Task billing.process_stripe_event[task id] succeeded in 0.086s: 'processed'
web-1  | INFO billing.admin Stripe event evt_rehearsal_failed (account.updated) re-queued from the admin by user 365aefd1-...
```

### Restore drill, as run (2026-11-16)

Against the rehearsal stack, with MinIO as the bucket and the AWS CLI given a config and credentials file of its own.

```
### 1. what is in the live rehearsal database
customers.ServiceLocation.gate_code: 2 readable, 0 unreadable
customers.ServiceLocation.alarm_code: 2 readable, 0 unreadable
customers.ServiceLocation.key_location: 0 readable, 0 unreadable
OK: all 4 encrypted values are readable with this FIELD_ENCRYPTION_KEY.
locations|6
invoices|8
payments|4
job photos|1
migrations applied|71
### 2. pg_dump (custom format, no owner, no ACLs)
pg_dump (PostgreSQL) 17.10
200739 bytes  pink-glove.dump
tables in the dump: 40
### 3. the dump holds ciphertext, not codes
rows carrying a Fernet token (gAAAAA...): 2
occurrences of the seeded gate code 4821#: 0
occurrences of the seeded alarm code 9930: 0
### 4. the media bucket
download: s3://pink-glove-media/job_photos/2026/11/photo.png to media/job_photos/2026/11/photo.png
3bca58e912e0494706c7307462badf49d832b7121e915f1f4782084148e25d82  media/job_photos/2026/11/photo.png
```

Part A, the sequence `docs/DEPLOY.md` gives Jordan, verbatim:

```
### A1. a fresh database, and the dump restored into it
pg_restore exit: 0
### A2. the right key reads it
customers.ServiceLocation.gate_code: 2 readable, 0 unreadable
customers.ServiceLocation.alarm_code: 2 readable, 0 unreadable
customers.ServiceLocation.key_location: 0 readable, 0 unreadable
OK: all 4 encrypted values are readable with this FIELD_ENCRYPTION_KEY.
exit: 0
### A3. the schema is the one this image expects   (migrate --check)
exit: 0
### A4. a different key does not
customers.ServiceLocation.gate_code: 0 readable, 2 unreadable
customers.ServiceLocation.alarm_code: 0 readable, 2 unreadable
customers.ServiceLocation.key_location: 0 readable, 0 unreadable
CommandError: 4 of 4 encrypted values cannot be read with this FIELD_ENCRYPTION_KEY. It is not the key this database was written with.
exit: 1
```

Part B goes further than the drill asks: a second, empty Postgres server under a different owner, a second bucket, and the whole app booted against both.
Condensed in two places: the `ps` table is cut to the services that changed, and the sign-in is one line.

```
### B1. a second, empty Postgres server owned by a different role
restore_owner|0
### B2. restore into it
pg_restore exit: 0
locations|6
invoices|8
payments|4
job photos|1
migrations applied|71
tables owned by restore_owner|40
### B3. a second bucket, and the media copied back
make_bucket: pink-glove-media-restored
upload: media/job_photos/2026/11/photo.png to s3://pink-glove-media-restored/job_photos/2026/11/photo.png
### B4. boot the whole app against the restored copy
beat           Up 10 seconds
db-restored    Up 31 seconds (healthy)
migrate        Exited (0) 11 seconds ago
web            Up 10 seconds (healthy)
worker         Up 10 seconds
--- migrate (the release command) said:
migrate-1  |   No migrations to apply.
--- the running web process is on:
postgres://<credentials>@db-restored:5432/restored
pink-glove-media-restored
--- sign in over TLS: login 202, code from the web log, verify 200, session 200 as owner
### B5. an encrypted field, read through the app's own audited reveal
reveal 200
{'gate_code': '4821#', 'alarm_code': '9930', 'key_location': ''}
matches the seed (4821# / 9930): True
### B6. the media object, from the restored bucket
object: http://s3.localhost:9000/pink-glove-media-restored/job_photos/2026/11/photo.png
GET with signature    200
sha256 uploaded : 3bca58e912e0494706c7307462badf49d832b7121e915f1f4782084148e25d82
sha256 restored : 3bca58e912e0494706c7307462badf49d832b7121e915f1f4782084148e25d82
GET without signature 403
### B7. the same reads with a different FIELD_ENCRYPTION_KEY
CommandError: 4 of 4 encrypted values cannot be read with this FIELD_ENCRYPTION_KEY. It is not the key this database was written with.
exit: 1
ValueError: Could not decrypt customers.ServiceLocation.gate_code. FIELD_ENCRYPTION_KEY has probably changed since this row was written.
```

The codes shown are the seed's, which are in `seed_demo.py`; the drill on real data ends on `check_field_encryption`, which shows none.
The stack was brought down with `down -v` afterwards.

### Built, not verified here

Each needs a live service, so each is Jordan's, with the command in `docs/DEPLOY.md`:

- Staging's secrets satisfy the new refusals. `DEFAULT_FROM_EMAIL` must be set to a non-example address, and either `EMAIL_BACKEND` is the console backend or `EMAIL_HOST_PASSWORD` is set. If not, the next deploy's release command fails and the old release keeps serving.
- `FLY_IMAGE_REF` is present on a Fly machine, so the release tag is the image and not `unknown`.
- Sentry receives an event from Fly ("Error tracking", step 3), and its alert rule emails an address that is read.
- SendGrid's domain authentication screens, record shapes and error texts ("Email", steps 1 to 5).
- `fly proxy`, `fly ssh console -C 'printenv DATABASE_URL'`, `fly volumes snapshots list`, the Tigris read-only key and endpoint, and the whole of "Restoring production". The `pg_dump`, `pg_restore`, `aws s3 sync` and drill lines were run locally; the `fly` lines around them were not.
- `pg_dump` 17 on the development Mac: the one on PATH is 14 and would refuse a Postgres 17 server.

### Phase gate

**1. Test coverage.** Backend 1049 → 1203: 107 in `app/tests/test_sentry.py`, 20 more in `app/tests/test_settings_production.py` (email refusals, error tracking wiring, the audit script), 18 in `billing/tests/test_admin.py`, 9 in `base/tests/test_check_field_encryption.py`.
The admin action has a test on each side of each line: FAILED and each other status, owned and unowned, superuser, view-only staff, a session without the second factor, anonymous, a broker that refuses, and two runs before a worker gets to one.
No new tenant-scoped endpoint, so no new cross-tenant endpoint test; the action is tested to hand each row its own organization.
Gaps: the SDK's real HTTP transport is exercised in the rehearsal and not in pytest; nothing in pytest or CI starts a real worker, so the `celery[redis]` regression is guarded by the image build's import step and by the rehearsal, not by a test; the redaction of `Fly-Client-IP` is tested on a planted header and never seen on a real Fly request; backups are run by hand and nothing checks that one was taken.

**2. DRY.** The `enqueue` and `no_fee` fixtures moved to `billing/tests/conftest.py` when the admin tests needed them too. The settings probe takes its production environment from the settings tests' baseline. Left: the placeholders production settings need to import appear in `bin/deploy-audit.sh`, the Dockerfile and the rehearsal compose file, three formats that cannot import one another; listed under 4.

**3. Modularity.** The replay rule is `billing.webhook.replay`, beside `sweep` and the redelivery path; the admin only reports. `check_field_encryption` lives in `base` with the field it checks and finds its columns by field type, so it names no other app. `app/sentry.py` is the only importer of `sentry_sdk`. Nothing in `ui/` changed.

**4. Orthogonality.** New couplings, each noted where it lives:
- a new required production variable is now added in three places, not one: `app/bin/deploy-audit.sh`, the `collectstatic` layer of the root `Dockerfile`, and `deploy/docker-compose.rehearsal.yml` (and the baseline in `app/tests/test_settings_production.py`). A test runs the audit script, and the image build and the rehearsal fail on the other two;
- `app/sentry.py` redacts by name. A new secret under a new name (a new encrypted column, a new credential header) is added to its word list, and `app/tests/test_sentry.py` names the ones that exist;
- the same module treats a URL path segment longer than a UUID as a credential, which is what the pay link's and the magic link's tokens are. A new long, non-secret path segment would be redacted in error reports; a new short bearer token would not be;
- `celery[redis]` in `pyproject.toml` and the `import kombu.transport.redis` build step in the `Dockerfile` belong together: change the broker and change both.
`CLAUDE.md` was not edited; the exact lines are in the stream's report.

**5. Security.** No new model, endpoint or Celery task, so invariants 1 to 3 are untouched; the action passes `organization_id` from the row. No new `AllowAny` view in the application: one exists in `app/tests/sentry_probe_app.py`, a URLconf used only by a child test process and included by nothing. The admin still admits only a session that cleared the second factor (tested for the action, in pytest and over TLS); the action adds a superuser requirement and the ledger stays read-only. Access codes stay write-only and encrypted: `check_field_encryption` decrypts in memory, prints counts, records no reveal because no person sees a value, and is tested never to print a code or ciphertext. Nothing sensitive is logged by the new code; the admin logs an event id and a user id. No secret in source: the DSNs in tests are placeholders on `.invalid`, and gitleaks passed on every commit. The deploy audit comes back clean, and the rehearsal signed in over TLS and wrote a private object. What may leave to Sentry is ADR-029. Gap: `/security-review` was not run on the diff; the review above was done by hand.

**6. Branch and production guard.** Everything is on `feat/ops-readiness`; nothing was committed to `main`, pushed or merged. No `fly` command was run, no live service or credential was touched, and nothing was sent to a third party: every DSN was `.invalid` or the container's own loopback, mail went to the console backend, and the AWS CLI was pointed at MinIO on loopback with its own empty configuration. Docker: only the `pink-glove-rehearsal` project, brought down with its volumes; the `pink-glove` project was not touched.

### After Phase 4c (rebased 2026-11-17)

`main` gained the platform subscription (Phase 4c, ADR-028) and the worker fix while this branch was open.
The branch was rebased onto it; the worker fix dropped out as already applied, and `pyproject.toml` and the `Dockerfile` carry one copy of it with the `sentry-sdk` line beside it.
Four files conflicted, all of them two appends at the same place, and each kept both sides: `app/.env.example` (the platform block, then error tracking), `billing/tests/conftest.py` (`platform_billing_on`, then the shared `enqueue` and `no_fee`), `docs/DECISIONS.md` (ADR-028, then ADR-029) and this file.
`docs/DEPLOY.md`, `settings/production.py` and the settings tests merged without one, and the runbook still reads top to bottom: steps 1 to 10, then Production, Email, Error tracking, Backup and restore, the rehearsal and the recovery table.

What 4c changed for this work, and what was done about each:

**The admin replay covers platform events as written.**
A platform event is ledgered with no account, but it is only ever RECEIVED, and so only ever FAILED, with the organization its Stripe customer resolved to.
The action hands that organization's id to the same task (invariant 3) and `process` picks the platform handlers from the row's empty account.
Tested with `checkout.session.completed`, the one type both handler tables share, so a replay sent down the Connect path would record a payment and fail: it re-reads the subscription and writes none.
A platform event nobody holds has no organization and is refused with a sentence on the page, whatever its status.

**The scrubber needed no change, and the tests now say why.**
`STRIPE_PLATFORM_WEBHOOK_SECRET` is redacted by name.
The billing portal's URL and a Checkout URL lose the session they name by the rule for a long path segment; neither is logged today.
Stripe's ids for a tenant (`cus_`, `sub_`, `acct_`, `price_`) are left readable on purpose: they are names, nothing can be done with one without the platform's key, and they are what an error about a subscription is looked up by.

**Both sets of production guards hold together.**
The email refusals, the error-tracking refusal and 4c's two (a price without the platform webhook secret, a price without the key) are tested in the one module and pass together.
4c's settings are all optional, so the Dockerfile, the rehearsal compose file and the settings tests' baseline needed nothing for them.
The audit script did: with the Stripe key emptied there, a developer's `.env` holding a platform price tripped 4c's "price without the key" refusal, the same leak through `python-decouple` as before, and the script and the settings probe now set the price empty too.
On a machine with such a `.env`, the commits of this branch before that one fail up to three tests for that reason; with no `.env`, as in CI and the gate, every commit was run on its own and passes.

**`check_field_encryption` and the restore drill hold with `organizations` migration 0006.**
In the rebased image the release command applied `0006_platform_subscription`, the check read all four seeded values, and part A of the drill was run again: dump (40 tables), restore into a fresh database, right key `OK` and exit 0, `migrate --check` exit 0, a different key `4 of 4 ... cannot be read` and exit 1.
"Restoring production" gained a step: the subscription fields mirror Stripe, so an older dump restores them stale and the daily task, or a run by hand, corrects them.

The rehearsal was run once more from an image built from the rebased worktree: `web`, `worker` and `beat` up, the health, asset, 404 and redirect checks as before, sign-in over TLS (202, code from the web log, 200), a private object written and read back by signature (200, same SHA-256) and refused without (403), and one invoice emailed through the worker.
Stack brought down with its volumes.

Backend 1234 → 1400: the 154 above, 4 more in `billing/tests/test_admin.py` and 8 more in `app/tests/test_sentry.py`.
Still open: `/security-review` was not run; part B of the drill and the mutation check were not repeated after the rebase.

---

## Visit reminders as built

Branch `feat/visit-reminders`, cut from `main` at e39bae2.
Backend 2351 → 2465 (+114), frontend 427 (unchanged).
Decision record: ADR-035, revised before merge with Jordan's answers on the hour, the contact preference, leads, late bookings and moves.
The first piece of Phase 5: a customer is emailed ahead of a scheduled visit, if the organization has turned reminders on.

**The switch.**
`Organization.visit_reminders_enabled`, default false (`organizations/0007`), on `OrganizationSerializer`.
It rides `PATCH /api/organizations/current/`, which was already admin-tier for writes, member-readable, and refused in a read-only organization.
No new view and no new action; no settings page and no UI control (the organization settings page is a later branch).
Until then it is turned on through that endpoint or the Django admin's new "Notifications" fieldset.

**When.**
A visit is due once it is 17:00 local on the day before it, until it starts (`notifications.services.reminder_window`).
Two things ask: beat runs `notifications.send_visit_reminders_all` hourly at :15, which queues `notifications.send_visit_reminders(organization_id)` per organization that sends; and a `post_save` receiver on `Job` (`notifications/signals.py`, connected in `NotificationsConfig.ready()`) queues `notifications.send_visit_reminder(organization_id, job_id)` on commit when a single visit is booked or moved into its window.
Visits a plan bulk-creates send no signal and are left to the hourly run.

**The ledger.**
`notifications.VisitReminder` (visit, per-visit `sequence`, start reminded about, recipient, `sent_at`), unique on (visit, sequence) among live rows (`notifications/0002`, which numbers any rows from 0001).
A visit is due whenever its current start differs from the start in its latest row; moved away and back, it is reminded again.
The row is inserted before the email is sent and is the lock: a second tick, a second worker, the save hook or a re-delivered task inserts nothing and sends nothing.
A send that raises hard-deletes its row so the next run retries; a worker that dies mid-send leaves the row unsent and it is not retried (at most once).
Admin: read-only, no delete.

**What qualifies.**
`notifications.services.due_reminders`: organization active with the switch on; visit `scheduled` and not started; visit, customer and location not soft-deleted; customer a lead or active; customer has an email; current start differs from the latest reminder's.
Contact preference is not consulted.
Promoting a lead to active on booking is approved for a later branch and not built here.

**No text-message preference.**
`CustomerSerializer` refuses `preferred_contact_method = sms` with "Text messages aren't available yet." on create, update and the customer import, and the import template's hint ("One of: email, phone.") leaves it out.
The enum keeps the value, with no data migration; "Phone call" stays.
The UI's customer form never offered the preference, so nothing changed there, and the schema did not change.

**The email.**
`notifications/visit_reminder_email.html`, through `EmailMessage` and `DEFAULT_FROM_EMAIL` like the invoice email, with `Reply-To` set to the organization's address when it has one.
Day and times in the organization's zone, the service, the address, and the organization's phone and email; no access notes or codes.
Locally it prints to the worker's console; under test it goes to locmem.

**Seed.**
`seed_demo` turns reminders on for Sparkle Clean and leaves Rival Cleaners off; every seeded customer address is on `.test`.

**Not verified.**
The email has been asserted in tests and never opened in a mail client.
The beat entry and the save hook have not been watched in the compose stack; every task has run only eagerly in tests, never through a broker.
No UI was built, so there is nothing to screenshot.

### Phase gate - as built

1. **Test coverage.** Backend +114: `notifications/tests/` (84: the selection with one test per skip case and the until-it-starts window, the DST and far-from-UTC days, idempotency including a lost race, a failed send and the latest-start rule, the email's content, the fan-out, per-organization and one-visit tasks, the save hook including rollback, bulk-create and the API, cross-tenant, the beat entry, the 0002 numbering run for real, and the one-way import rule), `organizations/tests/test_visit_reminder_setting.py` (17: default, every role on each side of the line, cross-tenant, read-only organization), `customers/tests/test_contact_method.py` (11: create, PATCH, PUT, existing rows, import), and 2 in `test_seed_demo.py`. Mutation-checked: reckoning in UTC, the hour, dropping the not-started bound, each soft-delete join, the latest-start comparison in the selection and in the claim, a fixed sequence, not freeing a failed claim, the receiver not connected, queueing without `on_commit`, and the receiver's status check each turn a test red. The notifications tests were run twice. *Gaps:* two genuinely concurrent claims are argued from the unique constraint and played in sequence, not run in parallel transactions; the admin class has no test; the email is asserted by substring, not looked at; no vitest change, as no frontend changed.
2. **DRY.** One statement of the timing rule (`reminder_window`), of eligibility (`due_reminders`) and of which organizations send (`remindable_organizations`), shared by the hourly task, the one-visit task and the receiver. The day bounds come from `Organization.local_day_bounds`. The send block repeats the invoice email's four lines rather than sharing a helper, as the team-management gate already accepted for its two.
3. **Modularity.** All logic is in `notifications/services.py`; the tasks and the receiver only look things up and call it. `notifications` reads `scheduling`, `customers` and `organizations` through their models and enums; it reaches into no other app's views or serializers. The SMS refusal is in `CustomerSerializer`, which every customer write path already shares.
4. **Orthogonality.** New couplings: `due_reminders` restates "a customer worth reminding" as `CustomerStatus.LEAD`/`ACTIVE` and "a visit waiting to happen" as `JobStatus.SCHEDULED`, so a new status in either enum needs a decision here; `notifications` listens to `Job` saves through a `post_save` receiver connected in its `ready()`, invisible from scheduling, and `signals.LOOKAHEAD` (48 hours) must stay longer than the longest window (32 hours); the beat entry in `settings/base.py` names `notifications.send_visit_reminders_all`; the reminder email reads the organization's contact fields directly; `CustomerSerializer.unavailable_choices` refuses `ContactMethod.SMS` until SMS ships, and the import template's hint reads the same set so it does not offer it. Nothing imports `notifications`, and a test enforces it.
5. **Security.** `VisitReminder` is a `TenantModel`; no view serves it, so no `TenantViewSetMixin` is needed. The switch is written only through `CurrentOrganizationView` (admin tier, `IsOrgMember` read-only rule; no `allowed_while_inactive`). No new `AllowAny`. All three tasks take `organization_id`, and the one-visit task finds a visit only through that organization. The receiver queues on commit, so a rolled-back booking emails nobody. The email carries no access notes or codes, and the failure log names the job and not the address. No new secret and no new setting. Owned by the no-mistakes gate for test execution and `/security-review`.
6. **Branch and production guard.** All work is on `feat/visit-reminders`; nothing was committed to `main`, pushed or merged; no `fly` command and no staging or production resource was touched. Migrations were generated under test settings against the scratch database `pg_wave2_reminders`; `migrate` was never run against the dev database.

---

## Link tokens in the fragment as built

Branch `feat/link-tokens-in-fragment`, cut from `main` at e39bae2.
ADR-036 is the decision; this is what was built and checked.

**Backend.**
`base/links.py` (`token_link`) builds the three emailed links in the fragment form: `pay_url` in `billing/connect.py`, the invitation in `users/services.py`, the reset in `two_factor/services.py`.
`POST /api/billing/pay/invoice/` and `POST /api/billing/pay/checkout/` take the pay token in the body (`PayTokenSerializer`), throttled on the existing `pay_page` and `pay_checkout` scopes, so no settings file changed.
The path-token views stay, `deprecated` in the schema, and are declared after the new routes in `billing/urls.py`.
Stripe's `success_url` and `cancel_url` (`stripe_return_url`) are `/pay?paid=1#token=...` and `/pay?cancelled=1#token=...`, built by `token_link` with its optional query.
The first cut left them in the path form because Stripe's documentation is silent on fragments; Jordan's test-mode Checkout on 2026-11-18 showed both returned unchanged (ADR-036, 3), and a follow-up commit switched them.
The three email anchors carry `clicktracking=off`.
`app/sentry.py` redacts a `#name=value` pair as it does `?name=value`.
No migration.

**Frontend.**
`src/lib/linkToken.ts` reads the token (`tokenFromHash`) and builds the redirect for each old route (`fragmentRedirect`).
The routes are `/pay`, `/accept-invite` and `/reset-password`; `/pay/:token`, `/accept-invite/:token` and `/reset-password/:token` are redirect records beside them.
An address with no token shows "That link is incomplete" and makes no request.
`App.vue` keys the routed page by the link token, so a second link over the first starts the page over.
The pay page's checkout refusal no longer disappears when the page re-reads the invoice (a pre-existing bug, its own commit).

**Driven in a browser.**
The built SPA under gunicorn with the worktree's code, against a scratch database (`pg_wave2_fragment`, migrated and seeded; the dev database was not touched), in headless Chromium at 1280px and 390px, twice with identical results.
For each of the three flows, the new form and the old form both end at `pathname` `/pay` (or `/accept-invite`, `/reset-password`), empty `search`, `#token=...`, with `history.length` 2 in a fresh tab, so the old address was replaced rather than pushed.
`/pay/<token>?paid=1` ends at `/pay?paid=1#token=...` and polls.
A missing fragment shows the incomplete-link state, a garbled one the server's invalid-link state, and swapping the hash on an open page reloads the right invoice.
The gunicorn access log holds no token from any new-form load.
Each old-form load wrote its token once in the request line and 12 to 18 times in the `Referer` of the requests before the rewrite (240 lines for the pay token over 16 loads); none of those lines came from a new-form load or a hash swap.
Screenshots: `/private/tmp/claude-501/-Users-jordanduffey-code-pink-glove/7fab66a2-6a0d-43b9-ba6b-1fafca562bbc/scratchpad/shot-*.png` (scratch, not kept).

The Stripe return was driven the same way after the switch: `/pay?paid=1#token=...` and `/pay?cancelled=1#token=...` stay exactly as they arrived (no rewrite, `history.length` 2), show "Confirming your payment..." and "No payment was made.", and the gunicorn log holds `GET /pay?paid=1` and `GET /pay?cancelled=1` with no token in any line; run twice.

**Built, not verified here.**
That Stripe keeps a fragment was verified by Jordan on the platform sandbox, not on a connected account (ADR-036, 3); nothing here calls Stripe.
That SendGrid honours `clicktracking=off` and that a real inbox delivers the fragment intact: needs one real send per email on staging.
The compose stack's own `ui` service was not used, so the Vite dev server path (separate origin) was exercised only by vitest, not in a browser.

### Phase gate

**1. Test coverage.**
Backend 2351 → 2369 (+18), frontend 427 → 459 (+32).
The body endpoints have the path form's tests and more: bad, missing, expired, foreign-salt and deleted-invoice tokens, a signed-in owner of another organization naming theirs in the header, the token absent from the request URL, and one throttle bucket shared across both forms for each scope.
The emails, `dev_link`, `pay_url` and Stripe's `success_url` and `cancel_url` are asserted in the fragment form, the last two to the exact string Stripe is given, with no token before the `#`.
vitest covers the Stripe return arriving in the fragment form (query read, nothing rewritten) as well as the old form.
vitest covers `tokenFromHash`, the real route table on jsdom's history (old form replaced in place, token gone from `pathname` and `search`, `?paid=1` kept), and the three pages reading the fragment, sending a body, and refusing an empty one.
*Gaps:* the `App.vue` key has no spec (App is not mounted in vitest); it is covered by the browser run only.
The `clicktracking=off` attribute is asserted by nothing; it is in the templates and the ADR.

**2. DRY.**
One builder (`token_link`) and one reader (`tokenFromHash`), with one shared redirect for the three old routes and one sentence (`INCOMPLETE_LINK`) for an empty address.
The two pay views share `_invoice_from` and `_token_in_body`.

**3. Modularity.**
Pages reach the API only through `src/api/endpoints.ts`.
`base/links.py` depends on settings alone; `billing`, `users` and `two_factor` import it, and it imports none of them.

**4. Orthogonality.**
Couplings this changes or adds, each noted in code:
the pay page's path is now `/pay` with the token in the fragment, for the email and for Stripe's returns alike, so CLAUDE.md's `/pay/<token>` coupling now reads `/pay` (with `?paid=1`/`?cancelled=1` and `#token=`) plus the `/pay/:token` redirect in `ui/src/router/index.ts`, and `pay_url` and `stripe_return_url` in `billing/connect.py`, both through `app/base/links.py`;
the invitation and reset paths move the same way (`/accept-invite`, `/reset-password`, with their `/:token` redirects);
the fragment key `token` is spelled in `app/base/links.py` and `ui/src/lib/linkToken.ts`;
the old `/pay/:token` route must outlive the longest Checkout session opened before the deploy (`CHECKOUT_SESSION_TTL`, 30 minutes), which the 120-day pay-link window already covers.

**5. Security (owned by the no-mistakes gate).**
`AllowAny`, deliberate and throttled: `PublicInvoiceView` and `PayCheckoutView`, beside the two deprecated views they replace.
No tenant data is newly exposed: the body endpoints serialize with the same `PublicInvoiceSerializer`.
`/security-review` was not run on this branch; the gate's review is the security pass.

**6. Branch and production guard.**
All of this is on `feat/link-tokens-in-fragment`.
Nothing was committed to `main`, nothing was pushed or merged, no `fly` command was run, and Stripe was not called.

---

## Referrer policy as built

Branch `chore/referrer-policy-strict-origin`, rebased onto `main` at `fe284af`.
Every response now sends `Referrer-Policy: strict-origin`, so every `Referer` the site causes carries the origin and never a path, query or fragment (ADR-038).
It closes the access log half of the token-in-path leak: on the first load of an old-form emailed link, the token was repeated in the `Referer` of every asset and API call the page made.

- `SECURE_REFERRER_POLICY = "strict-origin"` in `settings/base.py`, beside the CSRF cookie settings; the `same-origin` line in `settings/production.py` is gone, so development, the suite and every deployment send the same value.
- Nothing else set the header before: no `<meta name="referrer">` in `ui/index.html`, and neither gunicorn, whitenoise nor the catch-all view writes one. `SecurityMiddleware` already sat above whitenoise, so the files whitenoise serves carry it with no middleware change.
- CSRF: the Referer fallback compares scheme and host only (Django 5.2.17, `django/middleware/csrf.py` lines 297 to 340, reached from lines 436 to 462 when there is no `Origin`), so an origin-only `Referer` passes. Tested in pytest and over TLS.
- No schema change, no migration, no frontend change.

### Rehearsal, as run (2026-11-18)

Image built from the worktree root, `deploy/docker-compose.rehearsal.yml` up, `seed_demo --force`, then:

- `ps -a`: `web`, `worker`, `beat`, `proxy`, `db`, `redis`, `minio` all `Up`; `migrate` and `minio-bucket` exited 0.
- `/health/ready/`: both checks `ok`. `/schedule`: 200, `no-cache`, `referrer-policy: strict-origin`, `x-frame-options: DENY`. `/pay/<token>`: 200 and the same header.
- A hashed JS asset and a hashed CSS asset named from the image, and `/layers.css`: 200, `immutable` on the hashed ones, `referrer-policy: strict-origin` on all three (whitenoise).
- `/api/users/session/`: 200 and the header. `/api/no-such-thing/`: Django's 404, not the page. `http://localhost/`: 301 to https. `sentry_sdk` not imported.
- Headless Chromium (Playwright 1.63), first load of `https://localhost/pay/<planted token>`: the browser sent 30 requests and every `Referer` was `https://localhost/` or absent; of the 29 access log lines that followed the document, 0 carried the token in the `Referer`. The token was still in the request line of the document and of `GET /api/billing/pay/<token>/`, which is the fragment branch's to fix.
- The same load with the browser forced back to `same-origin` (a Playwright route rewriting the header on every response): 27 of 29 lines carried the token in the `Referer`, so the probe goes red without the change.
- Sign-in through the UI over TLS as `owner@sparkle-clean.test`, code read from the web log: lands on `/schedule`. The login and verify POSTs went out with `Origin: https://localhost` and `Referer: https://localhost/`.
- A private object: a PNG uploaded from the signed-in page through `POST /api/scheduling/job-photos/` (CSRF checked), 201; the returned URL is signed, 200 with the signature and 403 without.
- The Referer fallback in production shape, with curl and no `Origin`, signed in, `POST /api/users/logout/`: `Referer: https://evil.example/` 403, no `Referer` 403, `Referer: https://localhost/` 200. Run twice, same result.
- An issued invoice emailed: 202, and the worker logged the message and `send_invoice_email ... succeeded`.
- Brought down with `down -v`.

Not driven: a real cross-origin navigation to Stripe (no Stripe keys in the rehearsal), Firefox or Safari, and the dev server (Vite sends no `Referrer-Policy`; see ADR-038, Consequences).

### Phase gate - as built

1. **Test coverage.** Backend 2483 to 2492 (+9): `app/tests/test_referrer_policy.py` (6: the setting, an API response, and a signed-in POST over `secure=True` with an origin-only `Referer` and no `Origin`, with `Origin` too, a foreign `Referer`, and neither), `app/tests/test_spa.py` (2: the header on the index document, and on a file whitenoise serves with `SecurityMiddleware` above it), `app/tests/test_settings_production.py` (1: production keeps the value). Reverting the setting turns the five header tests red; the four CSRF tests stay green by design, since they prove compatibility, not the header. Frontend untouched, 459. Gaps: no browser test in CI asserts the `Referer` a page sends; that was checked in the rehearsal only.
2. **DRY.** One setting in one module replaces the production-only line; the tests read it through responses, not by restating it.
3. **Modularity.** Settings only; Django's `SecurityMiddleware` applies it. No view, serializer or app module changed.
4. **Orthogonality.** No new coupling between apps. Couplings to note: `SecurityMiddleware` must stay above whitenoise in `MIDDLEWARE` for the assets to carry the header (asserted in `app/tests/test_spa.py`, as the clickjacking header already was); the Vite dev server serves the development page without the header, so the dev page's own `Referer`s follow the browser default. `CLAUDE.md` was not edited.
5. **Security.** No model, endpoint, task or `AllowAny` added; the change only sends less. CSRF verified in pytest and over TLS as above. Deploy audit clean. The rehearsal signed in over TLS and wrote a private object. `/security-review` was not run as a skill; the diff was reviewed by hand, and the no-mistakes gate owns the review.
6. **Branch and production guard.** All work is on `chore/referrer-policy-strict-origin`. Nothing committed to `main`, pushed or merged; no `fly`, no live service, no `migrate` against the dev database. Docker: only the `pink-glove-rehearsal` project, brought down with its volumes; the `pink-glove` dev stack was not touched.

---

## Customer status on booking as built

Branch `feat/promote-lead-on-booking`, cut from `main` at `e39bae2`.
Booking work moves a Lead or an Inactive customer to Active, and booking work for an Archived customer is refused (ADR-037).

- Booking is five writes through the API: a new visit, a new active plan, a paused plan made active, and a visit or active plan handed to another customer. The old customer of a hand-over is never demoted.
- `scheduling.services.activate_booked_customer(customer)` (first written as `promote_lead`) is one conditional UPDATE of `status` and `updated_at`, scoped by pk, organization and `status IN ('lead', 'inactive')`.
- Archived: each of the five writes is a 400 on `customer` ("This customer is archived. Unarchive them before booking work."), checked by `refuses_booking` in the serializers' `validate`. An active plan for an archived customer is not saved at all except to pause it, because any other edit can regenerate its visits.
- Nightly: `materialize_plan` makes no new visits for an archived customer's plan and leaves the existing ones as they are; unarchiving lets the next run resume.
- Transactions: a new visit already was one; a visit edit, a plan create and a plan update now are. Behaviour change beyond the status: a plan create or a plan update whose visits cannot be priced was a 400 that kept the write; it is now rolled back.
- Schema: only the plan update's description changed (regenerated with the code). No migration. No frontend change: no response carries the status, the customers page refetches when opened, and no store caches customers.
- Not done here: `seed_demo` still leaves its booked customers as Leads (a later demo-seed branch); the visit dialog and plans page still list archived customers, so the 400 is the only guard there.

### Phase gate - as built

1. **Test coverage.** Backend 2351 to 2412 (+61), all in `scheduling/tests/test_booking_status.py`: every trigger against every status (Lead and Inactive move, Active is not written, Archived is a 400 with nothing written), every role that may book, every 400 path on both create and edit leaving the status unchanged, the archived customer's existing work (edit a visit, pause a plan, refuse other plan edits, a paused plan may be created), soft-deleted customers still refused, the nightly run and the organization task for an archived customer, delete and cancel do not demote, and four cross-tenant tests. Run twice. Mutation-checked once per rule: the create promotion (7 red), the plan create transaction (1), the edit triggers (3), the plan update transaction (1), Inactive moving (5), the visit archived refusal (2), the plan archived refusal (5), the nightly guard (2). Frontend unchanged at 427; lint, type-check and vitest re-run green after the schema regeneration. Gaps: admin and `seed_demo` paths untested (they do not move statuses by design); nothing was run in a browser.
2. **DRY.** One function moves the status and one decides the refusal, each called from every booking path; the refusal text is one constant. No client-side copy of either rule.
3. **Modularity.** Both rules live in `scheduling/services.py`; the serializers and the view call them.
4. **Orthogonality.** No new coupling beyond `scheduling.services` importing `customers.enums` and `customers.models`, the direction that already existed. Nothing in `customers` imports scheduling for this. The `ui/` pickers know nothing of the rule; the 400 reaches them as an ordinary field error.
5. **Security.** No new endpoint, model, task, `AllowAny` or `allowed_while_inactive`. The update filters on the customer's own organization and the booking's customer is tenant-validated before it. The change only refuses more and writes one column. `/security-review` was run on the first diff (no findings); the later commits were reviewed by hand with the same checks.
6. **Branch and production guard.** All work is on `feat/promote-lead-on-booking`; nothing committed to `main`, pushed or merged; no `fly`, no `migrate` against the dev database.

---

## Demo seed realism as built

Branch `fix/demo-seed-realism`, cut from `main` at `c2188f1`.
`seed_demo` now tells the truth on three screens: the reports, the customers list and the pay page.

- **The tenants are owed different amounts.** Rival's Standard Clean is $135 and 90 minutes against Sparkle's $150 and 120 (`SERVICE_OVERRIDES`). A one-off Move-Out Clean at Priya's rental (1650 sq ft, $297, the seed's one taxable line) goes on the overdue invoice with her oldest plan visit, so Sparkle's carries $24.50 of tax at 8.25% and Rival's carries none. Sparkle issues its no-access fee ($25 flat) and Rival drafts its fee (50% of $135, $67.50), so Sparkle has five invoices and Rival four (`INVOICES`). The paid, part-paid and draft invoices bill two of Dana's visits each.
- **Seeded on 2026-11-18:** receivables Sparkle $646.50 over 3 invoices, Rival $567.00 over 2; collections in the last 60 days Sparkle $450.00 plus $20 tip, Rival $405.00 plus $20 tip; timesheet Sparkle 12h15m, Rival 8h45m. Identical across two fresh seeds and a `--force` run the same day.
- **Booked customers are Active.** Each plan, and the one-off visit, calls `scheduling.services.activate_booked_customer`, the call the API's booking paths make (ADR-037), on every run, so a database seeded before ADR-037 is corrected by `--force`. Marcus has nothing booked and stays the one Lead per tenant.
- **Dates follow the work.** The old seed issued its overdue invoice 60 days before the run and the others 3 days before, whatever the visit dates, so SPK-0003 was due a month before its visit. An invoice is now issued the day after its last visit (today, for one finished today), its due date is the terms after that, and payments land between issue and today. The plans start seven weeks back instead of three, so the overdue invoice's visits are old enough for Rival's 30-day terms; eight would leave a gap on the board between the first materialization's horizon and today.
- **`--force` adds no invoices.** It used to draft and issue two more per tenant from whatever visits were still unbilled, every run. `_billing` now does nothing for an organization that has any invoice, soft-deleted included. A database seeded before this branch keeps its old invoices, dates and amounts (issued invoices are never edited); a fresh database shows the new ones.
- Not changed: the pay page's due date has no year (`formatDayLabel` in `ui/src/lib/datetime.ts`), left for the queued UI-fix branch. No schema, serializer, view or migration change.

### Phase gate - as built

1. **Test coverage.** Backend 2553 to 2561 (+8), all in `organizations/tests/test_seed_demo.py`: booked customers Active with one unbooked Lead per tenant, `--force` adds no customers, invoices or payments, `--force` moves an older seed's booked customers out of Lead, per-tenant invoice counts (5 and 4), every issued invoice dated on or after its last visit with a later due date and payments between issue and today, one overdue invoice per tenant, tax on Sparkle only, each no-access fee by its own rule, multi-line invoices, and receivables, invoice count, collections and timesheet totals all differing. Seed tests run twice. Against the old seed nine tests go red, the date test on exactly the reported bug (issued 2026-09-19 for a visit on 2026-11-03). A scratch run, not committed, seeded at 28 instants (every weekday at four UTC hours) and held the date, overdue, count and status assertions at each. Frontend untouched, 459. Gaps: the seed is not run on every weekday in CI (no clock-freezing library in the dev dependencies); nothing was checked in a browser.
2. **DRY.** Invoices are built by `billing.services` (`billable_jobs`, `draft_invoice`, `issue_invoice`, `record_payment`), the one-off visit is priced by `scheduling.services.quote_job_price` and customers are activated by `activate_booked_customer`; the seed restates none of their rules.
3. **Modularity.** All changes are in the management command and its tests; it reaches billing and scheduling through their services only.
4. **Orthogonality.** No new coupling. The seed already imported `billing.services` and `scheduling.services`; it now also imports `billing.models.Invoice` for the once-only check. `CLAUDE.md`'s seed description is now inaccurate in two places and was not edited (reported to Jordan).
5. **Security.** No endpoint, model, task, `AllowAny` or `allowed_while_inactive` added; seeded customer emails stay on `.test`. The seed still refuses to run with `DEBUG` off or over existing organizations without `--force`. `/security-review` was not run; the diff touches only the dev seed and the gate owns the review.
6. **Branch and production guard.** All work is on `fix/demo-seed-realism`; nothing committed to `main`, pushed or merged; no `fly`; `seed_demo` and `migrate` ran only against the scratch database `pg_wave2_seed`, never `pink_glove`.

---

## Customer sign-in as built

Branch `fix/customer-sign-in`, cut from `main` at `89d5dbc`.
A customer can now ask for a sign-in link, open it, and land on a page of their own visits; every role lands on a screen it may open (ADR-039).

- **Reproduced first,** on the built SPA under gunicorn against the scratch database `pg_wave2_signin`, with mail printed to the console: the login page offered customers nothing, the mailed link was `/sign-in/<token>` and opened "Nothing here", and once signed in by posting the token by hand, `/` froze the tab (the page stopped answering scripts and screenshots).
- **The link** is `token_link("/sign-in", raw_token)`, so `/sign-in#token=...`. The consume endpoint already took the token in the body and is unchanged; there was no path-token endpoint to deprecate, and the schema did not change.
- **The pages.** `/sign-in-link` asks for a link (the server's same-for-everyone sentence, and `dev_link` locally). `/sign-in` reads the fragment with `tokenFromHash` and spends the token on a button press, not on load, so a mail scanner cannot burn it; a success re-reads the session and replaces the page with `/`. A missing token is "That link is incomplete" with no request; a garbled, spent or expired one is the server's sentence; both offer a new link. `/sign-in/:token` redirects to the fragment form. The login page links to `/sign-in-link`.
- **The landing.** `/` redirects to `landingFor(session)` in `ui/src/router/index.ts`: `schedule` for dispatcher and above, `my-day` for a cleaner, `customer-home` (`/my-visits`) for a customer, `no-access` for a session with no role. The guard reads every tier through `admits`, which gains a `customer` tier. The loop's cause was the old root redirect sending every non-cleaner to `schedule`.
- **The customer home** greets the customer and lists their visits for the next eight weeks from `GET /api/scheduling/jobs/` (their own only, in the customer's serializer shape), and says that invoices come by email with a pay link. It reads nothing a customer is refused. The shell's drawer gives a customer one item, "Your visits".
- Not done here: serving customers their invoices (a new permission, listed for Jordan); `seed_demo` linking its customer logins to a `Customer` row (the demo customer signs in to an empty home; the browser run linked one by hand in the scratch database only).

### Phase gate - as built

1. **Test coverage.** Backend 2561 to 2573 (+12), in `users/tests/test_auth.py`: the dev link and the emailed body carry `/sign-in#token=` with no path or query token, the emailed token signs the customer in, staff get the 202 and no email per role, the token in a query string is refused and not spent, one customer's link signs in only that customer, and a link's session reads only its own visits, gets a 403 with another tenant's header, and is refused invoices. Expiry, single use, a promotion and a deactivation were already pinned and pass unchanged. `two_factor/tests/test_lockout_guard.py` parsed the old path form and now reads the fragment. Frontend 459 to 493 (+34): `src/router/landing.spec.ts` (each role on `/`, a superuser with no membership, a customer refused four staff routes, three staff roles refused `/my-visits`, and every landing admitted), `src/router/linkRoutes.spec.ts` (`/sign-in` in both forms, the old one rewritten in place), `src/pages/SignInPages.spec.ts` and `src/pages/CustomerHomePage.spec.ts`. Mutation-checked: the old link line turns 14 tests in `users/tests/test_auth.py` red (the new ones plus the existing ones that now read the fragment); the old root redirect turns the six customer and superuser landing tests red (the spec counts guard runs so a loop fails instead of hanging vitest). Both suites run twice. Browser: the full flow at 1280px and 390px, run twice. Gaps: no test drives `App.vue`'s customer drawer item; the 401 path of `signInWithLink` while a staff session is already open is not covered (see 5).
2. **DRY.** The tier check exists once (`admits`) and the landing once (`landingFor`), replacing three guard branches and the ternary in the root redirect. The token is read with `tokenFromHash` and the link built with `token_link`; the request page mirrors `ForgotPasswordPage` and reuses `failureMessage` and `fieldErrors`. Dates go through `src/lib/datetime.ts`.
3. **Modularity.** The pages reach the API through `requestSignInLink` in `src/api/endpoints.ts` and `signInWithLink` in the session store, never `fetch`. No backend logic moved.
4. **Orthogonality.** The role-tier mirror listed in `CLAUDE.md` changed: the route tiers in `src/router/index.ts` gain `customer`, and the tier check and role landing now live in `admits` and `landingFor` there, so a tier change in `users/enums.py` also means those two functions; the `CLAUDE.md` bullet now names them. The fragment key is still spelled only in `app/base/links.py` and `ui/src/lib/linkToken.ts`.
5. **Security.** No new endpoint, model, task, permission, `AllowAny` or `allowed_while_inactive`; no data newly served to customers, and the customer home asks for nothing a customer is refused. The token travels only in the fragment and a POST body; the browser run saw nothing of it in a request line. The landing and tiers are navigation only; the API refuses the same things on its own. Known edge: a 401 from the consume endpoint clears the store's session if a staff member was already signed in on that browser, though the server session is untouched; reloading restores it. `/security-review` was not run; the gate owns the review.
6. **Branch and production guard.** All work is on `fix/customer-sign-in`; nothing committed to `main`, pushed or merged; no `fly`. `migrate` and `seed_demo` ran only against `pg_wave2_signin`, never `pink_glove`; the scratch server ran on :8123 from scratch settings outside the repository.
