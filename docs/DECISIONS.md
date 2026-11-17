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

**Superseded by ADR-027 (Phase D, 2026-09-17): the target is Fly.io.** The env
seam described here is what made the choice a configuration file rather than a
rewrite, and it stays.

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

**The fork below is now decided -- see ADR-024.** Kept for the reasoning.

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

**Amended by ADR-032 (visit booking, 2026-11-16): "untouched" also requires the price, end and note to equal what the plan stamped, and a kept visit is re-keyed to its day.** Kept for the reasoning.

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

---

## ADR-021: A visit is suppressed by cancelling it, not by deleting it

**Decided (Phase 3a).** `materialize_plan` looks for an existing occurrence
among **live** rows only. Soft-deleted jobs do not block re-creation, so a job
that is deleted reappears on the next nightly run. A job that is **cancelled**
does not: it is a live row at that occurrence, and it carries the reason.

**Why it has to be this way.** The unique constraint that makes materialization
idempotent, `unique_job_per_plan_occurrence`, is itself partial on
`deleted_at is null` — it has to be, because ADR-020's regeneration works by
soft-deleting untouched future jobs and rebuilding them from the plan's new
definition. If the materializer treated soft-deleted rows as existing,
`regenerate_plan` would delete eight weeks of jobs and create nothing.

**Rejected — making the constraint and the lookup include deleted rows.** Then
regeneration cannot rebuild an occurrence it just dropped, and ADR-020 needs a
different mechanism (a "superseded" flag, or hard deletes) to work at all.
Hard-deleting loses the audit trail on a row that may already be referenced by
an `AccessReveal`.

**Rejected — having the materializer skip occurrences with a soft-deleted
job.** It reads as the intuitive behaviour and it is what most people expect on
first reading, which is why the rejection is written down. It cannot be
distinguished from the regeneration case: both look like "this occurrence has
a soft-deleted job". Distinguishing them would mean recording *why* the row was
deleted, which is the "detached" flag ADR-020 already rejected.

**Consequences:**

- `JobViewSet.destroy` refuses (409) a job with recorded time and says to
  cancel instead. Deleting one would orphan hours already billed against it.
- Cancelling is the documented way to call off a visit, and the UI offers it;
  deletion is for a job created by mistake.
- Stated in a comment in `scheduling/services.py` at the lookup itself, where
  it would otherwise read as a bug.

---

## ADR-022: `ui/` is built on Vuetify 4 and requires Node 22

**Decided (Phase 3b).** Amends ADR-018, which said Vuetify 3.

**Vuetify 4, not 3.** ADR-018 was written while 3 was the current major; it
named the version it would have got from the scaffold, rather than rejecting 4
(which did not exist). `npm create vuetify` now produces Vuetify 4, and
scaffolding one major behind would recreate exactly the staleness ADR-018
rejected in the source repo's `ui/` tree. Vuetify 3.13 is still maintained, so
this was a real choice and not a forced one.

**Rejected — pinning Vuetify 3 to match the ADR's letter.** More third-party
examples still target 3, which is the honest argument for it. Not enough to
start a greenfield scaffold on a version that is already superseded, when the
screens here are ordinary lists, forms and dialogs that both majors do well.

**Node 22 is a hard requirement, not a preference.** `eslint-config-vuetify`'s
toolchain calls `Object.groupBy`, which landed in Node 21. On Node 20 linting
does not degrade — it crashes with `TypeError: Object.groupBy is not a
function` from inside a dependency, which reads as a broken install rather
than an unmet requirement. Pinned in three places so it fails clearly instead:
`ui/.nvmrc`, `engines` in `ui/package.json`, and `node:22-alpine` for the
compose service. The Vite build and the dev server themselves run on Node
20.19; it is only the lint toolchain that does not.

**Consequence:** the `ui-eslint` pre-commit hook is a `local`/`system` hook
running the project's own eslint, not a pinned mirror — the config has to
agree with the versions in `ui/package-lock.json`.

---

## ADR-023: The frontend renders server-owned copy and server-owned rules

**Decided (Phase 3b).** Two places where the obvious thing is to hardcode in
the client, and both are refused.

**The job state machine.** The status buttons are rendered from the `allowed`
list in the 409 body that a refused transition returns, not from a copy of
`ALLOWED_TRANSITIONS`. A client-side copy is correct until the day the
backend's rules change and nothing in the UI fails.

**The access-code warning.** The reveal dialog fetches its text from
`GET /api/customers/locations/{id}/access-warning/`, which returns
`audit.models.ACCESS_WARNING`. The alternative is a string literal in the
component that can quietly stop matching the warning the audit row claims the
user was shown — and that record is the entire point of ADR-016.

**Amended (Phase 3b, after use).** This first worked by asking for a reveal
*without* acknowledgement and reading `detail` out of the resulting 400. Same
single source of truth, and it needed no new endpoint — but it put a red 400
in the browser console on a completely healthy path, where it is
indistinguishable from a real failure, and would do the same in server logs
and any error tracker. A dedicated GET costs one trivial action and keeps a
failure looking like a failure. The endpoint is gated exactly as the reveal
is, and logs nothing: no reveal has happened.

**Rejected — generating a client SDK so the rules come along for free.** They
would not: neither of these is in the schema. The schema types the shapes; the
behaviour still has to come from a live response.

**Accepted cost:** the reveal dialog makes two requests where one would do,
and a first-time render of the status buttons uses a local list until the
server has had a chance to disagree. Both are cheap next to the failure they
prevent.

---

## ADR-024: Two Stripe relationships -- a platform subscription, and Connect with direct charges

**Decided (2026-09-17).** Settles the fork ADR-007 flagged.

Money moves in two directions and they share nothing but the word Stripe:

| | Tenant pays for pink_glove | Tenant's customers pay the tenant |
|---|---|---|
| Stripe object | Subscription on the **platform** account | Charge on the tenant's **connected** account |
| Required | Yes, to use the product | No -- optional per tenant |
| Webhook | platform endpoint, platform secret | Connect endpoint, Connect secret, `account` on every event |
| Effect here | lapse -> `Organization.is_active = False` -> read-only | a `Payment` row (ADR-025) |

**Connect uses direct charges, Stripe-hosted onboarding, and the tenant's own
Stripe dashboard.** The charge, its refunds, its disputes and any negative
balance belong to the cleaning company.

**Rejected -- destination charges through the platform account.** A smoother,
fully branded flow, and it makes the platform liable for every tenant's
chargebacks and the first line of support for every payment question. Wrong
trade for a one-person platform.

**Rejected -- one Stripe account for everything** (what the source repo does).
Correct for one tenant. With two, customer ownership and payouts are tangled in
a way that cannot be untangled without moving money.

**An application fee setting exists and ships at 0%.** Revenue is the
subscription. A fee on card payments makes cards dearer through pink_glove than
direct, which pushes tenants back to cash -- but turning one on later should be
configuration, not a release.

**Tenant resolution for webhooks.** A Connect event names an `account`; the view
resolves it to an organization *before* enqueuing, and the task takes
`organization_id` like every other (invariant 3, ADR-004). An event for an
unknown account is ledgered and dropped, never guessed at. The `StripeEvent`
idempotency ledger keys on (event id, account).

**Order of build:** invoicing first (4a, no Stripe), then Connect (4b), then the
subscription (4c). The subscription is needed before anyone is charged, not
before a pilot tenant can use the product.

---

## ADR-025: Payments are a ledger, and Stripe is one payment method

**Decided (2026-09-17).** Cleaning customers pay by cash, check, money order and
Zelle at least as often as by card. Recording those is not a workaround beside
the real system -- it **is** the system, and Stripe is one more way a row gets
written.

- An invoice is paid when its non-voided payments sum to its total. Status is
  derived from the ledger, never set by hand. Partial payments fall out for
  free.
- A `Payment` has a `method`, an amount, a received date, a free-text
  `reference` (check number, Zelle confirmation), who recorded it, and -- for a
  provider -- `provider` and `provider_reference`. A Stripe payment differs
  only in that a webhook wrote it.
- **Payments are voided with a reason, never edited or deleted.** "Did they pay
  me?" is the dispute this table exists to settle; same reasoning as the
  reveal trail (ADR-016).
- **Tips are a separate amount on the payment** (`tip_cents`). They are not
  revenue, are not taxed, and never count toward settling the invoice.

**Rejected -- a payment-provider abstraction now.** There is one provider. Two
columns are the seam; an interface designed against a single implementation
encodes that implementation's shape. Build it when a second provider arrives.

**Rejected -- splitting tips among the assigned cleaners.** That is payroll
logic arriving before there is a payroll feature. A per-job tip report is
enough to hand the money on.

---

## ADR-026: An invoice is a snapshot

**Decided (2026-09-17).** Everything an invoice says is fixed when it is issued:
line amounts, the tax rate, the tax, the total, the bill-to name and address.
Changing a service price, the organization's tax rate or a customer's address
must never alter a document that has already been sent. This extends the rule
`Job.price_cents` already follows.

- **A completed visit's line is `Job.price_cents`** -- the price agreed when the
  visit was scheduled, not a re-quote. For an hourly service the dispatcher is
  shown the time actually worked beside it and can re-price the line from
  actuals in one action; it is never automatic, because a crew running long is
  not self-evidently the customer's bill.
- **A no-access visit bills the organization's no-access fee** (flat cents or a
  percentage of the job price, default 0 = no line), editable or waivable per
  invoice before issue.
- **Tax is one flat rate per organization, and each service is taxable or not.**
  Rates are `Decimal` (ADR-009); tax is computed once per invoice over the
  taxable lines and rounded once to whole cents, so per-line rounding cannot
  accumulate.
- **A draft is editable; an issued invoice is not.** Corrections after issue are
  a void and a new invoice, so numbers are never reused and the sequence has no
  silent rewrites.

**Rejected -- Stripe Tax.** It covers only Stripe-paid invoices; a cash invoice
would still need a rate from somewhere, so there would be two tax systems.
**Rejected -- a rate per service location.** More correct for a multi-city
operator and more data entry for everyone else; it layers onto this design
later (the snapshot already stores the rate used). **Rejected -- no tax.**
Adding it afterwards means migrating every total and every screen showing one.

---

## ADR-027: Fly.io, one image, and the frontend served from the API's origin

**Decided (Phase D, 2026-09-17).** ADR-006 deferred the deploy target; 4b ends
the deferral because Stripe cannot deliver a webhook to localhost. Three
decisions, taken together because each shapes the others.

**1. The target is Fly.io.** One `deploy/fly.toml` describes `web`, `worker`
and `beat` as process groups of one image -- the same shape as
`app/docker-compose.yml` -- with the migration as the release command, Fly
Postgres, Upstash Redis and a Tigris bucket for media, all attached as
secrets. Staging and production are two apps from the same file; the app name
is given on the command line so the file cannot quietly deploy to the wrong
one. `NUM_PROXIES=1`: Fly's proxy is the one hop, and it appends the client
address to `X-Forwarded-For`. Fly redirects to HTTPS at the edge, so
`SECURE_SSL_REDIRECT` stays off. Staging costs a few dollars a month.

**2. The image carries the frontend build, and gunicorn serves it.** The
Dockerfile moved to the repository root and gained a Node stage; Vite's output
lands in `/ui/dist`, whitenoise serves its files at the root URL (hashed
assets under `/assets/` are cached for a year -- `app.middleware.static_files`
teaches whitenoise that name shape), and a catch-all Django view answers every
route the SPA's router owns with `index.html`, `Cache-Control: no-cache`. The
catch-all is fenced off from `/api/`, `/admin/`, `/health/`, `/static/` and
`/media/` so an unknown API path stays a 404. A production build calls the
API with relative URLs; `VITE_API_BASE_URL` is for the dev server, which still
runs on its own port.

**3. Therefore one origin, and the cookies tighten.** With nothing cross-origin
calling the API, `CORS_ALLOWED_ORIGINS` is no longer required in production
and the session and CSRF cookies are `SameSite=Lax`. Both follow from one
setting: an allowed origin loosens the cookies to `None` again, so a
separately hosted frontend remains possible by configuration, and the two can
never disagree.

**Also decided here, because production settings now fail at import rather
than at the first upload:** `MEDIA_BACKEND=filesystem` is refused unless
`MEDIA_ROOT` is set explicitly (the default is inside the container and lost
on every deploy); a cloud backend without a bucket is refused; a missing
frontend build is refused unless `UI_DIST_DIR` is set empty on purpose. The
image installs the `s3` extra, so the same image runs everywhere. Objects are
written private and read through URLs that expire in `MEDIA_URL_TTL_SECONDS`.

**Rejected -- Render.** The closest alternative: a `render.yaml` blueprint
would describe the same three processes and preview environments come free.
Each background worker is its own paid instance and there is no
S3-compatible storage of its own, so staging lands around four times the
cost for the same shape, and Stripe webhooks would need a fourth service or
the web one. Nothing in the code prefers Fly; the env seam from ADR-006 means
moving is `render.yaml` and a different set of secrets.

**Rejected -- Cloud Run (what the source repo used).** Celery needs an
always-on worker and exactly one beat; Cloud Run is built for request-scoped
containers, so both become a second product (Cloud Run jobs, or a GCE VM) and
the operational surface roughly doubles. The source repo's `terraform/` and
four `cloud_deploy_*.yaml` files were what ADR-006 declined to carry over.

**Rejected -- a VPS with the compose file.** Cheapest, and the compose file
already works. But Jordan then owns Postgres backups, Redis persistence, TLS
renewal and the box itself, on a product whose data is people's alarm codes.
`deploy/docker-compose.rehearsal.yml` is that shape, kept for rehearsal
only; the production check refusing filesystem media is what stops it being
quietly promoted.

**Rejected -- a separately hosted frontend (Cloudflare Pages, Netlify, a
bucket).** Free and fast, but it is the cross-origin shape: `SameSite=None`
cookies, `COOKIE_DOMAIN`, CORS kept in step with every hostname, a second
deploy pipeline, and a frontend that can go live ahead of the API it was
generated against (ADR-019). One image means the schema and the code that
consumes it ship together or not at all. It remains available by setting
`CORS_ALLOWED_ORIGINS`, `VITE_API_BASE_URL` and `UI_DIST_DIR=`.

**Rejected -- deploying production from CI.** `main` deploys to staging once
Jordan sets the `FLY_STAGING_APP` variable and a deploy token scoped to that
one app; production is `fly deploy` from Jordan's machine. `CLAUDE.md` makes
production Jordan's alone, and a workflow that could reach it is one bad merge
from doing so.

**Consequences.** `docker build` runs from the repository root, and the
compose file's build context is `..` -- so a development `docker compose
build` runs the Node stage too and then sets `UI_DIST_DIR` empty, discarding
what it built; the price of one image. The frontend build is refused rather
than skipped when missing. `index.html` is never served as a file: whitenoise
skips it so it always goes through the view, with `no-cache` and the
clickjacking header, and `XFrameOptionsMiddleware` moved above whitenoise so
the files whitenoise does serve carry the header too. `MEDIA_BACKEND=filesystem`
is refused in production outright -- nothing serves `/media/` outside `DEBUG`,
so a volume would not have helped. `FRONTEND_BASE_URL` is required and must
be https, because a customer's sign-in token travels in it. A staff sign-in
in a rehearsal is read from the web log (`EMAIL_BACKEND` is env-selectable
for that). The `FIELD_ENCRYPTION_KEY` backup rule in `.env.example` becomes
operational: `docs/DEPLOY.md` says where it must live.

---

## ADR-028: The platform subscription is enforced from a coverage date, with a trial of our own

**Decided (2026-10-09).** Fills in what ADR-024 left to Phase 4c: how a
tenant comes to be covered, what a lapse is, and what it costs them.

**A flat monthly price per organization, held in Stripe.** The amount is a
Price in the dashboard; `STRIPE_PLATFORM_PRICE_ID` names it and the code
never knows the number.

*Rejected -- per-seat pricing.* A cleaning company's headcount moves weekly,
and a one-person platform does not want to police seats.

*Rejected -- the price in code or settings.* Changing it would be a deploy,
and Stripe already versions prices.

**The trial is ours, not Stripe's.** `Organization.trial_ends_at` is stamped
when the row is created, `PLATFORM_TRIAL_DAYS` out, and extended by hand in
the admin when a pilot needs longer. Nothing in Stripe exists until the owner
subscribes. This is what ADR-024 meant by "needed before anyone is charged,
not before a pilot tenant can use the product".

*Rejected -- Checkout with `trial_period_days`.* It puts the card form in
front of a pilot on day one, and a trial that ends in Stripe but was never
started here is a lapse the app cannot explain.

**Coverage is a date, and the grace runs from it.** `covered_until` is the
later of the trial's end and the end of the last period Stripe was *paid*
for -- on the pinned API version the period is on the subscription item,
and a renewal Stripe is still trying to collect has already advanced it, so
the item's `current_period_start` is the answer until its invoice is paid.
`PLATFORM_GRACE_DAYS` after `covered_until`, a daily task sets `is_active =
False`, which the baseline gate already made read-only. One rule for every
lapse: an expired trial nobody subscribed after, a renewal whose card
failed, a subscription the owner cancelled.

*Rejected -- deactivating on Stripe's `past_due` or `canceled` directly.*
Stripe's retry schedule and its "when retries fail" behaviour are dashboard
configuration, and `past_due` arrives the morning a card fails, which is no
time to lock a dispatcher out of the day's schedule.

*Rejected -- computing read-only-ness per request from the dates.*
`is_active` is already the chokepoint `IsOrgMember` reads with no extra
query, and the admin flag already means "read-only" to the rest of the
product. A second meaning for the same state is a bug factory.

**Deactivation by billing is reversible only by billing; deactivation by
hand is not touched.** `deactivated_for_billing_at` records which it was. A
subscription coming good reactivates an organization that billing
deactivated; one switched off in the admin stays off whatever Stripe says.

**Stripe's Customer Portal, not our own card pages.** Updating a card,
reading receipts, cancelling at period end and resuming are the portal's.
The product gets two buttons and no PCI surface.

**The state is re-read, never copied.** Every platform webhook handler and
the daily task call the same `refresh_subscription`, which fetches the
Subscription and then applies the coverage rule, for 4b's reason: Stripe
does not promise order, and a lost webhook costs a day, not a tenant.

**A deactivated tenant's customers can still pay it.** The pay page and its
checkout are public, by token, and the Connect webhook writes the payment
regardless of `is_active`: the money is the tenant's, not ours, and an
invoice already sent is a document their customer is entitled to settle.
Recording a cash payment is a write the tenant cannot make until they are
current -- the read-only rule working as intended.

**The one hole in the read-only rule.** The three subscription endpoints
(checkout, portal, refresh) set `allowed_while_inactive = True`, which
`IsOrgMember` honours before refusing an unsafe method. Without it the owner
of a lapsed organization could see the bill and not pay it. It is explicit
on the view, and the phase gate lists every view that sets it.

**Optional on the server, like Connect.** With no price nobody is ever
deactivated for billing, the subscription card is absent and the banner has
nothing to say. Production refuses a price without the platform webhook
secret, and a price without the key.

---

## ADR-030: What the reports mean: which day an entry is on, which date a payment is on, and whose balance it is

**Decided (2026-11-16).**
An owner's first two questions are "what do I owe my cleaners this week" and "who owes me".
Three read-only reports answer them: a timesheet (`scheduling.reports`), receivables and collections (`billing.reports`).
A report is a definition as much as a query, and each definition below has an obvious alternative that gives a different number.
The numbers are what someone is paid from, so the definitions are recorded here rather than left in a docstring.

**1. A time entry belongs, whole, to the organization-local day it was clocked in on.**
A shift from 22:00 to 02:00 is four hours on the day it began and none on the next.
The same rule decides the edge of a period: an entry that clocks in on the last day and out after midnight is entirely inside it.
So every entry is in exactly one day and one period, and two pay periods laid end to end add up to the whole with nothing counted twice or dropped.
The day's bounds are `Organization.local_day_bounds`, the ones the jobs filter and the time-entry filter already use, so a 23 or 25 hour day is the right length (invariant 8).
`TimeEntryFilterSet` was already filing entries by `clock_in`; the report agrees with the list beside it.

**Rejected: splitting an entry at local midnight.**
It is the more exact answer to "how many hours were worked on Tuesday", and nothing here asks that: there is no daily overtime rule and no payroll feature (ADR-025 rejected one for tips on the same grounds).
It would make "entry count" mean something different from the number of rows, put half of one shift in each of two pay periods, and need its own answer for the hour that happens twice in November.
If daily overtime arrives, it layers on top: the entries and their instants are unchanged.

**Rejected: the day of clock-out.**
An open entry has none, so it would belong to no day until someone clocked out, and then move.

**Rejected: the day the job was scheduled for.**
A visit rescheduled after it was worked would carry its hours into a different pay period, possibly one already paid.

**2. Seconds are elapsed time between two UTC instants, truncated per entry, and a total is the sum of the rows under it.**
On the night the clocks go back, 00:30 to 02:30 on the wall is three hours worked and is reported as three.
Truncating per entry rather than once on the total means a total never disagrees with the rows printed beneath it.

**3. An open entry is listed and flagged, and counts for nothing.**
`seconds` and `clock_out` are null, not zero, which would read as worked.
`entry_count` includes it and `open_entry_count` says how many there are, so a total that is still short says so.

**4. A correction is the entry.**
A dispatcher's PATCH rewrites `clock_in` and `clock_out` in place, and `TimeEntry` keeps no earlier version and has no void.
The report reads the row as it stands, including which day it is now on.
An audit trail of corrections would be a model change and a different feature.

**5. Soft deletes follow the foreign key.**
The manager hides soft-deleted rows only on the model being queried, never across a join (phase gate item 5), so each join is a decision.
The rule: where the foreign key cascades, the child does not outlive the parent; where it protects, it does.
`TimeEntry.job` cascades, so an entry on a soft-deleted job is left out by an explicit join filter.
`Job.customer` and `Invoice.customer` protect, so hours worked for, and money owed by, a customer since removed from the address book stay in the reports under the customer's name.
The API lets a dispatcher delete a customer who has history; dropping the hours would underpay a cleaner and dropping the invoice would understate what is owed, and the name is on the invoice's own snapshot regardless (ADR-026).
Invoice lines are not read at all: an issued invoice's total is the snapshot on the row.

**6. Receivables are the live ledger, aged by due date. `as_of` moves the aging clock and nothing else.**
A balance is `billing.services.balance_cents`, the same function the invoice page uses (ADR-025).
So that it can be asked of every unpaid invoice in one query, `services.with_paid_cents` carries the ledger's paid total on each invoice, and `paid_cents` uses it when it is there: one definition of "live payment", one of "balance", reached two ways.
A test holds the two readings together over a ledger with a bounced check, an overpayment and a soft-deleted payment in it.
The buckets are not yet due, 1-30, 31-60, 61-90 and over 90 days past the due date; due today is not yet late, which is `services.is_overdue`'s rule and is tested against it.
Only issued invoices count: a draft is not a debt and a void invoice is not one any more.

**Rejected: rewinding the ledger to `as_of`**, so that the report says what was owed on that date.
It is what an accountant means by "aging as of month end", and it needs a second, date-bounded balance formula beside the one the rest of billing uses.
`received_on` is the date written on the check, typed by a person, so a rewind would also change every time a late check was entered.
The question at the trial is who owes the organization now; `as_of` exists so a test, or someone looking a week ahead, can say which day "now" is.
The frontend does not offer it and always asks for today.

**Rejected: a balance computed in SQL inside the report.**
Faster by nothing that matters, and a second formula that a change to the ledger's rules would not reach.

**7. Collections file a payment under `received_on`, and a voided payment is in no period.**
`received_on` is an organization-local date on the row, so there is no instant to convert, the same reasoning as the payment filters.
A check that arrived on Friday and was entered on Monday is Friday's money.
Voiding says the money did not arrive or went back, so the payment leaves the period it was first counted in; a report for a closed month can therefore change, and that is the ledger being right rather than the report being wrong.
A payment against an invoice voided afterwards still counts: it arrived, and the refund is what voids it.
Net is amount plus tips less the provider's fees, which is what reached the organization; tips are shown apart because they are not revenue (ADR-025).
A card payment whose fee Stripe has not reported yet has nothing to subtract, so `fees_pending` counts those and a net that is still too high says so.

**Rejected: `created_at`, when the row was written.**
It dates the typing, not the money, and it is a UTC instant that would need converting.

**Rejected: estimating a missing fee from Stripe's published rate.**
Phase 4b reads the fee from the balance transaction for the reason that the rate is not the fee.

**8. Owner and admin only, one tier above the rest of billing and scheduling.**
A dispatcher raises and chases individual invoices and corrects individual time entries; what everyone was paid and the business's whole position are not theirs to read.
The route and the navigation mirror it, and the API refuses independently.

**9. A CSV is its own URL, amounts in it are decimal dollars, and only text is neutralised.**
Each report has a `.../csv/` twin rather than `?format=csv`, so the schema describes a `text/csv` response without a renderer that would also have to render errors.
The API speaks integer cents; a spreadsheet's reader wants dollars, so cents are converted by moving the decimal point of an integer, never by dividing a float (ADR-009), and the column is named `..._usd` so it cannot be read as cents.
Any text cell that begins with `=`, `+`, `-`, `@`, a tab or a carriage return is prefixed with an apostrophe, and every text cell is quoted so a typed value stays one cell whatever the spreadsheet splits on.
Numbers the server computed are written bare: they cannot carry a formula, and as text they could not be summed.
The file opens with a byte-order mark, because without one Excel reads UTF-8 as the machine's legacy code page.
There is no totals row: a spreadsheet sums a column, and a total among the rows is counted twice by the first person who does.

**Rejected: cents in the CSV.**
Unambiguous and exact, and the first thing anyone would do with the file is divide a column by a hundred.

**Rejected: neutralising every cell that starts with a minus sign, numbers included.**
It is the literal reading of the usual advice and turns a genuinely negative amount into text.
No amount in these three reports can be negative today; the rule is written for the day one can.

**10. The reports live in `<app>/reports.py`, in the app that owns the data.**
The timesheet is in `scheduling`, the money in `billing`, and both use `base.exports` for the file.
`billing` may import `scheduling` and nothing imports the other way.

**Rejected: a `reports` app.**
It would import both `scheduling` and `billing`, and it would be the first place a combined report (hours against revenue) got written with its own idea of a balance.

**11. A read-only organization still reads its reports, and the platform subscription is in none of them.**
ADR-028 makes an organization past its grace read-only, not locked out.
Reports are reads and the data is the tenant's, so the owner of a lapsed organization can read and download all of them, and they keep moving: a deactivated tenant's customers can still pay it by card, and that payment is reported.
No report view sets `allowed_while_inactive`; `IsOrgMember` only refuses unsafe methods, so the hole ADR-028 opened for the three subscription views is not needed here and stays three views wide.
The two money relationships of ADR-024 stay apart.
What the tenant pays the platform is not money the tenant received and not money its customers owe it; platform events write `StripeEvent` rows and fields on the organization, never an `Invoice` or a `Payment`, and those two tables are all the money reports read.
A test runs every platform event type through the handler and holds both tables and all four money endpoints unchanged.

**Rejected: hiding the reports from a lapsed organization**, as a lever to get the bill paid.
The hours are owed to the crew and the invoices are owed to the tenant whoever is behind on a subscription, and ADR-028 already chose read-only over locked out for that reason.

**Consequences.**
A timesheet period is capped at `scheduling.filters.MAX_RANGE_DAYS`, the jobs filter's own cap, and collections at 366 days; the frontend does not copy either and shows the server's refusal.
A closed month's collections can change when a payment in it is voided.
The figure `with_paid_cents` carries is as old as the queryset, so it is for reading: nothing that writes to an invoice may be handed an annotated instance.
The demo seed's two tenants are owed identical amounts, because every seeded invoice is one untaxed service; tenant isolation on these reports is proved by the tests, not by the seed.

---

## ADR-031: A customer import matches on one key, never edits what is on file, and is all or nothing

**Decided (customer import, 2026-11-16).**
`POST /api/customers/customers/import/` loads a CSV of an existing customer book.
One row is one customer and one service location.
Four decisions, taken together because each one is what makes the next one safe.

**1. A customer has exactly one key.**
The email, compared without case, when the customer has one.
Without an email, the whole name (first, last, company) plus the digits of the phone number.
The key is computed the same way from a file row and from a stored customer (`customers.services.customer_key`), so two rows are the same customer when their keys are equal and a row belongs to a stored customer when their keys are equal.
A row with no email therefore never matches a customer who has one, and the answer never depends on the order rows arrive in.
Matching reads the caller's organization only, and only live rows: a soft-deleted customer is not "on file".

**2. An import adds; it never edits.**
A row whose customer is already on file leaves that customer exactly as it is, whatever the row's other cells say.
Its location is added if that customer has no location at that address, and skipped if they do.
"That address" is line 1, line 2 and the postal code, compared without case or spacing.
A skipped location keeps the access codes it already has.
This is what makes importing the same file twice add nothing, which a test asserts.
Within one file, the first row for a customer supplies their details and later rows only add locations.

**3. A row that matches two stored customers is an error, not a guess.**
Email is not unique in this schema, so two customers typed in by hand can share one.
Attaching the row's location to either would put somebody's alarm code on a record chosen by sort order.

**4. A dry run is the default, and a real import is all or nothing.**
`dry_run` defaults to true and a dry run plans without opening a transaction or saving anything.
A real import makes its plan inside the transaction that writes it, behind a lock on the organization's row, and writes nothing if any row is an error: the answer is a 400 carrying every row's outcome.
A dry run that finds errors is a 200, because the preview worked (the same reasoning as ADR-023's amendment: a healthy path should not look like a failure).

**Also decided here.**
Every row is validated by `CustomerSerializer` and by `ImportedLocationSerializer`, a subclass of `ServiceLocationSerializer` whose one change is that `customer` is supplied by the importer rather than read from the row.
Which columns are required is read off those serializers, not listed.
The header-to-field table is `customers/import_columns.py` and nothing else knows a header; the template endpoint, the unknown-column report and the reader are all built from it.
That table is a best guess, written before any real export from a business was available.
Owner and admin only: a dispatcher may add a customer by hand, not load a book.
The response and the log never carry an access code (ADR-014, ADR-016): a row's outcome has a name, an address, a sentence and error messages, and the one log line is counts.

**Amended (2026-11-16, decided by Jordan, before first use): a blank status imports as `active`.**
A blank cell means "not given", so the serializer field's own default applies, with one exception.
A blank `status` cell, or a file with no `status` column at all, makes the customer `active`, where the API's default is `lead`.
A file of an existing customer book is a list of customers, and importing two hundred of them as leads would have every one of them to correct by hand.
The default is one attribute on one row of the column table (`when_blank` in `customers/import_columns.py`), not a rule in the importer: the value is put in the row's data before the cells are read and then validated by `CustomerSerializer` like any typed one.
A status that is given is kept as written, so `lead` stays `lead`, and one that is not a status is still a row error.
It reaches only a customer the import creates: a customer already on file is never edited (decision 2), so an existing `lead` stays one.
The template's hint for the column says so ("Blank is imported as active."), which is the server's copy.

**Amended (2026-11-17, on rebasing over Phase 4c): a read-only organization cannot import, and the lock is rechecked.**
ADR-028 makes a deactivated organization read-only at `IsOrgMember`, by method.
The import obeys that with nothing of its own: both actions state `IsAdminOrHigher`, neither sets `allowed_while_inactive`, and a real import and a dry run are both a 403 carrying `ORGANIZATION_INACTIVE`.
A dry run is refused as well, on purpose: it is a POST, and a preview of a write that cannot happen is a dead end.
The row lock from decision 4 gained one line.
The permission check reads the row the request loaded; the import then waits for the lock, which the subscription checkout holds across calls to Stripe, and the platform webhook can deactivate the organization meanwhile.
Under the lock the row is read again, and one that was active when the request was let in and is not now is refused with the same sentence.
One that was already inactive when the request was let in is left to the permission layer, which is how a superuser can still put a deactivated organization right.

**Rejected -- leaving a blank status as `lead`.**
What this first shipped with, on the reasoning that an import should behave exactly as the create path does.
The create path's default answers "what is somebody who was just typed in", which is usually an enquiry.
An import answers a different question.

**Rejected -- changing the model's or the serializer's default to `active`.**
It would make the import right by making the create path wrong: a customer added by hand from a phone call would become `active` with nobody having decided so.
The default is the import's alone.

**Rejected -- email as the only key, with a row that has none always created.**
The obvious rule, and it makes a second import of the same file duplicate every customer without an email.
A cleaning company's book is full of them.

**Rejected -- falling through from email to name and phone against anyone.**
It looks more forgiving ("Dana Wu with no email is surely the Dana Wu who has one") and it makes the outcome depend on row order.
If the row without the email comes first it creates a customer, and the row with the email then finds nobody with that email and creates a second.
One key per customer has no such case.

**Rejected -- updating the stored customer from the file (an upsert).**
A spreadsheet exported last month would silently overwrite a phone number or a gate code corrected by hand since, with nothing in the preview to say which cells changed.
An import is a loader, not a sync.
If the business needs bulk edits, that is a different feature with a field-by-field diff in its preview.

**Rejected -- skipping the whole row when the customer exists.**
Simpler to state, but a customer typed in by hand before the import would never get their locations from the file, and the person importing would have to notice which ones.
Adding a location is the one write to an existing customer that cannot destroy anything.

**Rejected -- importing the good rows and reporting the bad ones.**
The person is then holding a file that is half in the system, and the fix is to work out which half.
With all or nothing the loop is one step: fix the rows the 400 names, upload the same file again.

**Rejected -- a second set of validation rules for import, or parsing the file in the browser.**
Either is a copy of the create path's rules that is correct until the day a serializer changes.
The browser sends the file and renders what comes back.

**Rejected -- a background task with progress.**
The file would have to be stored somewhere between the request and the worker, and it is full of access codes in plaintext.
Bounding the file instead (1 MB, 2000 rows) keeps it one request: 2000 rows plan in about 3 seconds and write in about 6 on a laptop, against a 60 second worker timeout.

**Consequences accepted.**
Two different people with the same name, no email and the same phone (or no phone) are one customer to an import; the preview says "Another location for the customer on row N", which is where a person catches it.
A customer whose email in the file differs from the one on file is a new customer.
An address written two ways ("1 Elm St", "1 Elm Street") is two locations.
A blank cell means "not given", so the field's own default applies, except `status` (see the amendment above): the same customer is `active` when imported with no status and `lead` when created through the API with none.
A row's outcome shows its name and address cells, and a validation message for a choice column quotes the cell it refused.
So a file whose columns are shifted can have a cell that was meant to be a code shown back, once, to the person who uploaded it: the server cannot tell a code from an address when it arrives under the wrong header.
Cells under the three access-code headers themselves are never shown, and nothing from any row is logged.
A cell that starts with `=`, `+`, `-` or `@` is stored as the text it is, which is harmless in this app (Vue escapes it) and is a spreadsheet formula to any future CSV export that writes it back out unguarded.
The file on the importer's own laptop is outside this system: ADR-014 protects the database, not a spreadsheet of codes in a downloads folder.

---

## ADR-032: A hand edit to a visit is bounded by the server, and a kept visit still accounts for its day

**Decided (visit booking, 2026-11-16).**
Until now no page could book, move or delete a visit, so what the API would accept for those was never exercised.
Two things turned out to be wrong, and both are decided here.
Amends the consequences of ADR-020 and ADR-021.

### 1. What may be edited and deleted, published as `available_actions`

- **A finished visit (complete, cancelled, no access) is not edited in place.**
  PATCH and PUT answer 409 and say to reopen it first.
  Reopening is the deliberate step the state machine already has, and `assign_user` already refuses a finished job the same way.
- **Delete is for a one-off visit that is still `scheduled` and has no recorded time.**
  Anything else answers 409: a visit a plan created ("cancel it instead"), one that is under way or finished, one with a time entry.
- **The job publishes the answer** as `available_actions` (`edit`, `delete`), computed by `scheduling.services.edit_refusal` and `delete_refusal`, which are also what `JobViewSet` enforces with.
  A test walks every status, both sources and worked or not, and asserts the list and the server agree.
  The pages draw Edit and Delete from that list and from nothing else (ADR-023).

**Why.**
Reproduced before the change: DELETE on a cancelled plan visit answered 204, and the next materialization recreated it as `scheduled`, which is the exact failure ADR-021 exists to prevent.
DELETE on any plan visit only hid it until the nightly run.
A complete visit on an issued invoice could be handed to another customer, repriced, moved a month, or deleted.

**Rejected -- leaving the API permissive and hiding the buttons.**
Hiding a button is not a permission, and the nightly materializer does not read the UI.

**Rejected -- deciding it in the client from `status`, `plan` and the role.**
Whether a visit may be deleted turns on recorded time and on whether a plan created it.
A client copy is correct until the rule changes and nothing in the UI fails.

**Rejected -- letting a finished visit's notes or price still be edited.**
Harmless on their own, but it makes "finished" mean two things, and the price of a completed visit is adjusted on the draft invoice line, which is where ADR-026 puts it.

**Rejected -- allowing delete on a plan visit whose plan is paused or gone.**
It would not come back, so it is safe, but the rule would then read the plan's state rather than the job's.
Cancelling works in every case and keeps the reason.

**Accepted cost:** a visit marked in progress by mistake cannot be deleted directly.
It is finished, reopened, then deleted.

### 2. `regenerate_plan` re-keys a kept visit to the same local day

ADR-020 keeps a visit that was moved, cancelled or worked, and finds it again by `plan_occurrence`, an instant.
Change the plan's time and every occurrence is a different instant, so the kept visit no longer accounted for its day and the rebuild put a new visit beside it.

Reproduced through the API before the change: move next Tuesday's visit to Wednesday, edit the plan from 09:00 to 14:00, and Tuesday 14:00 appears as well.
Cancel a visit, make the same edit, and it comes back as `scheduled`.
The nightly run repeated it the next morning.

`regenerate_plan` now runs `_rekey_kept_jobs` between the drop and the rebuild: each live visit of the plan planned for today or later is handed the occurrence the new definition has on the organization-local day it was planned for.
It reaches back to the start of today so this morning's finished visit still accounts for today.
So `plan_occurrence` is no longer strictly immutable: no caller can write it, and this is the one thing that changes it.

**Rejected -- keying the materializer on the local date instead of the instant.**
It is the cleaner model for a rule that fires once a day, but it changes the unique constraint and the idempotency key under every existing row, and an RRULE may legitimately fire more than once a day.

**Rejected -- matching a kept visit to the nearest new occurrence when its day is gone.**
Tuesdays becoming Thursdays has no honest mapping: the moved Tuesday might be this week's visit or an extra one.
Such a visit keeps its old key, the new series is built beside it, and `kept` in the response is what tells the dispatcher to look.

**Accepted cost:** after a rule change that moves the day, a kept visit and the new series can both stand in one week.
That was already true and is now the only case left.

### 3. A plan visit is stamped with what the plan wrote, and "untouched" reads the stamp

Amends ADR-020, whose own text is left as it was.
ADR-020 read "untouched" off the start alone, so a price set by hand, a longer slot or a note on a plan visit was thrown away by the next plan edit.
A lost hand-set price becomes a wrong invoice.

`materialize_plan` now stamps each visit it creates with `planned_price_cents`, `planned_end` and `planned_notes`: the price, the end and the note it wrote.
A future plan visit is untouched when it is still `scheduled`, `scheduled_start == plan_occurrence`, has no time entries, **and** its price, end and note each equal their stamp.
The comparison is three `F()` equalities in the same query, so a NULL stamp matches nothing and that visit is kept: not knowing what the plan wrote is not evidence that nobody changed it.

It is still derived from state, which is why ADR-020 rejected a "detached" flag.
No write path has to remember anything: only the materializer writes the stamp, no serializer accepts it, and the admin shows it read-only.
An edit that is taken back makes the visit the plan's again, and a PATCH that changes nothing pins nothing.

**Three columns, not one snapshot column.**
A JSON snapshot would be one field to add and none to keep whole.
But the rule has to run in SQL, over eight weeks of visits, in the query that picks what to drop, and "does this timestamp equal that JSON string" is not a comparison to trust.
Three typed columns compare with `=`, and a check constraint (`job_plan_stamp_is_whole`) keeps them all set or all NULL.
`planned_notes` is the one nullable text column in the project, on purpose: NULL is "no plan wrote this visit" and `""` is "the plan wrote it with no note".

**The backfill** (`scheduling/migrations/0003_plan_stamps.py`) is one UPDATE: every row with a `plan_occurrence`, soft-deleted rows included, is stamped with its own current price, end and note.
That is correct for every visit made before this branch, because no page could edit one.
The one case it gets wrong is a plan visit PATCHed through the API before the migration ran: its hand-set values become its stamp, so it reads as untouched and the next plan edit rebuilds it.
That is the unsafe direction, and it is exactly what happened to such a visit before the stamp existed, so nothing gets worse.

**With the re-key (part 2).**
A visit kept for its price is still sitting at its planned instant.
When the plan's time moves it keeps its old time, is re-keyed to the new occurrence on its day, and nothing is built beside it; a cancelled one does not come back.
The re-key leaves the stamp alone: it records what the plan wrote when it made the visit, not what it would write now.
So a kept visit later moved by hand onto the plan's new time still differs from its stamp in its end, and stays kept.

**Rejected -- comparing against a re-quote and the plan's current note.**
This is the obvious rule, and a probe through the API settled it.
Raise the service's price, edit the plan's note (neither regenerates anything, both by design), let a later nightly run extend the horizon: one plan's untouched visits then carried prices `{15000: 56, 20000: 7}` and two different notes.
Every one was exactly what the plan wrote on the day it wrote it.
A re-quote calls the 56 older ones hand-edited and pins them, and plan edits stop regenerating anything.
`TestNothingIsPinnedThatNobodyTouched` is that probe as a test, across an edit to the rule, time, duration, service, location and price override.

**Rejected -- `updated_at` later than `created_at` as the signal.**
No migration, and it catches every edit.
But an empty PATCH bumps `updated_at` (verified), so a save that changed nothing pins the visit; an edit that is taken back stays pinned; `bulk_create` already leaves the two a few microseconds apart, so it needs an epsilon; and any future bulk `.save()` over jobs would pin every visit silently.

**Rejected -- making it true by construction.**
Regenerate on every plan-note edit and re-price future plan visits on every service or location change, so the plan and the service could be compared against after all.
That reverses a tested decision about notes, has `catalog` reach into `scheduling`, and changes what "the price is a snapshot" means for a visit already booked.

**Rejected -- the duration alone.**
It is the one field that was derivable without a stamp, since a plan's duration only changes through an edit that regenerates.
It leaves the price out, which is the case that costs money.

**Accepted cost, unchanged from ADR-020:** a hand-added assignee does not pin a visit.
The crew is not stamped, and `default_assignees` has the same history problem the note has.
A test pins it so it stays a decision.

### Left open, on purpose

- **There is no double-booking rule.**
  The same cleaner can be put on overlapping visits by create, assign or a move, and nothing warns.
  Tests pin that too.
- **A visit on a live invoice could still be reopened**, and a reopened visit edited or deleted.
  Closing that needed `scheduling` to ask `billing` a question, and the dependency runs the other way by design.
  Closed by ADR-033.

---

## ADR-033: An app that depends on scheduling holds a visit by registering a check, not by being imported

**Decided (2026-11-16).**
A visit that a live invoice line points at is not reopened, edited or deleted.
The rule is billing's.
The status action, the update and the delete are scheduling's, and `scheduling` may not import `billing`: the dependency runs one way, and CLAUDE.md names the cycle as the thing to avoid.

**The mechanism.**
`scheduling.services.register_job_claim_check(check)` takes a function from a batch of jobs to `{job id: sentence}` for the ones the caller has a claim on.
`BillingConfig.ready()` registers `billing.services.visit_claims`.
Scheduling asks whoever has registered and never learns who they are:

- `next_statuses` offers a claimed finished visit nothing, and `transition_job` refuses the reopen with a 409 whose `detail` is the claimant's sentence and whose `allowed` list is empty;
- `edit_refusal` and `delete_refusal` return the claim first, so `available_actions` and the 409s from PATCH and DELETE agree (ADR-032);
- the job list asks once for the page, through `JobListSerializer`, never once per row, and the ordinary forward moves do not ask at all.

With nothing registered, nothing is ever claimed, and a test pins that scheduling then behaves exactly as it did before the hook.
Another test fails if any module under `scheduling/` imports `billing`.

**Why.**
Reproduced through the API before the change: issue an invoice over a complete visit, reopen it through the status action, delete it.
Each step answered 2xx and the invoice was left billing a visit that no longer existed.

**What counts as a claim** is `billing.services.live_lines()` and nothing looser.
A removed line, a deleted draft and a voided invoice all let the visit go, because voiding is how a mistaken invoice gives its visits back.
The line's organization must be the job's own, stated in the check as well as in `TenantModel.save()`.

**A draft holds a visit too.**
A draft is not a sent document, so this was a real choice.
But nothing at issue checks that a draft's visits are still finished: reproduced, a visit reopened while on a draft was issued as INV-0001 with the visit `scheduled`, billing work nobody had done.
The reopened visit also stayed off the ready-to-invoice list once it was completed again, because the draft's line still claimed it.
The remedy for the dispatcher is one click (remove the line, or delete the draft), so the draft refuses, and says "take it off the draft first" where an issued invoice says "void the invoice first".

**Rejected -- `scheduling` importing `billing`, even lazily inside a function.**
It works on the day it is written.
It closes the cycle that keeps scheduling testable and deployable without billing, and a lazy import hides that from everything except the test this ADR adds.

**Rejected -- reading the reverse relation (`job.invoice_lines`) from scheduling.**
No import, same coupling: scheduling would restate "not soft-deleted, invoice not soft-deleted, invoice not void", which is `live_lines()` and is billing's to change.

**Rejected -- a stored marker on the job (`is_billed`, or an invoice id).**
It is a second copy of a fact billing already derives, and every billing path would have to keep it right: draft, add a line, remove a line, delete a draft, void.
ADR-025 refuses to store "paid" for exactly that reason.
A marker that goes stale either locks a free visit or frees a billed one, and neither fails loudly.

**Rejected -- a Django signal (`pre_save` on `Job`, or a custom "about to reopen" signal).**
A signal can veto by raising, but it cannot answer a question: `next_statuses` and `available_actions` have to *publish* whether a reopen will be accepted, for a page of jobs at once, before anyone tries it.
A signal fires per instance at write time, which gives a refusal with no way to stop offering the button, and one query per row if it were bent into answering.
`pre_save` also fires for every save of a job, including the ones that are fine.

**Rejected -- refusing in billing's own endpoints instead.**
Billing has no endpoint in the path: the reopen is a scheduling request from start to finish.

**Accepted cost:** load order matters.
The check is registered in `ready()`, so code that runs scheduling without Django's app registry populated (there is none today) would see no claims.
And the registry is process-global state, which tests must patch rather than append to; `test_claim_guard.py` shows the pattern.

**Other paths to a billed visit, checked and left alone.**
Clock-in and assign already answer 409 on any finished visit.
Clock-out of an entry that was left open, unassign, a dispatcher's time-entry correction and a new job note all still succeed on a billed visit.
None changes what the invoice bills (the line amount is a snapshot, ADR-026), so none is this bug.
The Django admin edits a job directly and passes through none of this, as it passes through none of the state machine.

---

## ADR-034: An owner runs their own team: invitations, the team rules, and staff password reset

**Decided (2026-11-17, before the first work trial).**
`MembershipViewSet` was read-only, so every new cleaner and every lost password meant the developer in the Django admin.
Five decisions, taken together because each one closes a hole the next would otherwise open.

**1. An invitation is an inactive membership, and only acceptance activates it.**
`invite` creates the `Membership` with `is_active=False` and `invited_at` set; following the emailed link sets `is_active=True` and `accepted_at`.
Everything that decides access already reads `is_active`: tenant resolution in the middleware, the 2FA policy, the magic-link rule, the assignee checks.
So an invitation nobody has accepted changes nothing about how its recipient signs in or what they reach, in this organization or any other.
That matters because an invited address may already be somebody: counted as a membership, a staff invitation would make a rival's customer "not only a customer" and take their magic link away, so any organization's admin could lock another organization's customers out by inviting them.
For the same reason the invited account is never written to (no name, no password), its name is not shown to the inviter until it joins, the response is identical for a known and an unknown address, and reactivating a withdrawn invitation re-invites instead of granting access.
`Membership.state` (invited, active, deactivated) is derived, never stored; `deactivated_at` is the one new column, and it exists to tell a withdrawn invitation from a pending one.
Memberships made by the seed or in the admin have `invited_at` and `accepted_at` NULL and read as active: nothing gates sign-in on either column, and `two_factor/tests/test_lockout_guard.py` pins that.

**2. The team rules are one function, and each row publishes its answer.**
An owner manages anyone; an admin manages dispatchers and cleaners and cannot invite, promote to, demote, deactivate, reactivate or re-invite an owner or admin; nobody below admin manages anybody.
Nobody changes their own role or deactivates themselves.
The last active owner (one who can actually sign in) keeps both the role and the membership, checked under a row lock on every owner in the organization so two owners cannot remove each other at once.
Customers are neither invited nor managed here.
`users.services._refusal` is the single statement of all of it: every write enforces it, and every row is serialized with the `available_actions` and `assignable_roles` it yields for the caller, with the invitable roles on their own endpoint.
The team page draws its buttons from those and keeps no table of which role outranks which (ADR-023).
A refusal for rank is a 403; a refusal for the row's current state (already deactivated, nothing to resend, last owner) is a 409, so the page knows to re-read.

**3. `AccountToken` is a sibling of `MagicLinkToken`, not a generalisation of it.**
It is built the same way: 256 random bits, only the SHA-256 stored, an expiry, single use by a conditional update.
It carries a `purpose` (invitation, 7 days; password reset, 1 hour) that is a required argument of every lookup, so a token is only ever found by the endpoint it was issued for.
An invitation also names the one membership it opens, enforced by a check constraint.
Issuing withdraws whatever was live for the same thing, so a resend kills the earlier email and so does asking for a reset twice; deactivating an invitation withdraws its token.
Both flows look the token up, validate the password, and only then spend it, so a password the validators refuse does not cost the person their link.

**4. Accepting and resetting set a password and sign nobody in.**
Both are public endpoints that answer with a sentence.
The person then signs in the ordinary way and meets the ordinary challenge (ADR-008), on every device, so neither an intercepted invitation nor a reset is also a session, and no bypass of the second factor was added anywhere.
Accepting sets the password and the name only for an account that has never had a password; an existing account keeps both, whatever the request says.
A reset leaves nothing from before it working: every session ends with the password (Django's session auth hash), every trusted device is forgotten, every outstanding 2FA code is withdrawn, and a sign-in that was waiting on one of those codes is over, including through `/api/auth/resend/`.
Reset is for active staff only: a customer signs in by magic link, an invitee has not joined, and a platform superuser changes their password from a shell.

**5. Asking for a reset says the same thing to everyone, and does the same work.**
The endpoint answers 202 with one body whether the address is staff, a customer or nobody.
The view does not look the account up: it hands the address to a Celery task (`two_factor.send_password_reset`), which decides and sends.
A lookup in the request would cost a staff address a token and an SMTP conversation and any other address nothing, and the response time alone would tell them apart.
It is throttled per client address and per address asked about; the second counts unknown addresses the same way, so a 429 says nothing either.
That task takes no `organization_id`, which is a recorded exception to invariant 3: a reset belongs to a person and happens before any tenant is resolved, as a 2FA code does, so there is no organization to pass and no tenant data for a missing one to leak.
The link is created in the worker and goes only into the email; the broker carries the typed address and no result is stored.
With `LOCAL = True`, and only then, the invitation and reset links come back in the response as `dev_link`, as `dev_code` does, and nothing is emailed.

**Rejected -- adding a `purpose` column to `MagicLinkToken`.**
One table and one model, which is why it looks like the obvious thing.
A magic link signs its holder in and these two never do, so sharing a table leaves "which kind is this" to a filter that somebody can forget, and that failure is a staff session from a link meant for something else: it fails open.
With a sibling, an invitation simply is not in the table the magic-link endpoint reads.
It would also have rewritten the customer's sign-in path, in the same change that must not alter anyone's sign-in.

**Rejected -- an invitation as an active membership with `accepted_at` NULL.**
Simpler, and it is what the two existing columns suggest.
Every reader of `is_active` (six call sites, and every one written later) would then have to remember to exclude the unaccepted, and forgetting is the cross-tenant lockout described above.
Inactive-until-accepted fails closed.

**Rejected -- a role table in the frontend.**
The page could work out its buttons from the caller's role and the row's.
It would be right until the day a rule changed on the server and nothing in the UI failed, and it cannot know the last-owner rule at all without counting owners (ADR-023).

**Rejected -- signing the person in on accept or on reset.**
One step fewer.
It would make a link in a mailbox equivalent to password plus code for an owner, which is the trade ADR-008 refuses for staff, and it would let a reset walk around the trusted-device rule.

**Rejected -- sending the reset email from the request, as the magic link does.**
It needs no task and no exception to invariant 3.
It is also a timing oracle for which addresses are staff, on an endpoint whose whole design is to not be one.
The magic-link endpoint has the same shape today and is left alone here; closing it the same way is a separate change.

**Rejected -- an admin typing the invitee's name.**
The inviter usually knows it.
But the address may already belong to an account in another organization, and then either the typed name is silently dropped or one tenant renames another tenant's person; and a name typed for a new account by whoever invites first would follow that person into every organization that invites them later.
The invitee names themselves when they accept.

**Rejected -- letting a superuser reset by email.**
A superuser can act in every organization.
That password is changed with `manage.py changepassword`, by someone with a shell, not by whoever can read a mailbox.

**Rejected -- refusing invitations and resets while an organization is read-only.**
ADR-028 makes a lapsed organization read-only, and "accept an invitation" and "set a new password" are writes.
But they are not writes to the organization: they are how a person gets in, and the person who most needs to get in to a lapsed organization is the owner, to pay.
Refusing them would turn read-only into locked, which is exactly what ADR-028 avoided for the subscription endpoints.
They need no `allowed_while_inactive` flag, because they never reach `IsOrgMember`: they are `AllowAny` with no organization resolved.

**Accepted costs.**
The second factor is a code sent to the same mailbox the reset link goes to, so with reset by email the mailbox alone is enough to take a staff account: before this, it took the mailbox and the password.
That is inherent in a self-service reset while the code is emailed, and an authenticator app or passkey is what would change it.
The invitation and reset tokens travel in the path of the page's address (`/accept-invite/<token>`, `/reset-password/<token>`), as the magic link's and the pay page's do, so a deployed server writes them to its access log before they are spent; a logged token still yields no session without the mailbox, and moving the token to the URL fragment would take it out of the log for all four links at once.
Reset emails depend on the Celery worker: with the worker down the request still answers 202 and the email waits in the queue.
Deactivating a membership does not end the person's session, since they may belong elsewhere; the middleware stops resolving the organization on their next request, and their trusted device, if any, is still trusted if they are reactivated.
A deactivated cleaner's future job assignments are left as they are.

**Consequences.**
The reset views live in `two_factor`, beside the sign-in they recover, because finishing a reset has to reach that app's devices and codes; `users` still imports nothing from `two_factor`.
`SessionSerializer` lists only active memberships, with the same filter as `TenantMiddleware`, so a deactivated membership or an unaccepted invitation is never offered as an organization to act in; before this the payload listed a deactivated one.
A person active in no organization is signed in with no role, and the frontend sends them to one page that says so.
In a read-only organization (ADR-028) the team's five writes are refused by `IsOrgMember` like any other write, and the rule function carries the same condition and the same superuser exemption so that no row offers an action that would be refused: `available_actions`, `assignable_roles` and the invitable roles are all empty there.
A membership's `state` and the organization's billing state share the words "active" and "deactivated"; they are different fields with different schema names (`MembershipStateEnum`, `BillingStateEnum`), and the team page reads only the first.
Five throttle scopes were added and must be listed in all three settings files.
The frontend paths `/accept-invite/<token>` and `/reset-password/<token>` are spelled in `users/services.py`, `two_factor/services.py` and `ui/src/router/index.ts`.
The `TeamAction` values are mirrored as a type and a label table in `ui/` (`src/api/types.ts`, `src/lib/team.ts`), the way invoice actions are.
