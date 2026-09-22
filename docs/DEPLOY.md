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

1. **A Stripe account for the platform**, in test mode for staging. Enable
   **Connect** (Settings → Connect) and choose the option for platforms
   whose connected accounts use the full Stripe dashboard (Standard). The
   tenants' accounts are created from the app; nothing is set up per
   tenant here.
2. **Register the Connect webhook.** Developers → Webhooks → Add endpoint:
   - URL `https://pink-glove-staging.fly.dev/api/billing/stripe/webhook/`
   - **Listen to: events on Connected accounts** -- not "your account".
     This is the one setting that is easy to get wrong and impossible to
     see from the app; a platform-account endpoint delivers nothing.
   - Events: `checkout.session.completed`, `charge.refunded`,
     `charge.dispute.created`, `charge.dispute.closed`, `account.updated`.
     Nothing else is handled; anything else sent is ledgered and ignored.
   - Copy the signing secret (`whsec_…`) it shows once.
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
   invoice shows paid, with the fee beside the payment. Refund it from the
   *connected account's* dashboard (test mode) and watch the payment go
   void and the balance come back.
5. **Production** is the same with the account in live mode, the endpoint
   on the production hostname, and its own signing secret. Staging and
   production endpoints have different secrets even on the same Stripe
   account.

Locally, the Stripe CLI forwards Connect events to the dev server:

```bash
stripe listen --forward-connect-to localhost:8000/api/billing/stripe/webhook/
# prints a whsec_… for THIS session; put it in app/.env as STRIPE_CONNECT_WEBHOOK_SECRET
```

Failed events are in Django admin under Billing → Stripe events, with the
error; fix the cause, then re-run the task
(`process_stripe_event.delay(organization_id, event_row_id)` from a shell)
-- the payload is on the row, so Stripe need not resend.

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
- deploy by hand: `fly deploy --config deploy/fly.toml --app pink-glove`.

There is no rollback command that undoes a migration. `fly releases` and
`fly deploy --image <previous>` bring the previous code back; a migration
that must be undone is a new migration.

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
| `FIELD_ENCRYPTION_KEY` | Do **not** simply replace it: existing ciphertext becomes unreadable. Needs a re-encryption migration (read with old, write with new). Not built; open an issue before it is needed. |
| Postgres credentials | `fly postgres users` / `fly secrets set DATABASE_URL=…`; machines restart on the new secret. |
| Tigris credentials | `fly storage update` or the Tigris console; set the new `AWS_*` secrets. Signed URLs already issued stop working. |
| A bad deploy | `fly releases --app …` then `fly deploy --image registry.fly.io/…:<previous tag>`. |
| A machine stuck unhealthy | `fly checks list` first: it shows the probe's actual reply. A `503` from readiness (`/health/ready/`) means Postgres or Redis is unreachable from it; `connection refused` or a timeout means the process is wedged, `fly machines restart <id>`. A `400` means the app rejected the probe's `Host` header (Fly probes by private IP; `app/middleware/health.py` exempts the two probes and nothing else). |
