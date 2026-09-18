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

```bash
fly secrets set --app pink-glove-staging \
    SECRET_KEY="$(app/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    FIELD_ENCRYPTION_KEY="$(app/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
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
  Staging and production must have *different* keys.
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
group. Confirm there is exactly one `beat`:

```bash
fly status --app pink-glove-staging
```

If there is more than one machine in any group, `fly scale count
web=1 worker=1 beat=1 --app pink-glove-staging`.

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
one proxy deep), with MinIO standing in for the bucket.

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
- `curl -kI https://localhost/assets/<any file in ui/dist/assets>` →
  `cache-control: max-age=315360000, public, immutable`
- `curl -k https://localhost/api/no-such-thing/` → Django's 404, not the page
- `curl -I http://localhost/` → `301` to https
- open `https://localhost/`, accept Caddy's self-signed certificate, and
  sign in as one of the seeded users. Mail is printed rather than sent here;
  the sign-in code is sent from the request, so it is in the web log:
  `docker compose -f deploy/docker-compose.rehearsal.yml logs web`
  (an emailed invoice, sent by Celery, shows up under `logs worker`).

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
| A machine stuck unhealthy | `fly machines restart <id>`. Readiness (`/health/ready/`) failing means Postgres or Redis is unreachable from it; liveness (`/health/live/`) failing means the process is wedged. |
