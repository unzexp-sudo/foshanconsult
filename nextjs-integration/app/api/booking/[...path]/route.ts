/**
 * Same-origin proxy to the booking service.
 *
 * Why this exists: the browser must never talk to the booking host directly. Going
 * through this handler keeps everything same-origin, so there is no CORS config to
 * get wrong and no API URL (or payment token) in client JS.
 *
 * Only the paths below are forwarded. An open proxy would be a gift to anyone
 * looking for a request-forgery foothold, so the allowlist is deliberate.
 *
 * Next.js 15: `params` is a Promise and must be awaited.
 */

import { NextResponse, type NextRequest } from "next/server";

import { bookingApiFetch, BookingApiError } from "@/lib/booking-api";

export const dynamic = "force-dynamic";

/** path prefix → allowed HTTP methods */
const ALLOWED: Record<string, readonly string[]> = {
  "api/event-types": ["GET"],
  "api/slots": ["GET"],
  "api/bookings": ["POST"],
};

const ALLOWED_PREFIXES = ["api/bookings/"];

function isAllowed(path: string, method: string): boolean {
  const methods = ALLOWED[path];
  if (methods) return methods.includes(method);
  return (
    ALLOWED_PREFIXES.some((prefix) => path.startsWith(prefix)) && method === "GET"
  );
}

async function proxy(request: NextRequest, segments: string[]) {
  const path = segments.join("/");
  const method = request.method;

  if (!isAllowed(path, method)) {
    return NextResponse.json(
      { detail: `unsupported booking route: ${method} /${path}` },
      { status: 405 },
    );
  }

  const body = method === "GET" || method === "HEAD" ? undefined : await request.text();

  try {
    const upstream = await bookingApiFetch(path, {
      method: method as "GET" | "POST",
      search: request.nextUrl.search,
      body,
    });
    return NextResponse.json(upstream.json, { status: upstream.status });
  } catch (error) {
    if (error instanceof BookingApiError) {
      // Pass the booking service's own status through: 409, 422 and 429 all carry
      // meaning the UI acts on ("this slot just went", "that time is not offered",
      // "you are being throttled — wait, do not retry"). The Retry-After header is
      // deliberately not forwarded; the UI tells the visitor to wait, it does not
      // schedule its own retry.
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    console.error("[booking proxy] upstream failure", error);
    return NextResponse.json({ detail: "booking service unavailable" }, { status: 502 });
  }
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, { params }: Context) {
  const { path } = await params;
  return proxy(request, path);
}

export async function POST(request: NextRequest, { params }: Context) {
  const { path } = await params;
  return proxy(request, path);
}
