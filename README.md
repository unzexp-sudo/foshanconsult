# zhituoyuan booking service

A paid 1-1 booking service for `www.zhituoyuan.com`: a visitor picks a slot from the
owner's Google Calendar, pays ¥500 with WeChat Pay (Native / 扫码), and the booking
becomes real **only after WeChat's signed callback verifies the money arrived**. It is
two deployables — a FastAPI booking app and a small calendar relay — that share one
repository, one SQLAlchemy schema and one test suite. The booking app never holds a
Google credential in production; the relay is the only thing that talks to Google.

Read `docs/MODULE_CONTRACT.md` for the frozen interfaces and `docs/BUILD_PLAN.md` for the
phases, security checklist and the decisions D1–D5 (summarised in
[Decisions](#decisions-d1d5) below).

---

## Two boxes, and why

| Deployable | Where | Why |
|---|---|---|
| `app/` (booking app) | Railway now → **mainland host on the ICP-filed domain** at go-live | WeChat Pay Native on a PC website requires an **ICP-filed domain**, and a filing is only valid with a mainland 接入商. It owns bookings, slots, WeChat Pay and the notify endpoint. |
| `relay/` (calendar relay) | **Railway** | `googleapis.com` is unreachable from mainland servers. The relay holds the Google OAuth credentials and is the only process that talks to Google. |

Outbound `mainland → Railway` is fine; `Railway → Google` is fine; **neither box does
both**. The booking app calls the relay over HTTPS with an HMAC-signed body
(`X-Relay-Timestamp` + `X-Relay-Signature`, contract §9) and never imports anything from
`relay/`; the relay imports nothing from `app/` (contract §14.4).

Consequence for development: the booking app must run with `CALENDAR_GATEWAY=fake` and
`PAYMENT_GATEWAY=fake`, with **zero external services**, for all local dev and tests.

---

## Environment variables

Every field in `app/config.py` and `relay/config.py`. "Required" means the service will
misbehave without it; the defaults are the local-dev values.

> **`WECHAT_NOTIFY_URL` is derived, never configured.** It is
> `{PUBLIC_BASE_URL}/api/payments/wechat/notify` (`app/config.py:70`). Do **not** set it as
> its own variable — set `PUBLIC_BASE_URL` and let the app derive it, then register the
> derived URL in 商户平台 → 产品中心 → 开发配置.

### Booking app — core

| Variable | Required | Default | Where to get it |
|---|---|---|---|
| `APP_ENV` | prod: yes | `dev` | Set `prod`. `dev` exposes the interactive `/docs`; `prod` disables it. |
| `DATABASE_URL` | prod: yes | `sqlite:///./booking.db` | Railway Postgres plugin. **Change the scheme to `postgresql+psycopg://…`** — the repo installs `psycopg` v3, not `psycopg2`, and a bare `postgresql://…` makes SQLAlchemy look for the wrong driver. |
| `SECRET_KEY` | yes | `dev-only-change-me` | `openssl rand -hex 32`. Also the admin token for `GET /api/admin/bookings`. |
| `PUBLIC_BASE_URL` | yes | `http://localhost:8000` | The ICP-filed payment domain, e.g. `https://pay.zhituoyuan.com`. The notify URL is derived from it. |
| `DEFAULT_TIMEZONE` | no | `Asia/Shanghai` | IANA name; used for display and for the email body. |

### Booking app — calendar

| Variable | Required | Default | Where to get it |
|---|---|---|---|
| `CALENDAR_GATEWAY` | yes | `fake` | `relay` in prod, `fake` for offline dev, `google` only if the app itself talks to Google (local dev). |
| `CALENDAR_RELAY_URL` | when `relay` | `""` | The Railway relay's public domain, e.g. `https://booking-relay.up.railway.app`. |
| `CALENDAR_RELAY_SECRET` | when `relay` | `""` | `openssl rand -hex 32`. Must equal the relay's `RELAY_SECRET`. |
| `GOOGLE_CLIENT_ID` | only when `google` | `""` | Google Cloud Console → APIs & Services → Credentials → OAuth client ID. |
| `GOOGLE_CLIENT_SECRET` | only when `google` | `""` | Same OAuth client. |
| `GOOGLE_REFRESH_TOKEN` | only when `google` | `""` | One-time consent with scope `https://www.googleapis.com/auth/calendar` and `access_type=offline` (e.g. OAuth 2.0 Playground). |
| `GOOGLE_CALENDAR_ID` | no | `primary` | Google Calendar → Settings → the calendar's ID, or `primary` for the owner's main calendar. |

### Booking app — payments

| Variable | Required | Default | Where to get it |
|---|---|---|---|
| `PAYMENT_GATEWAY` | yes | `fake` | `wechat` in prod, `fake` for offline dev. |
| `WECHAT_APP_ID` | when `wechat` | `""` | 商户平台 → 产品中心 → **APPID账号管理**: a verified 服务号/小程序 APPID bound to the merchant. |
| `WECHAT_MCH_ID` | when `wechat` | `""` | The 商户号 from 微信支付商户平台. |
| `WECHAT_API_V3_KEY` | when `wechat` | `""` | 商户平台 → 账户中心 → API安全 → **APIv3密钥** (32 chars, `openssl rand -hex 16`). |
| `WECHAT_CERT_SERIAL_NO` | when `wechat` | `""` | `openssl x509 -in secrets/apiclient_cert.pem -noout -serial` (merchant API cert). |
| `WECHAT_PRIVATE_KEY_PATH` | when `wechat` | `""` | Path **inside the container** to `apiclient_key.pem` (merchant API cert private key). |
| `WECHAT_PUBLIC_KEY_ID` | when `wechat` | `""` | 商户平台 → API安全 → **微信支付公钥**: its 公钥ID. |
| `WECHAT_PUBLIC_KEY_PATH` | when `wechat` | `""` | Path inside the container to `pub_key.pem` (微信支付公钥). |

`WECHAT_PRIVATE_KEY_PATH` and `WECHAT_PUBLIC_KEY_PATH` are **file paths**, and the adapter
reads the PEM from disk. Railway has no file-mount UI, so materialise the files at boot —
e.g. base64 the PEMs into variables and decode them in the start command:

```sh
sh -c 'mkdir -p /srv/secrets
       printf %s "$WECHAT_PRIVATE_KEY_B64" | base64 -d > /srv/secrets/apiclient_key.pem
       printf %s "$WECHAT_PUBLIC_KEY_B64"  | base64 -d > /srv/secrets/pub_key.pem
       exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}'
```

…with `WECHAT_PRIVATE_KEY_PATH=/srv/secrets/apiclient_key.pem` and
`WECHAT_PUBLIC_KEY_PATH=/srv/secrets/pub_key.pem`. Never commit the PEMs; `secrets/` and
`*.pem` are gitignored.

### Booking app — policy, email, worker

| Variable | Required | Default | Where to get it |
|---|---|---|---|
| `HOLD_MINUTES` | no | `10` | How long a slot is held while unpaid. |
| `SLOT_STEP_MINUTES` | no | `15` | Slot-grid step. |
| `SWEEPER_INTERVAL_SECONDS` | no | `60` | Loop interval of `python -m app.services.sweeper`. |
| `EMAIL_BACKEND` | no | `console` | `smtp` in prod, `console` for dev (prints to stdout). |
| `SMTP_HOST` | when `smtp` | `""` | Your mail provider. |
| `SMTP_PORT` | no | `465` | Provider port (465 = implicit TLS). |
| `SMTP_USER` | when `smtp` | `""` | Provider account. |
| `SMTP_PASSWORD` | when `smtp` | `""` | Provider password / app password. |
| `EMAIL_FROM` | when `smtp` | `""` | A mailbox on your sending domain. |
| `CELERY_BROKER_URL` | no | `""` | Railway Redis plugin (`REDIS_URL`). **Leave empty and no Celery app is built** — tasks run inline via `BackgroundTasks`, which is what dev and tests use. Set it only if you also run a worker. |
| `CELERY_RESULT_BACKEND` | no | `""` | Same Redis; optional. |

### Booking app — rate limiting (BUILD_PLAN §10)

`POST /api/bookings` creates a calendar hold **and** a WeChat order per accepted call,
so it is throttled per client IP. `GET /api/slots` is throttled more loosely for the
same reason — one calendar fan-out per call, and exhausting Google's freebusy quota
would break booking for everyone. `POST /api/payments/wechat/notify` is deliberately
**never** throttled: WeChat's retry schedule is part of the payment protocol, and
refusing a retry would strand a paid booking.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `RATE_LIMIT_BOOKINGS_PER_HOUR` | no | `20` | Per IP. A human books a handful of times an hour, so this is generous. |
| `RATE_LIMIT_SLOTS_PER_HOUR` | no | `300` | Per IP. Loose enough that browsing a month of dates never trips it. |
| `TRUSTED_PROXY_DEPTH` | no | `0` | Number of trusted reverse proxies in front of the app. `0` uses the peer address. **Set this to `1` behind a single nginx / platform proxy**, otherwise the app sees only the proxy's IP and every visitor shares one budget. |

Two honest limitations. **The counters are per process**, so the effective budget is
these values times the number of uvicorn workers or replicas — `Dockerfile`
starts one worker, so a one-replica deploy is exact. And it is a **fixed** window, not
a sliding one, so a caller can burst up to roughly twice the limit across a boundary.
Both are fine for abuse control; neither is a billing primitive. See `app/rate_limit.py`.

### Relay service

| Variable | Required | Default | Where to get it |
|---|---|---|---|
| `RELAY_SECRET` | yes | `""` | `openssl rand -hex 32`. Must equal the booking app's `CALENDAR_RELAY_SECRET`. |
| `GOOGLE_CLIENT_ID` | yes | `""` | Google Cloud Console → APIs & Services → Credentials → OAuth client ID. |
| `GOOGLE_CLIENT_SECRET` | yes | `""` | Same OAuth client. |
| `GOOGLE_REFRESH_TOKEN` | yes | `""` | One-time consent, `access_type=offline`, scope `https://www.googleapis.com/auth/calendar`. |
| `GOOGLE_CALENDAR_ID` | no | `primary` | The owner's calendar ID, or `primary`. |

---

## Local development — zero external services

The `.env.example` defaults are already an offline configuration: SQLite,
`CALENDAR_GATEWAY=fake`, `PAYMENT_GATEWAY=fake`, `EMAIL_BACKEND=console`.

```sh
git clone <repo> && cd booking-service

# Recommended (matches docs/):
uv sync --extra dev

# Or with plain pip:
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"

cp .env.example .env          # offline defaults, no edits needed
uv run python -m app.seed     # creates the consult-30 event type (idempotent)
uv run uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/> for the booking flow. `GET /healthz` returns `{"ok": true}`.
The fake payment gateway returns a `code_url` immediately, so you can walk the whole flow
without a WeChat merchant account.

Other useful commands:

```sh
uv run python -m app.services.sweeper   # the periodic hygiene sweeper
uv run python -m app.seed               # safe to re-run; one row
```

### Tests

No network and no external services, by design (contract §12). `tests/conftest.py` sets the
environment before importing the app, so **do not** point a test run at your `.env`.

```sh
uv run pytest                                   # the whole suite
uv run pytest tests/test_e2e.py -q              # the end-to-end suite
uv run ruff check .                             # lint
```

**If `tmp_path` fails with a `PermissionError` at fixture setup**, the sandbox is refusing
to create `.pytest_tmp/` inside the repo. Point pytest at a system temp root instead —
nothing else changes:

```sh
uv run pytest --basetemp=/tmp/booking-pytest
```

`tests/test_e2e.py` drives the real HTTP surface, and uses the **real**
`WechatPayGateway` (signature verification + AES-GCM decryption) with only its `httpx`
transport mocked, so the payment path is exercised without a socket. `tests/fakes.py`'s
`make_wechat_notify` builds a genuinely signed, genuinely encrypted callback from a
throwaway keypair.

---

## Deployment

Two images, two Railway services, one repository. Both Dockerfiles use the repository root
as the build context.

| File | What it builds |
|---|---|
| `Dockerfile` | `uvicorn app.main:app`, port `$PORT` (default 8000), non-root. Installs every dependency in `pyproject.toml`, copies only `app/`. |
| `Dockerfile.relay` | `uvicorn relay.main:app`, port `$PORT` (default 8000), non-root. Installs only the subset of `pyproject.toml` that `relay/**` imports — no SQLAlchemy, Celery, Redis, segno, Jinja2, cryptography or psycopg — and copies only `relay/`. |

```sh
docker build -t booking-service .
docker build -f Dockerfile.relay -t booking-relay .

docker run --rm -p 8000:8000 --env-file .env booking-service
docker run --rm -p 8001:8000 --env-file .env.relay booking-relay
```

### The booking Dockerfile must be named `Dockerfile`

This is not cosmetic, and it has already cost one failed deployment.

Railway's builder selection is: **use a file named exactly `Dockerfile` if the service's
root directory contains one; otherwise default to Railpack.** A Dockerfile under any other
name is simply not found — there is no warning, and Railpack takes over. The first deploy of
this repository failed with

```
Build › Build image   Railpack failed to prepare the build.
```

while the service manifest showed `builder: RAILPACK`, `dockerfilePath: null`. Nothing was
wrong with the Dockerfile; it was never looked at.

So `Dockerfile` (booking) keeps the plain name and builds with **zero per-service
configuration**. `Dockerfile.relay` cannot also be called `Dockerfile`, so the **relay
service must set its Dockerfile path explicitly** — see step 2 below.

Two ways to set it, either is fine:

| Where | Setting | Value |
|---|---|---|
| Dashboard | Service → Settings → Build → Builder / Dockerfile Path | `Dockerfile` / `Dockerfile.relay` |
| Repo | `railway.json` (booking) and `railway.relay.json` (relay), selected per service | auto-read for the booking service; the relay needs its config-as-code path set |

> **A root `railway.json` applies to every service that deploys from this repository.** The
> booking service is the one it is written for. When you add the relay service, point its
> config-as-code path at `/railway.relay.json` — otherwise it will find the root `Dockerfile`,
> build the *booking* image, and run the booking app on the relay's domain. That failure is
> silent: the build succeeds and the wrong service answers.

### Order to deploy

1. **Postgres.** Add the Railway Postgres plugin to the project. Note its `DATABASE_URL`
   and rewrite the scheme to `postgresql+psycopg://`.
2. **Relay first.** It has no dependency on the booking app, and the booking app needs its
   URL. Create a service from the repository, set its **Dockerfile path to `Dockerfile.relay`**
   (Settings → Build; or point its config-as-code path at `/railway.relay.json`), and set the
   relay variables from the table above (`railway variables --set RELAY_SECRET=…` or the
   dashboard). Check `https://<relay-domain>/healthz`.
3. **Booking app.** Create a second service from the same repository. If you use
   config-as-code, point its path at `/railway.json`; if you do not, set Builder =
   `Dockerfile` and Dockerfile Path = `Dockerfile`. Set `CALENDAR_RELAY_URL` to the relay's
   public domain, `CALENDAR_RELAY_SECRET` to the same value as `RELAY_SECRET`, and the rest
   of the booking variables. Check `https://<booking-domain>/healthz`.
4. **Seed once.** Run `python -m app.seed` against the deployed service (Railway one-off
   command, or a `preDeployCommand`). Without it the booking page has no event type.
5. **Sweeper.** Add a third service from the same image with the start command
   `python -m app.services.sweeper`. Correctness does not depend on it (contract §7) — it
   only releases stale calendar holds and closes abandoned WeChat orders.
6. **Register the notify URL.** Copy the derived
   `https://<booking-domain>/api/payments/wechat/notify` into 商户平台 → 产品中心 → 开发配置.

`railway.json` and `railway.relay.json` set `healthcheckPath` to `/healthz` and use the
`DOCKERFILE` builder with an explicit `dockerfilePath`. They contain only keys Railway
documents — an earlier revision carried the variable names under a non-schema `x-envVars`
key, which is gone: the env var tables above are the single source of truth for names.

> **Config-as-code is deprecated.** Railway's docs mark `railway.json` / `railway.toml` as
> legacy, keep them working only until **2026-12-01**, and do not let new services opt in.
> The forward path is `.railway/railway.ts` (Infrastructure as Code). This is exactly why
> the booking image does **not** depend on `railway.json` to be built — the plain
> `Dockerfile` name is the non-deprecated path. Plan the migration before the cutoff.

### Decisions (D1–D5)

- **D1 — where the payment step lives:** hybrid. The slot picker stays at
  `www.zhituoyuan.com/book`; on "pay" the browser is sent to `pay.zhituoyuan.com/pay/{reference}`
  on the ICP-filed domain, which hosts the QR, the polling, the confirmation and the notify
  endpoint. This keeps the whole payment path on the domain declared to WeChat and avoids
  CORS.
- **D2 — ICP route:** file a subdomain, `pay.zhituoyuan.com`, on a mainland host, rather
  than moving the whole marketing site off Cloudflare.
- **D3 — API access from the site:** proxy through a Next.js route handler
  (`app/api/booking/[...path]/route.ts`) so the browser stays same-origin — no CORS, and no
  API URL in client JS.
- **D4 — pricing and slots:** ¥500 / 30 minutes, Mon–Fri 09:00–18:00 Asia/Shanghai, 4-hour
  minimum notice, 60-day window. This is exactly what `app/seed.py` writes.
- **D5 — the paid consult sits beside the free `/contact` form**, not in place of it.

---

## Go-live checklist (BUILD_PLAN §9 P8)

- [ ] G1–G3 cleared (see [Blocking business gates](#blocking-business-gates-not-code)).
- [ ] A real ¥0.01 test payment on staging flips a booking to `paid` and creates the
      calendar event (P6 acceptance).
- [ ] Booking API deployed on the ICP-filed mainland host; `www`/Next.js
      `BOOKING_API_BASE` repointed at it (server-side only, not `NEXT_PUBLIC_`).
- [ ] `APP_ENV=prod`, `CALENDAR_GATEWAY=relay`, `PAYMENT_GATEWAY=wechat`,
      `EMAIL_BACKEND=smtp`, `CALENDAR_RELAY_URL`/`CALENDAR_RELAY_SECRET` set.
- [ ] `PUBLIC_BASE_URL` is the filed domain, and the **derived** notify URL is registered
      in 商户平台.
- [ ] `python -m app.seed` has been run against the production database.
- [ ] The sweeper service is running (or knowingly deferred — slots still free without it).
- [ ] A real ¥500 payment confirms a booking and the event appears in Google Calendar.

## Security checklist (BUILD_PLAN §10)

- [x] **Never trust a client-supplied amount, slot price or status.** `amount_fen` is
      snapshotted from `EventType.price_fen` at creation; a body carrying `amount_fen: 1`
      still charges 50000 — `tests/test_e2e.py::test_client_supplied_amount_cannot_change_the_price_charged`.
- [x] **Fail closed on signature verification.** `WechatPayGateway.parse_notification`
      verifies the RSA signature before decrypting and raises `PaymentSignatureError` on
      failure; the endpoint persists nothing and answers `401`. A stale timestamp is
      rejected before the body is touched.
- [x] **`transaction_id` unique** as the replay guard; a repeat delivery adds no second
      `PaymentEvent` and no second side effect.
- [x] **No public endpoint leaks a `code_url`.** `/book/{reference}` renders only the SVG
      markup; `GET /api/bookings/{reference}` omits `code_url`.
- [x] **Admin routes behind `X-Admin-Token`**, compared with `hmac.compare_digest`.
- [x] **Merchant private key and APIv3 key live in env/secret storage**, never in the repo;
      `secrets/` and `*.pem` are gitignored.
- [x] **Rate-limit `POST /api/bookings`** — in-app, per client IP, via `app/rate_limit.py`
      (`RATE_LIMIT_BOOKINGS_PER_HOUR`, default 20). Counters are per process and the window
      is fixed, so it is abuse control rather than a hard quota; **still put a limit at the
      edge** (mainland WAF / reverse proxy) before go-live, and set `TRUSTED_PROXY_DEPTH` to
      match the number of proxies so the app can see real client addresses.
- [ ] **Allow WeChat Pay's callback IP ranges through the firewall** on the mainland host,
      and block everything else on `/api/payments/wechat/notify`.

---

## Blocking business gates (not code)

**The project cannot go live until these are cleared. None of them can be done by writing
code, and none of them are done by deploying this repository.**

1. **ICP filing for the payment domain (G1).** `www.zhituoyuan.com` currently shows no 备案
   number and is served through Cloudflare. WeChat Pay's PC-website scenario requires the
   declared PC domain to be ICP-filed, and a filing is invalidated by leaving a mainland
   接入商. Verify the domain at `beian.miit.gov.cn` and record the 备案号 / 主体 / 接入商.
   The recommended route (D2) is to file `pay.zhituoyuan.com` on a mainland host.
2. **WeChat Pay merchant prerequisites (G2).** All of these must exist before the payment
   path can work:
   - a 营业执照 (个体工商户 or 企业);
   - a 微信支付商户号 with **Native 支付 permission enabled**;
   - a **verified 服务号/小程序 APPID bound to the merchant** (商户平台 → 产品中心 →
     APPID账号管理);
   - the **商户API证书** → `apiclient_key.pem`, `apiclient_cert.pem` and the 证书序列号;
   - the **APIv3密钥** (32 chars);
   - the **微信支付公钥** → `pub_key.pem` and its 公钥ID;
   - the **Native 回调地址 configured in 商户平台** — it must be the derived
     `https://<filed-domain>/api/payments/wechat/notify`.
3. **Google OAuth client published to Production (G3).** A Cloud OAuth client left in
   "Testing" expires refresh tokens every 7 days, which silently stops the calendar
   syncing — bookings would still be taken against a stale grid. Either publish the OAuth
   consent screen to Production, or move to a Workspace service account.
4. **Accept the two-box topology (G4).** One box cannot hold both an ICP-filed domain and
   reach `googleapis.com`. Do not try to collapse the two services.

---

## Known limitations / not in v1 (BUILD_PLAN §13)

- **No refunds.** There is no refund path at all, including for the late-payment edge case
  described in the report. Out of scope for v1.
- **No 发票 / invoicing, no coupons.**
- **No multi-staff round-robin** — one calendar, one owner.
- **No mini-program** (Native / 扫码 only).
- **No admin CRUD UI** — `GET /api/admin/bookings` is read-only.
- **No i18n** — the pages are Chinese.
- **No Stripe** and no other payment provider.
- **No recurring availability exceptions** — the weekly `AvailabilityRule` grid only; time
  off is expressed by putting it on the Google Calendar.
- **No Google Calendar push notifications** — availability is `freebusy` on read plus the
  sweeper. That is enough for v1.
- **No Alembic migrations.** The schema is created with `create_all()` at startup; swap in
  Alembic when the schema stabilises.
- **Rate limiting is per process and uses a fixed window.** With `--workers 2` or two
  replicas the real budget is the configured limit times the number of processes, and a
  caller can burst up to roughly twice the limit across a window boundary. Adequate for
  abuse control, not a quota. A shared store (Redis, or a table) is the fix when a single
  replica stops being enough. The notify endpoint is intentionally never throttled.
- **Correctness never depends on Celery.** With no broker configured no Celery app is built
  and the three tasks run inline; a dead worker costs only stale calendar holds, never a
  stuck slot.
