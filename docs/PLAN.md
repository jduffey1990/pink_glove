# Build plan

Rationale for the decisions referenced here lives in `docs/DECISIONS.md`.
Invariants that hold across all phases are in `CLAUDE.md`.

---

## Start here

Everything through Phase 2.5 is on `main`, tests green. Next work is Phase 3
(scheduling), specified below.

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

1. **Frontend** — nothing built. Vue 3 + Vuetify shell exists in the source
   repo at `/Users/jordanduffey/Desktop/pomarium copy/ui` with Stripe.js
   already wired. Reusing it is the working assumption, not a decision.
2. **Deploy target** — deliberately deferred (ADR-006). Nothing in the code
   assumes one.
3. **Unassigned access reveals** — see Phase 3 below. Needs a product answer
   before the end-of-day flagging pass can be written.
4. **Stripe Connect** — one Stripe account is right for one tenant; real
   paying tenants change the model (ADR-007). Decide before building Phase 4
   hard against the single-account assumption.

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, settings, Docker, test harness | **Done** |
| 1 | Tenancy core + identity | **Done** |
| 2 | Customers + service catalog | **Done** |
| 2.5 | Access audit trail | **Done** (flagging pass deferred to 3) |
| 3 | Scheduling | Next |
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

New `scheduling` app. Everything below is a `TenantModel` and every viewset
inherits `TenantViewSetMixin`, or `base/tests/test_tenancy.py` fails.

### Models

```
RecurringPlan       customer, location, service, rrule (RFC 5545 string),
                    starts_on, ends_on, preferred_start_time,
                    price_override_cents, is_active
Job                 customer, location, service, plan (nullable),
                    scheduled_start, scheduled_end, status,
                    price_cents (snapshot), notes
JobAssignment       job, user, assigned_at, accepted_at
TimeEntry           job, user, clock_in, clock_out
JobNote / JobPhoto  job, user, body / image
```

`JobStatus`: `SCHEDULED`, `EN_ROUTE`, `IN_PROGRESS`, `COMPLETE`, `CANCELLED`,
`NO_ACCESS`. That last one is not padding — "we couldn't get in" is a distinct
outcome from "cancelled", it happens regularly, and it needs its own billing
treatment.

**`price_cents` is snapshotted onto the Job**, not read live from `Service`.
Raising your prices must not retroactively change what last month's completed
jobs were worth. Compute it with `Service.quote_cents()` at materialization.

### Recurrence

Beat task materializes real `Job` rows ~8 weeks ahead from each active
`RecurringPlan` (ADR-012 — do not compute occurrences on the fly).

**The DST trap, which is the whole reason `Organization.timezone` exists:**
expand the RRULE in the organization's local timezone, then convert to UTC for
storage. A plan of "every Tuesday 9am" must stay 9am local across the March and
November transitions. Expanding in UTC silently shifts every job by an hour for
half the year. Write the test for a DST boundary first.

Materialization must be idempotent — the task will run daily and must not
duplicate jobs it already created. Key on `(plan, scheduled_start)`.

### Finish the access audit trail

`AccessReveal` already carries `evaluated_at`, `is_flagged`, `flag_reason`, and
the review trio, all unset. Phase 3 completes ADR-016:

1. Add `job = FK("scheduling.Job", null=True, blank=True)` and set it on reveal.
2. Beat task, once per day after close: flag reveals falling more than
   ~1h before / ~2h after their job's window, evaluated **in the org's
   timezone**. Set `evaluated_at` on every row processed, so "not yet
   evaluated" and "evaluated and fine" stay distinguishable.
3. Make the buffer per-organization rather than hardcoded.

**Open question, needs a product answer before writing the evaluator:** what
should happen to a reveal with **no job attached** — a dispatcher pulling a code
from the office, or a cleaner at an address they aren't assigned to? Flag every
one and legitimate office work generates constant noise; flag none and the
obvious gap stays open. Suggested answer: flag unassigned reveals only outside
business hours, which means adding open/close times and working days to
`Organization`. Not decided.

### Permissions sketch

- Dispatcher and above: create, reschedule, assign, cancel
- Cleaner: read jobs they are assigned to, clock in/out, add notes and photos,
  set status
- Customer: read their own jobs only

---

## Later phases

**Phase 4 — billing.** `Invoice`, `InvoiceLine`, `Payment`, `PaymentMethod`.
Ports the source repo's Stripe webhook pipeline nearly verbatim (ADR-007).
Organization subscription models come over renamed but unwired.

**Phase 5 — notifications.** Email/SMS reminders, invoice delivery, magic-link
delivery.

Open decisions are listed under **Start here** at the top of this file.
