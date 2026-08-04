# Build plan

Rationale for the decisions referenced here lives in `docs/DECISIONS.md`.
Invariants that hold across all phases are in `CLAUDE.md`.

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold, settings, Docker, test harness | **Done** |
| 1 | Tenancy core + identity | **Done** |
| 2 | Customers + service catalog | Next |
| 3 | Scheduling | Not started |
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

## Later phases

**Phase 2 — customers / catalog.** `Customer`, `ServiceLocation` (sqft,
beds/baths, access notes, gate code, pets), `Service` + pricing model (flat /
hourly / per-sqft).

**Phase 3 — scheduling.** `RecurringPlan` (RRULE), `Job`, `JobAssignment`,
`TimeEntry`, `JobNote` / `JobPhoto`. Beat materializes `Job` rows ~8 weeks ahead
(ADR-012).

**Phase 4 — billing.** `Invoice`, `InvoiceLine`, `Payment`, `PaymentMethod`.
Ports the source repo's Stripe webhook pipeline nearly verbatim (ADR-007).
Organization subscription models come over renamed but unwired.

**Phase 5 — notifications.** Email/SMS reminders, invoice delivery, magic-link
delivery.

---

## Open

- **Frontend.** The source repo has a Vue 3 + Vuetify SPA at
  `/Users/jordanduffey/Desktop/pomarium copy/ui` with Stripe.js already wired.
  Reusing that shell as `pink_glove/ui/` is the default assumption; not decided.
- **Deploy target.** Deliberately deferred (ADR-006). Every host-specific
  concern is behind an env seam, so this can be decided at deploy time.
