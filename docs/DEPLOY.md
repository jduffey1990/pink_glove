# Deploying pink_glove

Target: **Fly.io**, one image, three process groups (ADR-027). Two apps,
`pink-glove-staging` and `pink-glove`, from the same `deploy/fly.toml`.
Production is Jordan's alone: every command in this file that touches a live
app is run by Jordan, from Jordan's machine or by the CI job Jordan enables.
Agents write and rehearse; they do not deploy.

Rehearse first: [Local rehearsal](#local-rehearsal) runs the exact image under
production settings behind one TLS proxy, with S3-compatible media, on your
machine. Everything that fails on a first deploy fails there instead.

---

## What the deployed shape is

```
browser ──https──▶ Fly proxy ──http──▶ web (gunicorn)  ─┐
                    (1 proxy)          serves /api/, /admin/,     │ Postgres (Fly)
                                       and the built SPA          │ Redis (Upstash via Fly)
                                       worker (celery)  ─────────┤ Tigris bucket (private)
                                       beat   (celery, ×1)  ─────┘
```

- **One origin.** The frontend build is baked into the image and served by
  gunicorn through whitenoise, so there is no CORS, no `SameSite=None`, and no
  second host to keep in step. `CORS_ALLOWED_ORIGINS` stays unset.
- **Migrations run as the release command**, on a throwaway machine, before
  any process group is replaced. A failed migration leaves the old release
  serving.
- **Exactly one `beat` machine.** Two schedulers fire every job twice.
- **Media is private.** Photographs go to a Tigris bucket with no public
  access; the API hands out URLs that expire after `MEDIA_URL_TTL_SECONDS`.
  Filesystem media is development only: production refuses it, since nothing
  serves `/media/` outside `DEBUG` and the machine's disk is ephemeral.
- **`NUM_PROXIES=1`.** Fly's proxy appends the client address to
  `X-Forwarded-For` and the sign-in throttles key on it. If a CDN is ever put
  in front of Fly, this becomes `2`, in `deploy/fly.toml`.

---

## Stand up staging (once)

You need `flyctl` (`brew install flyctl`) signed in (`fly auth login`).
Run from the repository root. Names below are suggestions; if you change one,
change it everywhere it appears in this section. The region is `dfw`
(Dallas): central US, and one that supports Managed Postgres. Pick another
from `fly platform regions` if the customers are elsewhere -- but pick it
once, because a Postgres volume cannot move regions afterwards -- and put the
same code in `primary_region` in `deploy/fly.toml`.

### 1. Create the app, without deploying

```bash
fly apps create pink-glove-staging
```

### 2. Postgres

```bash
fly postgres create --name pink-glove-staging-db --region dfw \
    --vm-size shared-cpu-1x --volume-size 3 --initial-cluster-size 1
fly postgres attach pink-glove-staging-db --app pink-glove-staging
```

`attach` sets `DATABASE_URL` on the app as a secret. Note the superuser
password it prints once; it is not shown again.

For **production**, prefer Fly's Managed Postgres (`fly mpg create`) or a
cluster of size 2 with daily snapshots — an unmanaged single node has no
failover and you own its backups.

### 3. Redis

```bash
fly redis create --name pink-glove-staging-redis --region dfw --no-replicas
```

Copy the `redis://…` URL it prints (it is redacted in some terminals;
`fly redis status pink-glove-staging-redis` shows it again) and set it:

```bash
fly secrets set --app pink-glove-staging REDIS_URL='redis://default:…@fly-pink-glove-staging-redis.upstash.io:6379'
```

Redis holds the throttle counters and the Celery broker. It is required.

### 4. Private media bucket

```bash
fly storage create --name pink-glove-staging-media --app pink-glove-staging
```

This creates a Tigris bucket and sets `BUCKET_NAME`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3` and `AWS_REGION` as secrets on
the app. Settings read exactly those names. The bucket must stay private;
objects are written private and read through signed URLs regardless, but a
public bucket would still be a public bucket.

### 5. The rest of the secrets

`fly secrets set` never echoes what it stores and `fly secrets list` shows
only digests, so generate the encryption key where you can see it, put it in
your password manager, and only then set it:

```bash
KEY="$(app/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
echo "$KEY"        # store this now; there is no way to read it back later
fly secrets set --app pink-glove-staging \
    SECRET_KEY="$(app/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    FIELD_ENCRYPTION_KEY="$KEY" \
    ALLOWED_HOSTS=pink-glove-staging.fly.dev \
    FRONTEND_BASE_URL=https://pink-glove-staging.fly.dev \
    EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend \
    DEFAULT_FROM_EMAIL='pink glove <noreply@pink-glove-staging.fly.dev>' \
    SECURE_HSTS_SECONDS=0
```

(The venv's Python, because the Fernet generator needs the `cryptography`
package.) `EMAIL_BACKEND=…console…` prints mail to the web and worker logs
instead of sending it, so staging works before there is a mail account: the
sign-in code is in `fly logs --app pink-glove-staging`. To send real mail,
verify a single sender in SendGrid (any mailbox, no domain required), then
`fly secrets set EMAIL_HOST_PASSWORD='SG.…' DEFAULT_FROM_EMAIL='…'` and
`fly secrets unset EMAIL_BACKEND`.
That is enough for staging.
Production authenticates the whole sending domain instead: [Email](#email).
The app refuses to start with an `example.com` sender, or with the SMTP backend and no `EMAIL_HOST_PASSWORD`, so set both in one `fly secrets set` before unsetting `EMAIL_BACKEND`.

- **Back up `FIELD_ENCRYPTION_KEY` somewhere that is not Fly.** It encrypts
  gate and alarm codes. Lose it and every code is unreadable, permanently.
  Staging and production must have *different* keys. If it was set without
  being seen, set a new one before the first deploy -- nothing is encrypted
  until then, so overwriting costs nothing.
- `SECURE_HSTS_SECONDS=0` while first bringing a hostname up. Once TLS is
  known good, `fly secrets set SECURE_HSTS_SECONDS=31536000`. A wrong HSTS
  header is cached by every visitor's browser and cannot be withdrawn.
- `EMAIL_HOST_PASSWORD` is a SendGrid API key by default (`EMAIL_HOST`,
  `EMAIL_HOST_USER` in `.env.example`); use a *separate* key for staging so
  it can be revoked alone.
- Upstash Redis on the default plan bills per command, and Celery polls its
  broker continuously. After a day, `fly redis status` shows the count; if it
  is heading past a few million a month, a fixed-price plan is cheaper.

### 6. Deploy

```bash
fly deploy --config deploy/fly.toml --app pink-glove-staging
```

Fly builds the image remotely from the repository root (the Dockerfile builds
the frontend too), runs the migrations as the release command, then starts
`web`, `worker` and `beat`. The first deploy creates one machine per process
group. Confirm all three are there and `web` passes its check:

```bash
fly status --app pink-glove-staging
```

Expect a `started` machine for each of `web`, `worker` and `beat`, and
`web`'s check column showing `passing`. Fly also adds a **stopped standby**
for `beat` (marked `†`); that is fine -- it starts only if the host dies,
so there is still exactly one scheduler running. If `web` shows `critical`,
`fly checks list --app pink-glove-staging` prints what the probe got back
and `fly logs` says why; a deploy that stalls waiting on `web` may stop
before creating `worker`, and the next deploy creates it. If a group has
more than one *running* machine, `fly scale count web=1 worker=1 beat=1
--app pink-glove-staging`.

Look at `worker` and `beat` here after every deploy, not only the first.
`web` is the only group behind a health check, so a worker or beat that dies at start leaves the deploy green while no invoice is emailed and no Stripe event is applied.
The local rehearsal caught exactly that on 2026-11-16: a freshly built image whose Celery processes could not import their broker transport.
The image build now fails on it, and with [error tracking](#error-tracking) configured the crash is reported, but neither replaces looking.

### 7. Verify

```bash
curl -s https://pink-glove-staging.fly.dev/health/ready/
# {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
curl -sI https://pink-glove-staging.fly.dev/ | grep -i -e '^HTTP' -e cache-control
# HTTP/2 200 ... cache-control: no-cache
```

Open `https://pink-glove-staging.fly.dev/` — the sign-in page. Staging may be
seeded:

```bash
fly ssh console --app pink-glove-staging -C "python manage.py seed_demo --force"
```

Then sign in as one of the printed users (staff are challenged; the code
arrives by email, or in `fly logs` with the console mail backend), upload a photo to
a job, and confirm the image URL is on `fly.storage.tigris.dev`, carries a
signature, and stops working after five minutes.

### 8. Let CI deploy `main` to staging

In the GitHub repository settings:

- **Variables → `FLY_STAGING_APP`** = `pink-glove-staging`
- **Secrets → `FLY_API_TOKEN`** = the output of
  `fly tokens create deploy --app pink-glove-staging` (a token scoped to the
  one app, not your account)
- **Environments → `staging`**: create it. Optionally require your review
  before the job runs.

From then on every push to `main` that passes the backend, frontend and image
jobs deploys to staging. Until the variable is set the job is skipped.
Production is **not** deployed by CI.

### 9. Stripe Connect (Phase 4b)

Optional, and the app runs without it: with no `STRIPE_SECRET_KEY` there is
no connect button, no pay link, and the pay and webhook endpoints answer
503. Do this once there is a Stripe account to connect.

1. **A Stripe account for the platform.** Its own account, not one shared
   with another product: Connect is account-wide, and the account's name is
   what tenants see when they onboard. Under an existing login, the account
   switcher's *Create new account* → "in an organization" gives one
   organization (the LLC) with separate accounts under it.

   **Use one sandbox for everything, and only the sandbox.** Stripe has
   both a "test mode" on the account and separate *sandboxes*, and they
   share nothing -- keys, webhooks, connected accounts. The `sk_test_…` in
   Fly, the webhook endpoint, the Stripe CLI login and the dashboard you
   look at must all be the same sandbox; the switcher top-left must say
   *sandbox*, not *Test mode*. (Staging spent an hour with the key in one
   and the endpoint in the other: payments went through, nothing arrived.)

   In that sandbox, enable **Connect** (the Connect page's *Get started*),
   for platforms whose users take payments from their own customers with
   the full Stripe dashboard (Standard). The tenants' accounts are created
   from the app; nothing is set up per tenant here.
2. **Register the Connect webhook**, in the dashboard, in the sandbox:
   Developers → Webhooks → Add endpoint:
   - URL `https://pink-glove-staging.fly.dev/api/billing/stripe/webhook/`
   - **Listen to: events on Connected accounts** -- not "your account".
     This is the one setting that is easy to get wrong and impossible to
     see from the app or the API (the endpoint object does not carry it);
     a platform-account endpoint delivers nothing, with no error anywhere.
     After saving, the endpoint's page header says which it is -- read it.
   - Events: `checkout.session.completed`, `charge.refunded`,
     `charge.dispute.created`, `charge.dispute.closed`, `account.updated`.
     Nothing else is handled; anything else sent is ledgered and ignored.
   - **Reveal** and copy the signing secret (`whsec_…`).
3. **Set the two secrets** (the app refuses to start with the key and not
   the secret):

   ```bash
   fly secrets set --app pink-glove-staging \
       STRIPE_SECRET_KEY='sk_test_…' \
       STRIPE_CONNECT_WEBHOOK_SECRET='whsec_…'
   ```

   `STRIPE_APPLICATION_FEE_PERCENT` stays unset (0) unless a platform fee
   is decided on; it is a percentage, `2.9` not `0.029`.
4. **Try it end to end** as the seeded owner: Billing → Billing settings →
   Connect with Stripe, and complete the test-mode onboarding (Stripe
   accepts made-up details in test mode; use `000-000-0000` for the phone
   and `000 00 0000` for the tax id when asked). Back on the settings page
   it should say connected. Open the seeded overdue invoice, Email it,
   open the pay link from the log (or Copy pay link), and pay with
   `4242 4242 4242 4242`, any future date, any CVC. Within seconds the
   invoice shows paid. The fee beside the payment is usually there at
   once; when Stripe had not yet attached it to the charge, the sweep
   fills it in within five minutes. Refund it from the
   *connected account's* dashboard (test mode) and watch the payment go
   void and the balance come back.
5. **Production** is the same with the account in live mode, the endpoint
   on the production hostname, and its own signing secret. Staging and
   production endpoints have different secrets even on the same Stripe
   account.

**When a payment does not show up.** In order:

1. Did the event happen? On the connected account:
   `stripe events list --limit 5 --stripe-account acct_… --type checkout.session.completed`.
2. Did Stripe deliver it? `fly logs --app … --no-tail | grep stripe/webhook`
   should show `POST … 200`. No line at all with `pending_webhooks: 0` in
   step 1 means no endpoint is subscribed for connected accounts (step 2
   above). A `400` means the `whsec_` on Fly is not this endpoint's.
   Compare the event's `created` with `fly checks list`: an event that
   fired while `web` was critical was delivered into a machine that could
   not answer, and once the sandbox's few retries are spent
   `pending_webhooks` reads 0 with nothing ever logged. Step 4 is the fix.
   (`fly logs --no-tail` is the last hundred lines of *all* machines;
   `--machine <id>` reads one machine's log further back.)
3. Did the worker finish it? `/admin/` → Billing → Stripe events: the row's
   status and error. RECEIVED with nothing in the worker log means the
   worker died under it -- `fly logs … | grep <worker machine id>` will
   show why (the first one was an out-of-memory kill; the worker VM is
   512MB now, `deploy/fly.toml`). A row does not stay RECEIVED for long:
   beat runs `billing.sweep_stripe_events` every five minutes and re-queues
   anything still RECEIVED after ten, so give it that long before
   replaying by hand. One still RECEIVED an hour on is marked FAILED with
   "needs a person" -- re-queuing did not help, so read the worker log
   for what kills it before step 4.
4. **Replay** by resending from Stripe; a resent event whose row is still
   RECEIVED or FAILED is re-queued, so this is the replay button:
   ```bash
   stripe events resend evt_… --account acct_<connected> --webhook-endpoint we_…
   ```
   (`--account`, not `--stripe-account`: the endpoint is the platform's.)
   Or from the dashboard: the event → Resend. Never pay again instead --
   the second charge is real and the invoice ends up overpaid.

   A row that is **FAILED** needs nothing from Stripe: the payload is in the ledger.
   Sign in to the app as a superuser (the admin only admits a session that cleared the second factor), open `/admin/` → Billing → Stripe events, filter by status *Failed*, read the error, fix the cause, then tick the rows and run **Re-queue the selected FAILED events**.
   Each row stays FAILED until a worker has applied it, so reload after a few seconds: it reads PROCESSED, or FAILED with the new error.
   Running the action twice is harmless, rows in any other status are refused and counted on the page, and each replay is recorded against the person who ran it.
   A RECEIVED row is not replayed this way: the sweep already chases it, and a resend from Stripe (above) is the manual route.

**When a refund does not show up.** The same four steps with
`--type charge.refunded`. One more way it goes wrong: the refund arrives,
the worker marks it PROCESSED in milliseconds, and the invoice does not
move. That is a refund of a payment the ledger never had -- the checkout
event before it was lost (step 3 above) and never replayed -- and the
worker log says so: `no live payment for PaymentIntent pi_…; nothing to
void`. Replay the *checkout* event first (step 4); the payment appears.
The refund event is PROCESSED and a resend of a processed event is
ignored by design, so void that payment in the app by hand, reason
"Refunded in Stripe: $X of $Y" -- the entry the handler would have
written. Nothing on the invoice's lines changes for a refund, ever: lines
are what was billed, and the ledger cannot shrink them (ADR-025); the
payment goes void and the balance comes back.

Locally, the Stripe CLI forwards Connect events to the dev server:

```bash
stripe listen --forward-connect-to localhost:8000/api/billing/stripe/webhook/
# prints a whsec_… for THIS session; put it in app/.env as STRIPE_CONNECT_WEBHOOK_SECRET
```

### 10. The platform subscription (Phase 4c)

Optional on top of step 9, and the app runs without it: with no
`STRIPE_PLATFORM_PRICE_ID` nobody is ever made read-only for billing, the
subscription card is absent and the banner has nothing to say. Every
organization starts on a trial of `PLATFORM_TRIAL_DAYS` (30) with no card,
and goes read-only `PLATFORM_GRACE_DAYS` (14) after its coverage ends
(ADR-028). Do this once there is a price to charge.

1. **The Product and its Price**, in the same sandbox as step 9: Product
   catalog → Add product → one recurring monthly price. Copy the `price_…`
   id from the price's page. The amount lives here and nowhere in the code;
   change it by making a new price and setting the new id.
2. **Register the platform webhook**: Developers → Webhooks → Add endpoint,
   a *second* endpoint beside step 9's:
   - URL `https://pink-glove-staging.fly.dev/api/billing/stripe/platform-webhook/`
   - **Listen to: events on your account** -- step 9's is on connected
     accounts, this one is not. The header on each endpoint's page says
     which; read both. (An endpoint set the wrong way round ledgers the
     other's events as ignored; the right endpoint's own delivery still
     takes the row over, so nothing is lost, but the admin fills with
     IGNORED rows.)
   - Events: `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `invoice.paid`, `invoice.payment_failed`. Anything else is ledgered and
     ignored.
   - **Reveal** and copy the signing secret (`whsec_…`). It is a different
     secret from step 9's.
3. **Configure the Customer Portal once**: Settings → Billing → Customer
   portal: allow customers to update payment methods, view invoice
   history, and cancel subscriptions *at the end of the billing period*;
   do not offer plan changes (there is one plan). Without this the Manage
   billing button answers with Stripe's "portal not configured" as a 503.
4. **Set the retry behaviour**: Settings → Billing → Subscriptions and
   emails → *Manage failed payments*: keep Smart Retries, and when all
   retries fail, **cancel the subscription**. The app's grace is
   independent of Stripe's retries, and `unpaid` is a status with no end.
5. **Set the two values** (the app refuses to start with the price and not
   the secret):

   ```bash
   fly secrets set --app pink-glove-staging \
       STRIPE_PLATFORM_PRICE_ID='price_…' \
       STRIPE_PLATFORM_WEBHOOK_SECRET='whsec_…'
   ```

   `PLATFORM_TRIAL_DAYS` and `PLATFORM_GRACE_DAYS` stay unset for the
   defaults. Every organization that existed before this deploy got a trial
   from the moment of the migration, so nobody wakes up read-only.
6. **Try it end to end** as the seeded Rival Cleaners owner (the seed puts
   that organization five days from the end of its trial, and Sparkle Clean
   on a complimentary plan that shows nothing): the banner says when the
   trial ends; Billing → Billing settings → Subscribe, pay with
   `4242 4242 4242 4242`; back on the settings page it says Subscribed with
   no banner. Manage billing opens the portal; cancel there and the banner
   says "ends on" within seconds. To see a failed renewal, subscribe with
   `4000 0000 0000 0341` instead (it attaches, then fails every charge) and
   advance the sandbox's test clock past the renewal: the banner turns
   warning with the grace date. To see read-only, move `trial_ends_at` and
   `subscription_paid_through` back a month in `/admin/` and run the daily
   task by hand from a shell (`fly ssh console --app … -C "python manage.py
   shell -c 'from billing.tasks import enforce_platform_billing_all as t;
   t()'"`): the banner turns red, the schedule still loads, a job cannot be
   edited, Subscribe still works and the organization comes back.
7. **Production** is the same with the live-mode price and endpoint and
   their own secret.

**When the banner is wrong.** The banner is the organization row's own
fields, read by the rule in `organizations/services.py`; the fields are
what the last re-read of the Subscription said. In order:

1. What does Stripe say? The customer's page in the dashboard (search the
   organization's name; `stripe_customer_id` is in `/admin/`), its
   subscription's status and current period, and whether the latest
   invoice is paid.
2. Did the event arrive? `fly logs --app … --no-tail | grep platform-webhook`
   should show `POST … 200`; nothing at all means the endpoint in step 2
   is not subscribed to that type or is listening to connected accounts; a
   `400` means the secret on Fly is step 9's, not this one's.
3. Did the worker apply it? `/admin/` → Billing → Stripe events, filter on
   an empty account: the row's status and error. The sweep re-queues a row
   still RECEIVED after ten minutes, as for Connect.
4. Whatever happened, the daily task at 01:00 UTC re-reads every
   subscription and applies the rule. To not wait: run it as in step 6, or
   return to the settings page with `?subscription=return` in the URL
   (admin and above), which re-reads that one organization. An
   organization read-only with `deactivated_for_billing_at` set comes back
   the moment a re-read finds it covered; one with that field empty was
   switched off by hand and only the admin switches it on.

Locally, the Stripe CLI forwards both endpoints with one command:

```bash
stripe listen \
  --forward-to localhost:8000/api/billing/stripe/platform-webhook/ \
  --forward-connect-to localhost:8000/api/billing/stripe/webhook/
# prints ONE whsec_… for both; put it in app/.env as both
# STRIPE_CONNECT_WEBHOOK_SECRET and STRIPE_PLATFORM_WEBHOOK_SECRET
```

---

## Production

The same steps with `pink-glove` in place of `pink-glove-staging`, and:

- your own hostname, **on its own subdomain** (`app.yourdomain`, not the
  apex): `fly certs add app.yourdomain --app pink-glove`, then
  `ALLOWED_HOSTS` and `FRONTEND_BASE_URL` (https) to match, then
  `COOKIE_DOMAIN` if the API will ever be reached on more than one hostname
  (leave it blank otherwise). The HSTS header includes subdomains and asks
  for preload by default, which is right for a subdomain of its own; on an
  apex it would commit every subdomain of the company to https, so there set
  `SECURE_HSTS_INCLUDE_SUBDOMAINS=False` and `SECURE_HSTS_PRELOAD=False`;
- a managed or 2-node Postgres with snapshots (step 2);
- a **different** `FIELD_ENCRYPTION_KEY`, `SECRET_KEY` and SendGrid key from
  staging;
- `SECURE_HSTS_SECONDS=31536000` once TLS is confirmed;
- mail from an authenticated domain, checked end to end before anyone is invited: [Email](#email);
- `SENTRY_DSN` and `SENTRY_ENVIRONMENT=production`, so a 500 reaches a person: [Error tracking](#error-tracking);
- a first backup taken and one restore drill passed before real codes and payments go in: [Backup and restore](#backup-and-restore);
- deploy by hand: `fly deploy --config deploy/fly.toml --app pink-glove`.

There is no rollback command that undoes a migration. `fly releases` and
`fly deploy --image <previous>` bring the previous code back; a migration
that must be undone is a new migration.

---

## Email

Sign-in codes and invoices leave by email, through SendGrid's SMTP relay: `EMAIL_HOST=smtp.sendgrid.net`, `EMAIL_HOST_USER=apikey`, and the API key as `EMAIL_HOST_PASSWORD`.
Mail from a domain that has not been authenticated is accepted by SendGrid and then junked or refused by Gmail and Outlook, and nothing in the app can see that happen.
So production sends from a domain you own, with SPF, DKIM and DMARC in place, and step 5 is run before anyone is invited.

The app refuses to start when `DEFAULT_FROM_EMAIL` is not an address or is on `example.com` (the default), and when the SMTP backend is selected with an empty `EMAIL_HOST_PASSWORD`.
Those are the two failures configuration alone can cause; the steps below cover the ones only DNS can.
The `dig` commands and `sendtestemail` were run locally when this was written; the SendGrid screens and the `fly` lines were not, so read what SendGrid shows over what is written here.

### 1. Authenticate the domain in SendGrid (SPF and DKIM)

In SendGrid: Settings → Sender Authentication → Authenticate Your Domain.
Enter the domain mail will come from, the registrable one (`yourdomain`, not `app.yourdomain`), and leave automated security on.
SendGrid then shows three CNAME records to create at your DNS host.
Their shape, with the numbers being yours:

| Type | Host | Points to |
|---|---|---|
| CNAME | `em1234.yourdomain` | `u1234567.wl123.sendgrid.net` |
| CNAME | `s1._domainkey.yourdomain` | `s1.domainkey.u1234567.wl123.sendgrid.net` |
| CNAME | `s2._domainkey.yourdomain` | `s2.domainkey.u1234567.wl123.sendgrid.net` |

Copy the values SendGrid shows, not these.
The first is the return path: SendGrid publishes the SPF record behind it, so SPF passes with no SPF record of your own, and it aligns because `em1234.yourdomain` is a subdomain of the From domain.
Do not add a second `v=spf1` TXT record at the root for SendGrid.
If the root already has one for the company's ordinary mail, leave it as it is.
The other two are the DKIM keys.
If the DNS host is Cloudflare, set all three to "DNS only": a proxied CNAME does not verify.
Back in SendGrid, press Verify.

### 2. DMARC

A TXT record you create yourself; SendGrid does not make it.

| Type | Host | Value |
|---|---|---|
| TXT | `_dmarc.yourdomain` | `v=DMARC1; p=none; rua=mailto:dmarc@yourdomain` |

`p=none` asks receivers to report and not to act, which is where to start.
The reports arrive at the `rua` address daily and show whether anything else is sending as the domain.
After two weeks of reports in which everything that sends as the domain passes, change it to `p=quarantine`.
If the domain already has a `_dmarc` record, keep it: there must be exactly one.

### 3. An API key for the app

Settings → API Keys → Create API Key → Restricted Access, with **Mail Send** set to Full Access and nothing else.
One key per environment, so staging's can be revoked alone.
The key is shown once.

### 4. Set it on the app

```bash
fly secrets set --app pink-glove \
    EMAIL_HOST_PASSWORD='SG.…' \
    DEFAULT_FROM_EMAIL='Your Company <billing@yourdomain>'
```

The address must be on the domain from step 1.
If `fly secrets list --app pink-glove` shows `EMAIL_BACKEND` (staging sets it to the console backend), remove it in a second command, after the one above: `fly secrets unset --app pink-glove EMAIL_BACKEND`.
In the other order the app would be on SMTP with no password for one release, and it refuses to start that way.

### 5. Verify

The DNS, substituting the hosts SendGrid gave you:

```bash
dig +short CNAME s1._domainkey.yourdomain
# s1.domainkey.u1234567.wl123.sendgrid.net.
dig +short CNAME s2._domainkey.yourdomain
# s2.domainkey.u1234567.wl123.sendgrid.net.
dig +short CNAME em1234.yourdomain
# u1234567.wl123.sendgrid.net.
dig +short TXT _dmarc.yourdomain
# "v=DMARC1; p=none; rua=mailto:dmarc@yourdomain"
```

An empty answer means the record is not there yet, or is under the wrong name.
The usual wrong name is the DNS host appending the domain a second time (`s1._domainkey.yourdomain.yourdomain`): enter only `s1._domainkey` if the host's form adds the domain itself.

Then a real message, to a Gmail address you can open:

```bash
fly ssh console --app pink-glove -C "python manage.py sendtestemail you@gmail.com"
```

It prints nothing and exits 0 when the mail server accepted the message.
`SMTPAuthenticationError: (535, …)` means the key is wrong or revoked.
`SMTPDataError: (550, … does not match a verified Sender Identity …)` means `DEFAULT_FROM_EMAIL` is not on the authenticated domain.

In Gmail, open the message, then ⋮ → Show original.
Expected at the top:

```
SPF:    PASS with IP 149.72.…
DKIM:   'PASS' with domain yourdomain
DMARC:  'PASS'
```

All three must say PASS, and the DKIM domain must be yours.
If it reads `sendgrid.net`, step 1 was not verified and DMARC will fail once the policy is tightened.
Last, sign in to the app as the owner: the code must arrive in the inbox, not in spam, within a minute.

---

## Error tracking

Unhandled exceptions in a request or a Celery task, and anything logged at ERROR, go to Sentry (ADR-029).
It is optional and off until `SENTRY_DSN` is set; with no DSN the SDK is not even imported.
Only production settings start it, so development and the test suite cannot report anywhere.

What an event carries, and what is removed before it leaves, is listed in ADR-029 and enforced in `app/app/sentry.py`.
In short: the stack trace with source lines, the request's method, URL and headers, and the release; no request body, no local variables, no cookies, no user, no client address, and no value stored under a name like a gate code, token or password.
An exception message or a log line is sent as written, which is why nothing sensitive may be put in either.

### 1. Create the project

In Sentry, in an organization you own: Projects → Create Project → Django.
One project serves both staging and production; the environment tag tells them apart.
In the project's Settings → Security & Privacy, leave **Data Scrubber** on and turn **Prevent Storing of IP Addresses** on.
The app already removes these; this is the same rule held on the other side.
Copy the DSN from Settings → Client Keys (DSN).

### 2. Set it on the app

```bash
fly secrets set --app pink-glove-staging \
    SENTRY_DSN='https://…@o….ingest.us.sentry.io/…' \
    SENTRY_ENVIRONMENT=staging
```

Production is the same with `--app pink-glove` and `SENTRY_ENVIRONMENT=production`.
The two must be set together: the app refuses to start with a DSN and no environment, because staging and production are the same image and an event filed under the wrong one is an error nobody looks at.
The release is tagged with `FLY_IMAGE_REF`, which Fly sets on every machine and `fly releases --image` lists; set `SENTRY_RELEASE` only to override it.

### 3. Verify

```bash
fly ssh console --app pink-glove-staging
```

and in the shell it opens:

```bash
python manage.py shell -c "import logging; logging.getLogger('ops').error('error tracking check')"
exit
```

It prints one line, `ERROR … ops error tracking check`, and within a minute Sentry shows an issue titled "error tracking check" with environment `staging` and a release beginning `registry.fly.io/pink-glove-staging:`.
Run locally in the production image against a stand-in endpoint, that command sent one envelope holding one error event and nothing else.
No issue means the DSN is wrong or outbound HTTPS is blocked: `fly logs --app pink-glove-staging` shows the SDK's complaint.

### 4. Be told

A new Sentry project emails its owner on every new issue by default (Alerts → the rule named for the project).
Check that rule exists and names an address that is read, and add a second rule for "an issue changes state from resolved to unresolved".
Without a rule, errors are collected and nobody is told, which is where this started.

To turn it off: `fly secrets unset --app … SENTRY_DSN`.

---

## Backup and restore

Three things must survive, and the third is the one that gets forgotten.

| What | Where it lives | Lost means |
|---|---|---|
| The database | Fly Postgres | every customer, visit, invoice and payment |
| The media bucket | Tigris | every job photo, the evidence in a dispute |
| `FIELD_ENCRYPTION_KEY` | a Fly secret, and your password manager | every gate and alarm code, permanently, even with a perfect database backup |

Redis holds nothing durable (throttle counters and the task queue).
`SECRET_KEY` need not survive: a new one signs everyone out and breaks the pay links in invoices already emailed, which are resent.

**Where the key lives.**
On the app as a Fly secret, which `fly secrets list` will not show you, and in your password manager, which is the copy that counts.
Keep one more offline (printed, in the safe with the company's other papers).
The key is never stored with the backups: not in the backup bucket, not in a file beside the dump, not in this repository.
A database backup without the key cannot decrypt a single access code, and a backup stored beside the key protects nothing: whoever takes one has the other.
While the app still exists the key can be read back from a machine with `fly ssh console --app pink-glove -C "printenv FIELD_ENCRYPTION_KEY"`, which is how to repair a missing password-manager entry and is not a backup.

Everything below marked *rehearsed* was run on 2026-11-16 against the local rehearsal stack, with MinIO as the bucket; the output is in `docs/PLAN.md`, "Ops readiness as built".
The `fly` lines were not run by the agent that wrote them (agents do not touch a live app).
Run the whole section once against staging before trusting it with production.

### How often

- **Daily, automatic:** Fly's own volume snapshots of the Postgres machine. Confirm they exist (below) once a month.
- **Weekly, and before every deploy that carries a migration:** a `pg_dump` and a media copy, taken off Fly. A migration cannot be rolled back, so the dump taken just before it is the only way back.
- **Before the trial starts, then quarterly:** the restore drill. A backup that has never been restored is a hope.

These are run by hand; nothing schedules them yet.
With the weekly dump as the only copy off Fly, losing the Fly organization loses up to a week.
Card payments are the exception: Stripe keeps every event for 30 days and the ledger refuses duplicates, so events since the dump can be resent ("When a payment does not show up", step 4).

### Back up the database

`pg_dump` must be at least as new as the server, or it stops with `aborting because of server version mismatch`.
The `pg_dump` on PATH on the development Mac is 14 (`/opt/homebrew/opt/postgresql@14`), which is older than any Postgres Fly creates today.
Install a current one once and use it by path.
The commands below assume 17, the version the rehearsal runs; `psql "$LOCAL_URL" -Atc 'show server_version'`, once `LOCAL_URL` is set, says what the server really is, and the major versions must match or `pg_dump` be the newer.

```bash
brew install postgresql@17
/opt/homebrew/opt/postgresql@17/bin/pg_dump --version
# pg_dump (PostgreSQL) 17.x
```

Terminal 1, a tunnel to the database app, left running:

```bash
fly proxy 15432:5432 --app pink-glove-db
```

Terminal 2:

```bash
# The app's own database URL, kept in a variable and never printed.
DATABASE_URL="$(fly ssh console --app pink-glove -C 'printenv DATABASE_URL' | tr -d '\r')"
# The same credentials and database, through the tunnel.
LOCAL_URL="$(printf '%s' "$DATABASE_URL" | sed -E 's#@[^/]+/#@localhost:15432/#')"

DUMP="pink-glove-$(date -u +%Y%m%dT%H%M%SZ).dump"
/opt/homebrew/opt/postgresql@17/bin/pg_dump "$LOCAL_URL" \
    --format=custom --no-owner --no-acl --file="$DUMP"
/opt/homebrew/opt/postgresql@17/bin/pg_restore --list "$DUMP" | grep -c 'TABLE DATA'
# 40
```

*Rehearsed:* the `pg_dump` and `pg_restore --list` lines, and the `sed` rewrite on a sample URL. Forty tables as of this writing; the number grows with the schema, and zero means the dump is empty.
`--no-owner --no-acl` is what lets the dump restore under a different database user, which a rebuilt database will have.

Then move it off the laptop and off Fly, to storage where the key is not: a private, versioned bucket in your own AWS account, for instance.

```bash
aws s3 cp "$DUMP" "s3://<your-backup-bucket>/pink-glove/db/$DUMP"
rm "$DUMP"
```

The dump holds every customer's name, address and email in the clear.
It holds the access codes as ciphertext only: in the rehearsal, the table's dump contained the Fernet tokens and neither seeded code.

Fly's snapshots, for the monthly check:

```bash
fly volumes list --app pink-glove-db
fly volumes snapshots list <volume id>
```

A snapshot restores a whole new Postgres app (`fly postgres create --snapshot-id <id> …`) and is the fast way back from a dead volume.
It lives in the same Fly organization as the database, so it is not the copy that survives losing the account.
If production is on Managed Postgres, the tunnel is `fly mpg proxy` and the backups are in its dashboard; the `pg_dump` is the same.

### Back up the media

Use a **read-only** Tigris key made for backups, not the app's: `fly storage dashboard pink-glove-media` → Access Keys → new key, read-only, this bucket.
Put it in a named profile so it cannot be confused with your AWS account:

```bash
aws configure --profile pink-glove-media-ro
# AWS Access Key ID: tid_…   AWS Secret Access Key: tsec_…   region: auto   output: (blank)

aws s3 sync s3://pink-glove-media ./pink-glove-media \
    --profile pink-glove-media-ro --endpoint-url https://fly.storage.tigris.dev
aws s3 sync ./pink-glove-media "s3://<your-backup-bucket>/pink-glove/media/"
```

*Rehearsed:* the first `sync`, against MinIO.
No `--delete`, on purpose: the app never overwrites or removes an object, so the copy only ever grows, and a deletion at the source can never propagate into the backup.

### The restore drill

Proves the three things belong together: this dump, this key, these photos.
It restores into the local rehearsal stack, so nothing live is touched.
From the repository root, with the dump and the media copy downloaded:

```bash
docker build -t pink-glove:rehearsal .
docker compose -f deploy/docker-compose.rehearsal.yml up -d

# A fresh, empty database, and the dump restored into it.
docker compose -f deploy/docker-compose.rehearsal.yml exec -T db createdb -U pink_glove drill
docker compose -f deploy/docker-compose.rehearsal.yml exec -T db \
    pg_restore -U pink_glove -d drill --no-owner --no-acl < pink-glove-YYYYMMDDTHHMMSSZ.dump

# The key, from the password manager. Typed, not pasted into a file; nothing echoes.
read -rs KEY

docker compose -f deploy/docker-compose.rehearsal.yml run --rm \
    -e DATABASE_URL=postgres://pink_glove:pink_glove@db:5432/drill \
    -e FIELD_ENCRYPTION_KEY="$KEY" \
    web python manage.py check_field_encryption
```

Expected, with your own counts:

```
customers.ServiceLocation.gate_code: 2 readable, 0 unreadable
customers.ServiceLocation.alarm_code: 2 readable, 0 unreadable
customers.ServiceLocation.key_location: 0 readable, 0 unreadable
OK: all 4 encrypted values are readable with this FIELD_ENCRYPTION_KEY.
```

The command decrypts every encrypted column of every row and prints counts only, never a code.
With the wrong key it exits 1:

```
customers.ServiceLocation.gate_code: 0 readable, 2 unreadable
…
CommandError: 4 of 4 encrypted values cannot be read with this FIELD_ENCRYPTION_KEY. It is not the key this database was written with.
```

That is the result that matters most: it means the key in the password manager is not production's, and it is found on a quiet afternoon.

Then the schema and the photos:

```bash
docker compose -f deploy/docker-compose.rehearsal.yml run --rm \
    -e DATABASE_URL=postgres://pink_glove:pink_glove@db:5432/drill \
    web python manage.py migrate --check
echo $?
# 0: the dump's schema is the one this image expects.
# 1: the dump is older than the image. Expected if a migration has shipped since; not a failure.

find ./pink-glove-media -type f | wc -l
```

The file count should be the number of rows in `scheduling_jobphoto`, which the first command below prints.
Finish, and leave nothing of production on the laptop:

```bash
docker compose -f deploy/docker-compose.rehearsal.yml exec -T db \
    psql -U pink_glove -d drill -Atc 'select count(*) from scheduling_jobphoto'
unset KEY
docker compose -f deploy/docker-compose.rehearsal.yml down -v
rm -r pink-glove-*.dump ./pink-glove-media
```

*Rehearsed:* every `docker compose` line above, and then further than the drill asks: the dump restored into a second, empty Postgres server under a different owner, the media copied into a second bucket, the whole app booted against both, a gate code read back through the app's own reveal over TLS, and the photo fetched through a signed URL with the same SHA-256 as the file uploaded.
The rehearsal's Postgres is 17: a dump from a newer server will not restore into it, so raise the image tag in `deploy/docker-compose.rehearsal.yml` first if production has moved on.

### Restoring production

Not rehearsed against Fly.
The order matters more than the exact flags, so check each command's `--help` as you go.

1. Stop writes: `fly scale count web=0 worker=0 beat=0 --app pink-glove`.
2. A new database: `fly postgres create --name pink-glove-db-2 …` as in "Stand up staging", step 2 (or `--snapshot-id <id>` to start from a Fly snapshot, in which case skip to 5).
3. `fly secrets unset DATABASE_URL --app pink-glove`, then `fly postgres attach pink-glove-db-2 --app pink-glove`, which creates an empty database and user and sets the new `DATABASE_URL`.
4. Restore into it through a tunnel, exactly as the dump was taken: `fly proxy 15432:5432 --app pink-glove-db-2`, build `LOCAL_URL` the same way, then `pg_restore --no-owner --no-acl -d "$LOCAL_URL" pink-glove-….dump`.
5. Media, only if the bucket was lost: `fly storage create`, then `aws s3 sync` the backup into it with the new key.
6. `FIELD_ENCRYPTION_KEY` only if the app itself was recreated: `fly secrets set` it from the password manager. It must be the old key, not a new one.
7. `fly deploy --config deploy/fly.toml --app pink-glove`. The release command runs any migration newer than the dump. Then `fly scale count web=1 worker=1 beat=1 --app pink-glove`.
8. `fly ssh console --app pink-glove -C "python manage.py check_field_encryption"` must end in `OK`. Then sign in, open a job with photos, and reveal one location's codes.
9. Resend the Stripe events since the dump ("When a payment does not show up", step 4).

---

## Local rehearsal

Runs the production image under production settings behind Caddy (TLS,
one proxy deep), with MinIO standing in for the bucket. Every command in
this section runs from the **repository root**; the compose path is
relative to it.

```bash
docker build -t pink-glove:rehearsal .
docker compose -f deploy/docker-compose.rehearsal.yml up -d
docker compose -f deploy/docker-compose.rehearsal.yml run --rm web python manage.py seed_demo --force
```

(`--force` because the seed refuses to run with `DEBUG` off unless told; here
that is the point.)

Then:

- `docker compose -f deploy/docker-compose.rehearsal.yml ps` → `web`, `worker`, `beat`, `proxy`, `db`, `redis` and `minio` all `Up`.
  Read it: `up -d` reports success when `worker` and `beat` start and then exit a second later, and nothing below would notice.
  That is how a freshly built image with a broken Celery got as far as it did on 2026-11-16; `ps -a` and `logs worker` say why.
- `curl -k https://localhost/health/ready/` → both checks `ok`
- `curl -kI https://localhost/schedule` → `200`, `cache-control: no-cache`
- an asset, named from the *image*, not from a local `ui/dist` that may have
  been built from a different tree (a name that is not in the image falls
  through to the SPA page, `text/html` and `no-cache`, which looks like a
  failure and is not one):

  ```bash
  ASSET=$(docker compose -f deploy/docker-compose.rehearsal.yml exec web sh -c 'ls /ui/dist/assets/*.js | head -1 | xargs basename')
  curl -kI "https://localhost/assets/$ASSET"
  # cache-control: max-age=315360000, public, immutable
  ```
- `curl -k https://localhost/api/no-such-thing/` → Django's 404, not the page
- `curl -I http://localhost/` → `301` to https
- open `https://localhost/` and sign in as one of the seeded users. Mail is
  printed rather than sent here; the sign-in code is sent from the request,
  so it is in the web log:
  `docker compose -f deploy/docker-compose.rehearsal.yml logs web`
  (an emailed invoice, sent by Celery, shows up under `logs worker`).
- as that user, open an issued invoice and press Email, then
  `docker compose -f deploy/docker-compose.rehearsal.yml logs worker` →
  the message, and `Task billing.send_invoice_email[…] succeeded`.
  This is the one check here that goes through Redis to the worker and back.
- error tracking is off here (no `SENTRY_DSN`), which is the state to confirm:

  ```bash
  docker compose -f deploy/docker-compose.rehearsal.yml exec -T web \
      python -c "import django, sys; django.setup(); print('sentry_sdk' in sys.modules)"
  # False
  ```

The certificate is signed by Caddy's own internal CA, so the browser will
object. Chrome sometimes shows no "Proceed" link for `localhost`; typing
`thisisunsafe` on the interstitial (no input box appears -- just type it)
gets past it for the session. To stop the warning for good, trust that CA
once in the login keychain, then quit and reopen the browser:

```bash
docker compose -f deploy/docker-compose.rehearsal.yml exec proxy \
    cat /data/caddy/pki/authorities/local/root.crt > /tmp/caddy-root.crt
security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db /tmp/caddy-root.crt
```

The CA lives in the `caddy_data` volume, so it survives `down` but is
regenerated after `down -v`, and the trust has to be redone then. (Staging
and production use real certificates from Fly and none of this applies.)

The MinIO console is at `http://localhost:9001` (`rehearsal` /
`rehearsal-secret`): uploaded photos appear in the `pink-glove-media` bucket
and the bucket has no anonymous policy.

Tear down, including the database volume:

```bash
docker compose -f deploy/docker-compose.rehearsal.yml down -v
```

---

## Rotation and recovery

| What | How |
|---|---|
| `SECRET_KEY` | `fly secrets set SECRET_KEY=…`. Every session is signed out. Nothing else is affected. |
| `FIELD_ENCRYPTION_KEY` | Do **not** simply replace it: existing ciphertext becomes unreadable. Needs a re-encryption migration (read with old, write with new). Not built; open an issue before it is needed. `python manage.py check_field_encryption` says, in counts and without showing a code, whether the key a process holds reads the database it is connected to. |
| The database, the bucket, or the key itself | [Backup and restore](#backup-and-restore). |
| SendGrid key | Create the new key, `fly secrets set EMAIL_HOST_PASSWORD=…`, then delete the old one in SendGrid. [Email](#email), step 5, to check. |
| `SENTRY_DSN` | Sentry → Client Keys: create a new key, `fly secrets set SENTRY_DSN=…`, revoke the old. A leaked DSN lets someone send junk events, not read any. |
| A FAILED Stripe event | `/admin/` → Billing → Stripe events → select → "Re-queue the selected FAILED events" ("When a payment does not show up", step 4). |
| Postgres credentials | `fly postgres users` / `fly secrets set DATABASE_URL=…`; machines restart on the new secret. |
| Tigris credentials | `fly storage update` or the Tigris console; set the new `AWS_*` secrets. Signed URLs already issued stop working. |
| A bad deploy | `fly releases --app …` then `fly deploy --image registry.fly.io/…:<previous tag>`. |
| A machine stuck unhealthy | `fly checks list` first: it shows the probe's actual reply. A `503` from readiness (`/health/ready/`) means Postgres or Redis is unreachable from it; `connection refused` or `context deadline exceeded` means the process is wedged, `fly machines restart <id>`. The tell for a wedge in `fly logs` is probes logged in *bursts* -- eight `/health/ready/` lines with one timestamp is two minutes of queued probes drained at once -- and then nothing. Every request slot is a thread stuck on a read, and gunicorn's gthread worker never reaps one; the readiness check and the cache now carry a read timeout so a dropped Redis connection is a 503, not a dead machine (2026-09-23). A `400` means the app rejected the probe's `Host` header (Fly probes by private IP; `app/middleware/health.py` exempts the two probes and nothing else). |
