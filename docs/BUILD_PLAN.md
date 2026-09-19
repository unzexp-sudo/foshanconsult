# BUILD PLAN — paid 1-1 booking for zhituoyuan.com

**Status:** plan only. Nothing below has been executed beyond the scaffolding noted in §11.
**Goal:** a visitor to `www.zhituoyuan.com` picks a real slot from your Google Calendar, pays
by WeChat Pay, and the booking becomes real **only after WeChat's signed payment callback
verifies the money arrived.**

Read §1 first. Two of those gates are not code problems and they can stop the whole project.

---

## 1. Hard gates — clear these before writing production code

- [ ] **G1 — ICP filing for the payment domain.** `www.zhituoyuan.com` displays no 备案 number
      (footer is `© 2026` only) and is served through Cloudflare. WeChat Pay's PC-website
      scenario requires the declared PC domain to be **ICP-filed**, and a filing is invalidated
      when you leave a mainland 接入商. So the current domain almost certainly cannot be the
      payment domain.
  - Verify, don't assume: look the domain up at `beian.miit.gov.cn` and record the 备案号, 主体,
    and 接入商. If there is no valid filing, treat G1 as open.
  - Pick one: **(a)** file `zhituoyuan.com` and move it onto a mainland 接入商 — slow, and it
    means moving the marketing site off Cloudflare; **(b)** file a separate subdomain
    `pay.zhituoyuan.com` on a mainland server and declare that as the PC website — **recommended**;
    **(c)** use a payment aggregator whose own filed domain handles checkout and relays the
    notify to you — no filing work, but a vendor cut and a dependency.
- [ ] **G2 — WeChat Pay merchant prerequisites.** Collect all of these before code work starts:
  - [ ] 营业执照 (个体工商户 or 企业 — you confirmed you have one)
  - [ ] 微信支付商户号 with **Native 支付 permission enabled**
  - [ ] a **verified 服务号 or 小程序 APPID**, bound to the merchant under
        商户平台 → 产品中心 → APPID账号管理
  - [ ] 商户API证书 → gives `apiclient_key.pem`, `apiclient_cert.pem`, and the 证书序列号
        (`openssl x509 -in secrets/apiclient_cert.pem -noout -serial`)
  - [ ] APIv3密钥 — 32 chars, `openssl rand -hex 16`
  - [ ] 微信支付公钥 → `pub_key.pem` + its 公钥ID
  - [ ] the Native 回调地址 configured in 商户平台 → 产品中心 → 开发配置
- [ ] **G3 — Google OAuth client published to Production.** A Cloud client left in "Testing"
      expires refresh tokens every 7 days, which would silently stop the calendar syncing.
      Either publish the OAuth consent screen, or move to a Workspace service account.
- [ ] **G4 — accept the two-box topology.** `googleapis.com` is unreachable from mainland
      servers, and WeChat Pay needs a mainland domain. One box cannot do both. See §2.

## 2. Architecture

- [ ] Deploy **two** services. Do not try to collapse them.
  - **Booking API** — FastAPI. Runs on **Railway** now, moves to a **mainland host on the
    ICP-filed domain** before go-live. Owns bookings, slots, WeChat Pay, and the notify endpoint.
  - **Calendar relay** — small FastAPI. Runs on **Railway**. Holds the only Google credential.
    Talks to Google; the booking API never does.
- [ ] Direction of trust: booking API → relay over HTTPS with an HMAC-signed body. Outbound
      mainland → Railway works fine. Relay → Google works fine.
- [ ] Local dev must run with **zero external services**: `CALENDAR_GATEWAY=fake`,
      `PAYMENT_GATEWAY=fake`, SQLite, console email.
- [ ] Keep every gateway behind a port so the topology is a config switch, not a rewrite.

## 3. Integration surface into the existing Next.js site

- [ ] **D1 — decide where the payment step lives.** Recommendation: hybrid.
  - Slot picker at `www.zhituoyuan.com/book` (Next.js, existing brand, existing design tokens).
  - On "pay", redirect to `pay.zhituoyuan.com/pay/{reference}` on the ICP-filed domain. That
    domain hosts the QR, the polling, the confirmation, and the notify endpoint.
  - Why: it keeps the whole payment path on the domain you declared to WeChat, avoids CORS, and
    avoids putting an overseas hop in the middle of a payment.
- [ ] Add to the Next.js repo, exactly these files and nothing else:
  - [ ] `app/book/page.tsx` — server component shell, reuses `site-frame` / `layout-segment-context`
        so the nav and footer are unchanged.
  - [ ] `app/book/booking-flow.tsx` — `"use client"`. Event type select → date → slot grid →
        name/email → create booking → render QR → poll status every 3s → redirect on `paid`.
  - [ ] `lib/booking-api.ts` — the **only** file that knows the API base URL. Typed wrappers for
        `listEventTypes`, `listSlots`, `createBooking`, `getBooking`.
  - [ ] `app/book/confirmed/page.tsx` — success page.
- [ ] Add a nav item and change the `/contact` primary CTA to offer the paid consult alongside
      the free lead form. Do not remove the free form — it is the existing funnel.
- [ ] Env: `BOOKING_API_BASE=https://pay.zhituoyuan.com` (server-side only, **not** `NEXT_PUBLIC_`).
- [ ] Proxy through a Next.js route handler at `app/api/booking/[...path]/route.ts` so the browser
      stays same-origin — no CORS config, no API URL in client JS. Skip this only if you accept
      configuring CORS for `https://www.zhituoyuan.com`.
- [ ] Reuse `/brand/logo-horizontal.svg` and the existing type scale. The booking page must not
      look like a different product.

## 4. Backend deliverables (booking API)

- [ ] `app/config.py` — settings, frozen names. `wechat_notify_url` is derived from
      `public_base_url`, never configured separately.
- [ ] `app/models.py` — authoritative. Tables: `event_types`, `availability_rules`, `bookings`,
      `payment_events`.
- [ ] **Double-booking guard** — a partial unique index, the single most important schema detail:
  - [ ] `uq_active_slot ON bookings (event_type_id, slot_start) WHERE status IN ('pending_payment','paid')`
  - [ ] Must be declared with **both** `sqlite_where` and `postgresql_where` so it holds in dev
        and prod.
  - [ ] The enum must store **values** (`pending_payment`), not member names (`PENDING_PAYMENT`),
        or the index predicate silently never matches. Use `values_callable` + `native_enum=False`.
- [ ] `app/ports/` — three protocols: `CalendarGateway`, `PaymentGateway`, `EmailSender`.
- [ ] `app/adapters/` — `calendar_fake`, `calendar_relay`, `calendar_google`, `payments_fake`,
      `payments_wechat`, `email_console`, `email_smtp`.
- [ ] `app/services/availability.py` — generate the slot grid from `AvailabilityRule` in the event
      type's own timezone, subtract calendar busy intervals, drop anything inside
      `min_notice_minutes` or beyond `max_days_ahead`.
- [ ] `app/services/booking.py` — create hold, expire, cancel, confirm. Owns the state machine.
- [ ] `app/services/sweeper.py` — hygiene only. See §7.
- [ ] `app/routers/` — `pages`, `booking`, `payments`, `admin`.
- [ ] `app/seed.py` — seeds one event type: `consult-30`, "1-1 出海获客诊断", 30 min, **50000 分
      (¥500)**, Mon–Fri 09:00–18:00 Asia/Shanghai, 240 min minimum notice, 60-day window.

## 5. API surface — exact shapes

- [ ] `GET /api/event-types` → `[{id, title, description, duration_minutes, price_fen, currency, timezone}]`
- [ ] `GET /api/slots?event_type_id=&date=YYYY-MM-DD` → `{event_type_id, date, timezone, slots:[{start,end}]}`
      (UTC ISO-8601 with offset)
- [ ] `POST /api/bookings` body `{event_type_id, slot_start, customer_name, customer_email,
      customer_phone?, customer_note?}` →
      `{reference, status, amount_fen, currency, expires_at, slot_start, qr_svg}`
  - [ ] `qr_svg` is an inline SVG of the WeChat `code_url`, generated server-side with `segno`.
        Returning SVG rather than the raw `code_url` means the payment token never reaches client
        JS and you need no QR library in the Next.js bundle.
  - [ ] `409` if the slot is taken, `422` if off-grid / inside minimum notice / beyond the window.
  - [ ] **`amount_fen` is computed server-side from `EventType.price_fen` and snapshotted onto the
        booking.** Never trust a price from the client, and never re-read the price at payment time.
- [ ] `GET /api/bookings/{reference}` → `{reference, status, amount_fen, currency, expires_at,
      slot_start, event_type_id}` — the polling endpoint.
- [ ] `POST /api/bookings/{reference}/cancel` — only while `pending_payment`.
- [ ] `POST /api/payments/wechat/notify` — **no auth header**; authenticity comes from the signature.
- [ ] `GET /api/admin/bookings` — read-only list, `X-Admin-Token: <secret_key>`.
- [ ] `GET /healthz` → `{"ok": true}`

## 6. Payment flow — the part that must be exactly right

- [ ] **Create:** validate slot → insert booking (`pending_payment`, `expires_at = now + hold_minutes`,
      `amount_fen` snapshotted) → create calendar hold → call WeChat
      `POST /v3/pay/transactions/native` with `{appid, mchid, description, out_trade_no,
      time_expire, notify_url, amount:{total, currency:"CNY"}}` → return `qr_svg`.
- [ ] `out_trade_no` = the booking reference. One order per booking, and it is the idempotency key.
- [ ] **Notify handler, in this order:**
  - [ ] Verify `Wechatpay-Signature` over `f"{timestamp}\n{nonce}\n{body}\n"` using `pub_key.pem`,
        and check the timestamp is recent. **Fail closed** — bad signature returns
        `{"code":"FAIL"}`, never unverified data.
  - [ ] AES-256-GCM decrypt `resource` with the APIv3 key (`nonce`, `associated_data`).
  - [ ] Persist a `payment_events` row. `transaction_id` is **unique** — WeChat retries up to
        15 times and this is the replay guard.
  - [ ] Confirm `notification.amount_fen == booking.amount_fen` and `trade_state == "SUCCESS"`.
        On mismatch: log loudly, leave the booking `pending_payment`, respond `FAIL`. Do **not**
        mark it paid.
  - [ ] Mark `paid`, set `paid_at` and `provider_transaction_id`, confirm the calendar event,
        send the confirmation email.
  - [ ] Respond `{"code":"SUCCESS","message":"成功"}` **within 5 seconds**. All calendar and email
        work goes to a background task — the relay hop can exceed 5s and WeChat will retry.
- [ ] **Expiry:** lazy expiry is authoritative. Before inserting any booking, the same transaction
      runs `UPDATE bookings SET status='expired' WHERE status='pending_payment' AND expires_at<=:now
      AND event_type_id=:et AND slot_start=:slot`. That is what frees the slot for the partial
      unique index. A sweeper then releases the calendar hold and closes the WeChat order.
- [ ] **Correctness must not depend on Celery.** If the worker is down, slots still free correctly
      and the only residue is stale calendar holds.

## 7. Calendar relay deliverables

- [ ] `relay/main.py`, `relay/config.py`, `relay/auth.py`, `relay/google.py`, `relay/schemas.py`.
- [ ] Auth on every request except `/healthz`:
      `X-Relay-Timestamp` (unix seconds) + `X-Relay-Signature` =
      `hex(hmac_sha256(secret, f"{timestamp}.{raw_body}"))`. Reject if `|now - ts| > 300`.
      Compare with `hmac.compare_digest`.
- [ ] Endpoints: `GET /healthz`, `POST /freebusy`, `POST /events`, `PATCH /events/{id}`,
      `DELETE /events/{id}`.
- [ ] `DELETE` on an unknown event returns success — release must be idempotent.
- [ ] Holds are created opaque (they block the slot); `confirm()` rewrites title and description.
- [ ] Use `freebusy.query` for availability and `events.insert/patch/delete` for holds. No push
      notifications in v1 — freebusy plus the sweeper is enough.

## 8. Tests — all offline, no network

- [ ] `tests/conftest.py` sets env vars **before** importing any app module (SQLite temp file,
      fake gateways), then provides `db_session`, `client`, `event_type`, `freeze_now`.
- [ ] `tests/fakes.py` provides `FakeCalendarGateway`, `FakePaymentGateway`,
      `RecordingEmailSender`, and **`make_wechat_notify()`** — which builds a genuinely signed,
      genuinely AES-GCM-encrypted callback from a throwaway RSA keypair. This is the fixture that
      makes the payment path testable at all.
- [ ] Required cases:
  - [ ] slot grid honours weekday rules, timezone, buffers, minimum notice and booking window
  - [ ] a slot already held is not offered and `POST /api/bookings` returns `409`
  - [ ] **two concurrent creates for the same slot → exactly one succeeds** (drive the real index,
        not a mock)
  - [ ] an expired hold frees its slot
  - [ ] `amount_fen` cannot be influenced by the client
  - [ ] valid signature verifies; tampered body, tampered signature, and stale timestamp each fail
  - [ ] replaying the same `transaction_id` produces no second side effect
  - [ ] an amount mismatch leaves the booking `pending_payment`
  - [ ] relay rejects a bad HMAC and a stale timestamp
- [ ] `uv run pytest` must pass with the network off.

## 9. Phases and acceptance criteria

- [ ] **P0 — gates.** Clear G1–G3. *Done when:* you have a 备案号 for the payment domain and a
      merchant account with Native enabled. No code needed.
- [ ] **P1 — foundation.** Settings, DB, models, ports, adapters stubs, health endpoint, seed.
      *Done when:* `uv run python -m app.seed` creates `consult-30` and `GET /healthz` returns ok.
- [ ] **P2 — availability + booking.** Slot grid, hold creation, expiry, cancel.
      *Done when:* `uv run pytest tests/test_availability.py tests/test_booking.py` is green and the
      concurrent-create test passes.
- [ ] **P3 — WeChat Pay.** Signing, notify verification, decryption, idempotency, close order.
      *Done when:* `tests/test_wechat_signature.py` and `tests/test_notify_idempotency.py` are green.
- [ ] **P4 — calendar relay.** Service + HMAC auth + Google client.
      *Done when:* `tests/test_relay.py` is green and `GET /healthz` works on Railway.
- [ ] **P5 — calendar adapters.** Relay client, direct Google client, fake.
      *Done when:* `tests/test_calendar_adapters.py` is green.
- [ ] **P6 — front end.** Next.js `/book` flow + the mainland pay page.
      *Done when:* you can book and pay end to end on staging with a ¥0.01 test event type.
- [ ] **P7 — end-to-end + deploy.** Dockerfiles, `railway.booking.json`, `railway.relay.json`,
      README with every env var and where to get it.
      *Done when:* `tests/test_e2e.py` is green and a real ¥1 payment on staging flips a booking
      to `paid` and creates the calendar event.
- [ ] **P8 — go live.** Move the booking API to the ICP-filed mainland host, repoint
      `BOOKING_API_BASE`, switch `CALENDAR_GATEWAY=relay` and `PAYMENT_GATEWAY=wechat`.
      *Done when:* a real ¥500 payment confirms a booking and the event appears in Google Calendar.

## 10. Security checklist

- [ ] Never trust a client-supplied amount, slot price, or status.
- [ ] Fail closed on signature verification — no unverified data ever reaches the DB.
- [ ] `transaction_id` unique constraint as the replay guard.
- [ ] `qr_svg` returned only to the booking's creator; no public endpoint that leaks a `code_url`.
- [ ] Admin routes behind `X-Admin-Token`, compared with `hmac.compare_digest`.
- [ ] WeChat merchant private key and APIv3 key live in env/secret storage, never in the repo.
      `secrets/` and `*.pem` are already gitignored.
- [ ] Rate-limit `POST /api/bookings` — it creates a calendar event and a payment order per call.
- [ ] Allow WeChat Pay's callback IP ranges through the firewall.

## 11. Already on disk (written before you asked me to stop)

These exist in `booking-service/` and match §4–§8. Keep them as the P1 starting point, or say the
word and I'll remove them.

- [x] `docs/MODULE_CONTRACT.md` — the frozen interface contract
- [x] `pyproject.toml`, `.gitignore`, `.env.example`
- [x] `app/config.py`, `app/db.py`, `app/models.py`, `app/schemas.py`, `app/deps.py`
- [x] `app/ports/{calendar,payments,email}.py`

Not yet written: everything in §4 from `app/adapters/` onward, all of §7, all of §8, all of §9.

## 12. Out of scope for v1

Refunds, 发票, coupons, multi-staff round-robin, mini-program, admin CRUD UI, i18n, Stripe,
recurring-availability exceptions, Google Calendar push notifications.

## 13. Decisions I need from you

| # | Decision | My recommendation |
|---|---|---|
| D1 | Where the payment step lives | Hybrid: slot picker on `www`, payment + notify on `pay.zhituoyuan.com` |
| D2 | G1 route — file the main domain, file a subdomain, or use an aggregator | File `pay.zhituoyuan.com` on a mainland host |
| D3 | Proxy the API through a Next.js route handler, or enable CORS | Proxy — no CORS, no API URL in client JS |
| D4 | Paid consult pricing and slot length | ¥500 / 30 min, Mon–Fri 09:00–18:00, 4h minimum notice |
| D5 | Does the paid consult replace the `/contact` form, or sit beside it | Sit beside it — don't break the existing free funnel |
