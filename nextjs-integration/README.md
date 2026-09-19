# Next.js integration — drop into the `zhituoyuan.com` site repo

These files belong in the **www.zhituoyuan.com** repository, not in the booking
service repo. They are kept here as a patch so the integration is reviewable in one
place; copy them across (or `git apply` them) when wiring the site up.

They are written against Next.js 15 App Router. If the site is on 14, change
`await params` / `await searchParams` back to plain objects — those two awaits are
the only version-sensitive lines.

## Files

| File | What it is |
|---|---|
| `lib/booking-api.ts` | The **only** file that knows the booking API base URL. Typed wrappers + the 分→¥ display helpers. |
| `app/api/booking/[...path]/route.ts` | Same-origin proxy. An allowlist, not an open proxy. |
| `app/book/page.tsx` | Server component shell; loads event types server-side. |
| `app/book/booking-flow.tsx` | `"use client"`. Type → date → slot → contact → pay. |
| `app/book/confirmed/page.tsx` | The page the payment domain sends the customer back to. |

## Environment

```
# Server-side only. Must NOT be NEXT_PUBLIC_ — the browser never learns this host.
BOOKING_API_BASE=https://pay.zhituoyuan.com

# Where the browser is redirected to pay. Public by nature (it is a URL the
# customer's browser visits), so NEXT_PUBLIC_ is correct here.
NEXT_PUBLIC_PAY_BASE=https://pay.zhituoyuan.com
```

## Why the split is shaped this way

The browser never talks to the booking service. `booking-flow.tsx` calls this
site's own `/api/booking/*`, the route handler forwards server-side to
`BOOKING_API_BASE`. That buys three things:

- **No CORS.** Everything is same-origin, so there is no `Access-Control-*`
  configuration to get subtly wrong.
- **No API host in client JS.** One base URL, in one server-only file.
- **No payment token in client JS.** `POST /api/bookings` returns a `code_url`,
  which is a WeChat payment token. The flow **does not render a QR from it** —
  on submit it redirects to `{PAY_BASE}/book/{reference}`, where the booking
  service renders the QR server-side into the HTML. The token never enters the
  browser's JS heap. Do not "improve" this by rendering the QR here.

## Two edits still to make in the site repo

1. **Add a nav item** pointing at `/book`.
2. **Change the `/contact` primary CTA** to offer the paid consult *alongside* the
   free lead form. Do **not** remove the free form — it is the existing funnel and
   the plan is explicit about sitting beside it, not replacing it (decision D5).

## Deliberate limitations

- `/api/event-types` does not expose `min_notice_minutes` or `max_days_ahead`, so
  the date strip shows a fixed 60-day window (`DATE_WINDOW_DAYS`) rather than the
  event type's real one. Days with no bookable slots simply come back empty. To fix
  properly, add those two fields to `EventTypeOut` in the booking service.
- The UI copy is Chinese and the code/comments are English, matching the product
  convention.
- Styling is inline and minimal so these files have no dependency on the site's
  component library. Replace it with the site's own tokens and components — the
  booking page must look like the rest of the site, not like a separate product.
