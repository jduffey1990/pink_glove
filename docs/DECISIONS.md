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

## ADR-009: Money is integer cents — but *rates* are Decimal

Amounts are integer cents, always. No floats. The source repo already does this
(`amount_in_cents`) and it is one of the things it gets right.

**Refined in Phase 2:** a *rate* is not an amount. $0.125 per square foot is a
real price, and rounding the rate to 12 cents would quote $216 instead of $225
on an 1,800 sqft house — losing $9 on every job, silently, forever. So
`Service.hourly_rate_cents` and `per_sqft_rate_cents` are `Decimal`, the
multiplication happens in `Decimal`, and the result is rounded to whole cents
exactly once at the end (`Service.quote_cents`).

`base_price_cents` doubles as a minimum charge for hourly and per-sqft
services, so a 400 sqft studio cannot price below the cost of showing up.

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

## ADR-014: Gate codes, alarm codes, and key locations are encrypted at rest

**Decided (Phase 2).** `base.fields.EncryptedTextField`, Fernet, keyed by
`FIELD_ENCRYPTION_KEY`. Applied to exactly three columns on `ServiceLocation`.

**Why these and not everything:** for a cleaning company, a leaked database is
not merely a privacy incident — it is a list of addresses paired with the codes
to get inside them. That is a materially different kind of harm from a leaked
phone number, and worth the cost. Encryption is applied deliberately rather than
broadly because the cost is real:

- **Encrypted columns cannot be filtered, ordered, or indexed on.** Fernet
  includes a random IV, so identical plaintext yields different ciphertext every
  time. A test asserts this, precisely so nobody later assumes otherwise.
- **Losing the key loses the data, permanently.** It belongs in a secret store
  *and* in backups — not only in the app environment.
- It protects a stolen dump. It does **not** protect against an attacker with
  application-level access, because the running app necessarily holds the key.

**Rejected — full-database encryption at rest (managed-Postgres option).** Worth
turning on too, but it protects against a stolen disk, not a stolen dump or a
compromised read-replica credential. It is not a substitute.

**Rejected — not storing codes at all.** Cleaners genuinely need them, and the
alternative is codes living in group texts, which is worse.

Nested serializers omit these fields entirely, so a customer list cannot spray
gate codes across the wire. Only the location detail endpoint returns them, and
only to staff.

`production.py` refuses to boot without the key, and the Dockerfile's
build-time `collectstatic` passes a throwaway one — a real key is never baked
into an image layer.

---

## ADR-015: Django's ValidationError renders as 400, not 500

**Decided (Phase 2).** `app.exceptions.exception_handler`, wired as DRF's
`EXCEPTION_HANDLER`, translates `django.core.exceptions.ValidationError` into
DRF's own.

Found by a test. `TenantModel.save()` raises Django's `ValidationError` when a
write points at another organization's record (ADR-002) — correct behaviour, but
DRF only understands its own exception type, so it escaped as an unhandled 500.
A caller pasting somebody else's id is a client error, and the response should
say so.

This applies globally: any model-level `clean()` or `save()` validation now
surfaces as a usable 400 instead of a stack trace.

---

## ADR-016: Access codes are revealed behind an audited click, and flagged after the fact

**Decided.** Reading a home's gate code, alarm code, or key location requires
`POST /api/customers/locations/{id}/reveal-access/` with
`{"acknowledged": true}`. Each reveal writes an append-only `AccessReveal` row:
who, which location, which fields, when, IP, user agent.

**Scope is deliberately narrow.** Ordinary schedule data — address, phone,
arrival time, what the job involves — is shown freely and never logged. A
worker needs all of it constantly, and recording every glance would bury the
signal in noise until nobody reads the log. What gets recorded is the discrete,
intentional act of asking for a code, which maps to a real moment: someone is
at a door.

**Nothing is blocked, and nobody is notified in real time.** The reveal always
succeeds. This is a change from the first draft of this design, which proposed
a hard block plus an owner notification — both were rejected as too heavy.
Blocking strands a worker over a job that got moved; live notifications train
the owner to ignore them.

**Flagging is an end-of-day pass, not a request-time decision** (Phase 3). A
reveal is flagged if it sat more than 1–2 hours outside its appointment window.
This *has* to happen after the fact: a job rescheduled at 4pm changes the
verdict on a 2pm reveal, so evaluating live would bake in a judgement from
facts that had not settled. A flag is a prompt for the owner to ask a question,
not an accusation and not an enforcement action. `review`/`reviewed_by`/
`review_note` record that a human closed it out.

**The acknowledgement is enforced server-side**, so the warning is part of the
API contract rather than a dialog the frontend could quietly stop showing, and
the log can state the user saw it. The exact copy lives in
`audit.models.ACCESS_WARNING` and is returned by the 400, so API and UI cannot
drift apart.

**Consequences accepted:**

- Codes became `write_only` on `ServiceLocationSerializer`. They can be set and
  changed normally; reading them back needs the reveal. `has_access_codes`
  lets the UI show "codes on file" without disclosing them.
- **They were removed from the Django admin entirely.** Admin logs changes but
  not views, so leaving them there was an unlogged way to read every code in
  the database — precisely the hole a determined person would use.
- The audit trail is owner/admin only. The people being recorded should not be
  able to curate the record. `AccessReveal.delete()` raises, there is no update
  or destroy route, and the admin registration is fully read-only.

**Known limit, stated plainly:** this deters and reconstructs; it does not
prevent. A worker with a legitimate reveal can write the code on their hand.
The value is that misuse becomes attributable, which is real but is not access
control — do not treat the log as though it were a lock.

---

## ADR-017: Reveal access is role-tier gated; only cleaners are assignment-bound

**Decided.** Resolves the open question ADR-016 and `docs/PLAN.md`'s Phase 3
section left unanswered ("what happens to a reveal with no job attached").
Once `Job`/`JobAssignment` exist (Phase 3), `reveal_access` splits by role
instead of using one rule for every staff tier:

- **Cleaner:** may reveal only a location tied to a `Job` they are currently
  assigned to. No matching `JobAssignment`, no reveal — the request is refused
  outright (`IsAssignedCleaner`, object-level), not logged-and-flagged-later.
- **Dispatcher and above:** unrestricted, as `IsDispatcherOrHigher` already
  grants them for `CustomerViewSet`/`ServiceLocationViewSet` generally. Their
  reveals are logged exactly as before; the end-of-day pass (ADR-016) flags
  one only when it falls outside the organization's business hours, since
  there is often no appointment window to compare against for this tier.

**Why split by tier instead of judging every unassigned reveal by business
hours alone** (the option Phase 3's spec had floated as the default answer):
a cleaner has no legitimate reason to be in a location's record without an
assignment — the assignment is the authorization boundary for that role, not
just a fact to evaluate after the fact. Business-hours-only flagging would let
any cleaner pull any customer's codes at any daytime hour and only get caught
after the fact; that is real-time exposure a flag cannot undo. Dispatchers and
admins are already trusted with the whole book, so a lighter, after-the-fact
instrument is the right fit for them.

**This narrows, but does not reverse, ADR-016's "nothing is blocked" stance.**
That rejection was about hard-blocking *any* reveal against a job that might
have simply moved — blocking there strands a worker over a scheduling change
that isn't their fault. This blocks a different, narrower thing: a reveal with
no job relationship at all, for the one role with no legitimate reason to have
one. ADR-016's mechanics (append-only log, enforced acknowledgement, admin
exclusion, review workflow) are unchanged for every reveal that still occurs.

**Consequences:**

- `ServiceLocationViewSet.get_permissions()` (`customers/views.py:44-46`) can
  no longer return a flat `IsStaff()` for `reveal_access`; it needs
  `IsDispatcherOrHigher() | IsAssignedCleaner()`, where the latter is a new
  object-level permission checking `JobAssignment` for the requested location.
- `AccessReveal.job` (already planned for Phase 3) is non-null for every
  cleaner-tier reveal by construction — a cleaner reveal with no resolvable
  job is now a 403 at request time, never a row to review later.
- The end-of-day evaluator only ever needs to reason about dispatcher+
  reveals (job-window comparison where a job exists, business-hours check
  where one doesn't) — the cleaner-without-a-job case it would otherwise have
  had to handle can't occur.

---

## ADR-018: Frontend is a fresh Vue 3 + Vuetify + TypeScript scaffold in `ui/`, not the source repo's shell

**Decided (Phase 3b).** A new Vite scaffold — Vue 3, Vuetify 3, Pinia, Vue
Router, TypeScript — lives at the repo root in `ui/`. It talks to the API with
session cookies and a CSRF header, the auth model the backend was built for
(`SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_HTTPONLY = False`, credentialed CORS
all already assume a browser SPA on a separate origin).

**Rejected — reusing the source repo's `ui/` tree.** It was the working
assumption since Phase 0, so the rejection needs stating. The tree is Vue 3.2
on Vite 3 with Vuetify 3.6, carries both a Vite config and a leftover Vue CLI
config, lists webpack plugins Vite never runs, and pulls moment, jsPDF,
html2canvas, and chart.js for features this product does not have. Every
screen is Pomarium-specific. The transferable parts — the axios plugin with
CSRF and credentials, the session store shape, and the Stripe.js wiring — total
under two hundred lines and are ported by hand. Adopting the tree would mean
upgrading and deleting most of it before writing the first screen; cleaner to
start from the current Vuetify template and copy those pieces in.

**Rejected — token (JWT) auth to "simplify" the SPA.** Session auth is already
built, tested, and hardened (ADR-008); the 2FA challenge and trusted-device
cookie are session-bound. Tokens would mean re-doing that and losing HttpOnly
protection on the credential. The SPA's only obligations are `withCredentials`
and reading the CSRF cookie.

**Rejected — server-rendered Django templates or HTMX.** Two of the three
audiences (dispatchers on a calendar, cleaners on a phone in a driveway) need a
stateful, responsive client. A cleaner's clock-in must feel instant.

**Why TypeScript when the source was JavaScript:** the API contract is
generated (ADR-019). Types are what make the generated schema useful; without
them it is documentation nobody reads.

**Consequences:** `ui/` is its own package with its own lint; `docker compose`
grows a `ui` service for development; a production build for the UI is deferred
with the deploy target (ADR-006). Nothing in `app/` knows the UI exists beyond
`CORS_ALLOWED_ORIGINS` and `FRONTEND_BASE_URL`.

---

## ADR-019: The OpenAPI schema is the frontend contract

**Decided (Phase 3a).** `drf-spectacular` generates the schema from the
viewsets. It is served at `/api/schema/` and browsable at `/api/docs/`, both
authenticated. The frontend does not hand-write request or response types: a
script regenerates `ui/openapi.yaml` and `ui/src/api/schema.d.ts` from the
backend, and both are committed so an API change shows up in the same diff as
the backend change that caused it.

**A test asserts the schema generates with zero warnings.** A viewset action
that spectacular cannot describe is a viewset the frontend cannot call
correctly; making that a test failure keeps `@extend_schema` from being
forgotten.

**Rejected — hand-maintained TypeScript interfaces.** They drift the first time
someone adds a serializer field under deadline, and nothing fails when they
do.

**Rejected — generating a full client SDK.** The generated *types* plus a thin
axios wrapper is enough; a generated client would own the CSRF and
organization-header logic that must stay in one hand-written place.

**Rejected — committing nothing and generating at build time.** Then a backend
change can silently break the UI build on someone else's machine. Committing
the schema turns that into a reviewable diff.

---

## ADR-020: Editing a recurring plan regenerates untouched future jobs and keeps the rest

**Decided (Phase 3a).** A `Job` materialized from a `RecurringPlan` carries
`plan_occurrence`, the UTC instant it was originally planned for. When the plan
is edited (rule, time, duration, location, service, price override, or
deactivated), future jobs from that plan that are **untouched** — still
`SCHEDULED`, `scheduled_start == plan_occurrence`, no time entries — are
soft-deleted and re-materialized from the new definition. Any future job that
has been rescheduled, has changed status, or has been worked is kept as it is
and stays linked to the plan. The API reports `{"regenerated": n, "kept": m}`
so the dispatcher sees what happened.

**Why `plan_occurrence` and not `scheduled_start` as the idempotency key.**
The materializer runs daily. If a dispatcher moves next Tuesday's visit to
Wednesday, keying on `scheduled_start` re-creates a Tuesday job the next
morning and the customer gets two visits. Keying on the originally planned
instant means "this occurrence already exists, wherever it was moved to".

**Rejected — never touching materialized jobs on plan edit.** Eight weeks of
stale jobs at the old time is the common case for a plan edit, and a
dispatcher will not hand-fix forty rows.

**Rejected — regenerating every future job.** Throws away the reschedule the
customer asked for last week and the cleaner the dispatcher assigned by hand.

**Rejected — a "detached from plan" flag set on any manual edit.** Same
outcome as the untouched rule, but needs every write path to remember to set
it. The rule derives the answer from state that is already there.

**Accepted cost:** an assignment added by hand to an otherwise untouched future
job is lost on regeneration and replaced by the plan's `default_assignees`.
The response counts make that visible; a dispatcher who needs to keep such an
assignment can reschedule the job by a minute to pin it.

---

## Standing security notes

The source repo has a live `SECRET_KEY` (`app/app/settings.py`, line 26) and an
Alpha Vantage API key (line 29) committed in source and in git history. **If that
app is live, rotate both.** Nothing of the sort is carried into this repo, and
`gitleaks` runs in pre-commit to keep it that way.
