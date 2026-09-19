/**
 * The ONLY file that knows the booking service's base URL.
 *
 * Two halves, and the split is deliberate:
 *
 * - `bookingApi` runs on the SERVER. It reads `BOOKING_API_BASE` and talks to the
 *   booking service directly. `BOOKING_API_BASE` must NOT be `NEXT_PUBLIC_` — the
 *   browser never learns the API host.
 * - `clientBookingApi` runs in the BROWSER. It only ever calls this site's own
 *   `/api/booking/*` route handler, so every request stays same-origin. No CORS
 *   configuration, and no API URL in client JS.
 *
 * Adding a field here is how a new endpoint becomes reachable; do not call
 * `fetch` against the booking service from anywhere else.
 */

export type BookingStatus = "pending_payment" | "paid" | "expired" | "cancelled";

export type EventType = {
  id: string;
  title: string;
  description: string;
  duration_minutes: number;
  price_fen: number;
  currency: string;
  timezone: string;
};

export type Slot = { start: string; end: string };

export type SlotsResponse = {
  event_type_id: string;
  date: string;
  timezone: string;
  slots: Slot[];
};

export type BookingCreated = {
  reference: string;
  status: BookingStatus;
  amount_fen: number;
  currency: string;
  expires_at: string;
  slot_start: string;
  /**
   * The WeChat Native payment token. Present for completeness, but the browser
   * flow deliberately does NOT use it: after creating a booking we redirect to the
   * payment page on the ICP-filed domain, where the QR is rendered server-side.
   * That keeps the token out of client JS entirely. Do not render a QR from this.
   */
  code_url: string;
};

export type BookingStatusResponse = {
  reference: string;
  status: BookingStatus;
  amount_fen: number;
  currency: string;
  expires_at: string;
  slot_start: string;
  event_type_id: string;
};

export type CreateBookingInput = {
  event_type_id: string;
  /** ISO-8601 WITH an offset, e.g. 2026-09-21T14:00:00+08:00 */
  slot_start: string;
  customer_name: string;
  customer_email: string;
  customer_phone?: string;
  customer_note?: string;
};

export class BookingApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "BookingApiError";
  }
}

function baseUrl(): string {
  const value = process.env.BOOKING_API_BASE;
  if (!value) {
    throw new Error(
      "BOOKING_API_BASE is not set. It must point at the booking service " +
        "(e.g. https://pay.zhituoyuan.com) and must NOT be a NEXT_PUBLIC_ variable.",
    );
  }
  return value.replace(/\/$/, "");
}

type RequestOptions = {
  method?: "GET" | "POST";
  search?: string;
  body?: string;
};

/**
 * Server-side call to the booking service. Used by the route handler and by
 * server components. Throws `BookingApiError` on a non-2xx so callers can map the
 * status onto their own response.
 */
export async function bookingApiFetch(
  path: string,
  options: RequestOptions = {},
): Promise<{ status: number; json: unknown }> {
  const { method = "GET", search = "", body } = options;

  const response = await fetch(`${baseUrl()}/${path.replace(/^\//, "")}${search}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body,
    // Bookings and slot availability are never cacheable.
    cache: "no-store",
  });

  const text = await response.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = { detail: text };
  }

  if (!response.ok) {
    const detail =
      typeof json === "object" && json !== null && "detail" in json
        ? String((json as { detail: unknown }).detail)
        : `booking service returned ${response.status}`;
    throw new BookingApiError(detail, response.status);
  }

  return { status: response.status, json };
}

// ---------------------------------------------------------------------------
// Server-side typed wrappers
// ---------------------------------------------------------------------------

export const bookingApi = {
  async listEventTypes(): Promise<EventType[]> {
    const { json } = await bookingApiFetch("api/event-types");
    return json as EventType[];
  },

  async listSlots(eventTypeId: string, date: string): Promise<SlotsResponse> {
    const search = `?${new URLSearchParams({ event_type_id: eventTypeId, date })}`;
    const { json } = await bookingApiFetch("api/slots", { search });
    return json as SlotsResponse;
  },

  async createBooking(input: CreateBookingInput): Promise<BookingCreated> {
    const { json } = await bookingApiFetch("api/bookings", {
      method: "POST",
      body: JSON.stringify(input),
    });
    return json as BookingCreated;
  },

  async getBooking(reference: string): Promise<BookingStatusResponse> {
    const { json } = await bookingApiFetch(`api/bookings/${encodeURIComponent(reference)}`);
    return json as BookingStatusResponse;
  },
};

// ---------------------------------------------------------------------------
// Browser-side wrappers — same-origin only, via this site's proxy route
// ---------------------------------------------------------------------------

async function proxyFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/booking/${path}`, {
    ...init,
    cache: "no-store",
  });

  const json = await response.json().catch(() => null);

  if (!response.ok) {
    const detail =
      json && typeof json === "object" && "detail" in json
        ? String((json as { detail: unknown }).detail)
        : `request failed with ${response.status}`;
    throw new BookingApiError(detail, response.status);
  }

  return json as T;
}

export const clientBookingApi = {
  listSlots(eventTypeId: string, date: string): Promise<SlotsResponse> {
    const search = new URLSearchParams({ event_type_id: eventTypeId, date });
    return proxyFetch<SlotsResponse>(`api/slots?${search}`);
  },

  createBooking(input: CreateBookingInput): Promise<BookingCreated> {
    return proxyFetch<BookingCreated>("api/bookings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  },

  getBooking(reference: string): Promise<BookingStatusResponse> {
    return proxyFetch<BookingStatusResponse>(`api/bookings/${encodeURIComponent(reference)}`);
  },
};

// ---------------------------------------------------------------------------
// Display helpers — money is 分 everywhere until it is shown
// ---------------------------------------------------------------------------

/** `50000` → `"¥500"`, `50500` → `"¥505.00"`. Never do arithmetic on yuan. */
export function formatFen(fen: number, currency = "CNY"): string {
  const symbol = currency === "CNY" ? "¥" : `${currency} `;
  const yuan = fen / 100;
  return Number.isInteger(yuan) ? `${symbol}${yuan}` : `${symbol}${yuan.toFixed(2)}`;
}

/** Format a slot in the event's own timezone, not the visitor's. */
export function formatSlot(iso: string, timezone: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

export function formatTime(iso: string, timezone: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}
