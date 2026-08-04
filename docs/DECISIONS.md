# Architecture decisions

Each entry records what was decided, what was rejected, and why. **If you are
about to change something described here, read the "Rejected" section first** --
most of these look like obvious improvements until you know what they cost.

Source repo referenced throughout: `/Users/jordanduffey/Desktop/pomarium copy`
(a production Django app this project took its foundations from). Read-only.

---

## ADR-001: Shared-schema multi-tenancy, organization FK

**Decided.** One database, one schema. Every tenant-owned model carries an
`organization` FK via the `TenantModel` abstract base.

**Rejected — schema-per-tenant (`django-tenants`):** strongest isolation, but it
complicates migrations, Celery routing, testing, and deploys. Heavy machinery for
an app whose realistic ceiling is a handful of tenants.

**Rejected — Postgres row-level security:** genuinely stronger, and still on the
table as *defense in depth* later. Deferred because it requires plumbing a
session variable onto every connection (including Celery workers and migrations),
which is a lot of failure surface to add before the app does anything.

---

## ADR-002: Tenant scoping is enforced at a viewset chokepoint, NOT by a filtering default manager

**Decided.** `TenantViewSetMixin.get_queryset()` applies the organization filter;
`perform_create()` stamps the organization. A conformance test walks the URLconf
and fails CI if a view over a `TenantModel` doesn't inherit the mixin.

**Rejected — a `ContextVar` that a custom manager reads to auto-filter
`Model.objects`.** This is the design that looks cleanest and is the most likely
thing for a future contributor to "improve" toward. It breaks:

- **migrations** — they run with no request, so the context var is unset
- **the Django admin** — same, plus `_default_manager` is used internally
- **`dumpdata` / `loaddata` / management commands** — silently return nothing
- **related-object traversal** — `job.customer` raises `DoesNotExist`

and its failure mode is *empty results*, not an error, which is the worst kind of
bug to chase. The chokepoint approach keeps Django's internals behaving normally
and moves enforcement somewhere a test can verify statically.

**Consequence:** `organization` is never read from request data, only stamped
from the resolved tenant, so a forged `organization` field in a POST body is
inert.

---

## ADR-003: Role and organization live on `Membership`, not on `CustomUser`

**Decided.** `CustomUser` is an authenticating identity with no organization and
no role. `Membership(user, organization, role)` carries both.

**Rejected — `role` + `firm` FK directly on the user** (what the source repo
does). It is simpler and saves a join on every permission check. It was rejected
because multi-tenancy is a stated goal, and a single-org FK on the user table is
the single most painful thing in this schema to unwind later — it touches auth,
permissions, every queryset, and requires a data migration on live rows.

`users/tests/test_models.py::test_has_no_organization_or_role_field` guards this.

---

## ADR-004: Celery tasks take `organization_id` as an explicit argument

**Decided.** Tenant context is never inherited implicitly by background work.

**Rejected — reading the same request-scoped context the middleware sets.** In a
worker there is no request, so it resolves to `None` and the task either
processes nothing or, worse, processes everything. Explicit arguments are more
typing and cannot fail silently.

---

## ADR-005: Soft delete filters the default manager

**Decided.** `Base.objects` (a `SoftDeleteManager`) hides soft-deleted rows;
`Base.all_objects` sees everything. `Meta.base_manager_name = "all_objects"` so
related-object traversal still resolves soft-deleted targets, and
`Meta.default_manager_name = "objects"` because managers inherited from an
abstract base sort *ahead* of ones declared on the concrete model — without it,
`_default_manager` resolves to `all_objects`.

**Consequence, and the trap:** a concrete model that declares its own `Meta`
without inheriting `Base.Meta` silently loses both settings. Write
`class Meta(Base.Meta):`. `base/tests/test_models.py::TestBaseMetaConformance`
fails CI if you forget.

The source repo has a `deleted_at` column that nothing reads. Either implement
soft delete or drop the field; a column that lies is worse than no column.

---

## ADR-006: Deploy target deliberately deferred

**Decided.** Not chosen yet. Everything host-specific is behind an env seam:

- `DATABASE_URL` (one var, via `dj-database-url`) — works on any managed Postgres
- `MEDIA_BACKEND` = `filesystem` | `s3` | `gcs`, with optional install extras
- Whitenoise serves static from the container, so **no bucket is required at all**
- `SECURE_SSL_REDIRECT` off by default — most platforms redirect at the edge and
  doubling it causes loops. Turn on for a bare VPS.

Fly.io / Render / Railway / a VPS / Cloud Run all work without a code change.

**Not carried over from the source repo:** `terraform/`, four
`cloud_deploy_*.yaml` environment files, `.gitlab-ci.yml`, `schema.yml`. The
brief was explicitly "docker container → build → deploy."

---

## ADR-007: Billing runs in two directions; customer invoicing is built first

**Decided.** The cleaning company invoices *its* customers (Phase 4). The source
repo's subscription models — tenants paying for the software — come over renamed
but unwired, so turning them on later is wiring, not a rebuild.

**Flagged fork:** one Stripe account is correct while there is one tenant. Real
paying tenants means **Stripe Connect**, which changes customer ownership and
webhook routing. Know this before building hard against the single-account
assumption.

**Kept from the source repo nearly verbatim** (`app/billing/tasks.py` there):
signature verify → enqueue Celery task with the raw dict → `StripeEvent`
idempotency ledger → dispatch → persist every payload. It is the best code in
that repo.

**Dropped:** `FirmTransaction.revenue_usd`, which hardcodes `.971` as the Stripe
fee. Read actual fees from balance transactions.

---

## ADR-008: Authentication differs per audience

Three groups sign in, and one flow does not fit all three.

| Audience | Flow |
|---|---|
| Owner / admin / dispatcher | Password + 2FA on **every** login |
| Cleaner | Password + 2FA on an **untrusted device only** (30-day trusted-device token) |
| Customer | **Magic link**, no password |

**Why:** 2FA on every login is right for office staff handling financial data,
too much friction for a cleaner opening the app in a driveway, and wrong for a
homeowner who checks an invoice twice a year and will call for a password reset
every time.

Hardening applied to the ported 2FA (source: `app/two_factor/views.py`), which
stored codes in plaintext with no attempt cap, no rate limit, and leaked
`dev_code` in every non-production environment: hashed codes, `attempts` cap,
`consumed_at` single-use, DRF throttles, `dev_code` gated on `LOCAL` only.

`TwoFactorCode` extends `Base`, **not** `TenantModel` — 2FA happens before tenant
resolution, so there is no organization to scope to yet.

---

## ADR-009: Money is integer cents, everywhere

No floats, no `Decimal` columns for currency. The source repo already does this
(`amount_in_cents`) and it is one of the things it gets right.

---

## ADR-010: UUID primary keys

Job and invoice IDs appear in URLs customers can see. Sequential integers leak
business volume to anyone who looks.

---

## ADR-011: `CustomUser` shipped in Phase 0, before the first migration

**Decided.** The plan originally put `users` in Phase 1. Moved earlier because
`AUTH_USER_MODEL` must be correct before the *first* `migrate` — swapping the
user model after initial migrations exist is one of Django's genuinely nasty
recoveries. Only `CustomUser` moved; `Membership` stays in Phase 1.

---

## ADR-012: Recurring visits are materialized as rows

**Decided (Phase 3).** `RecurringPlan` stores an RRULE; a Celery beat task
materializes real `Job` rows ~8 weeks ahead.

**Rejected — computing occurrences on the fly.** Cleaner right up until one visit
is rescheduled, reassigned, or priced differently, at which point you need a real
row anyway and now you have two representations of the same thing.

**Related:** `Organization.timezone` exists from Phase 1 even though nothing reads
it until Phase 3. "Every Tuesday 9am" must stay 9am local across a DST boundary,
so recurrence expands in org-local time and stores UTC. Adding the field later
means a data migration on live rows.

---

## ADR-013: `pyproject.toml` is the single source of dependencies

`[tool.setuptools] py-modules = []` makes `pip install .` resolve dependencies
without trying to package the Django apps. No parallel `requirements.txt` to
drift.

**Rejected — `uv`:** not installed on this machine, and without a committed
lockfile its main advantage is unrealized. `pyproject.toml` is standard, so
adopting uv later needs no restructuring.

**Not carried over:** the source repo's `requirements.txt` is a `pip freeze` —
pandas, numpy, yfinance, peewee, and a pinned `pip`/`setuptools`/`wheel`, plus
the PyPI `uuid` package which shadows the stdlib module. ~60 packages → 15.

---

## Standing security notes

The source repo has a live `SECRET_KEY` (`app/app/settings.py`, line 26) and an
Alpha Vantage API key (line 29) committed in source and in git history. **If that
app is live, rotate both.** Nothing of the sort is carried into this repo, and
`gitleaks` runs in pre-commit to keep it that way.
