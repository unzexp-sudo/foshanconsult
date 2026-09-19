import type { Metadata } from "next";

import { bookingApi, type EventType } from "@/lib/booking-api";

import { BookingFlow } from "./booking-flow";

/**
 * `/book` — the slot picker.
 *
 * A server component so the event types are in the first paint and the page works
 * without JS up to the point where interaction genuinely requires it.
 *
 * TODO(site): wrap this in the existing `site-frame` / `layout-segment-context` so
 * the nav and footer are unchanged, and reuse the site's design tokens rather than
 * the inline styles below. The booking page must not look like a different product.
 */

export const metadata: Metadata = {
  title: "预约 1-1 咨询 · 智拓源",
  description: "选择时间，微信支付完成预约。30 分钟一对一咨询。",
};

export const dynamic = "force-dynamic";

/** Where the QR lives. The payment page is on the ICP-filed domain. */
function payBase(): string {
  const value = process.env.NEXT_PUBLIC_PAY_BASE;
  if (!value) {
    // Not a build error: it only matters once someone actually books. Fail loudly
    // at that point rather than shipping a redirect to nowhere.
    console.warn(
      "[book] NEXT_PUBLIC_PAY_BASE is not set; the pay redirect will not work.",
    );
    return "";
  }
  return value.replace(/\/$/, "");
}

export default async function BookPage() {
  let eventTypes: EventType[] = [];
  let loadError: string | null = null;

  try {
    eventTypes = await bookingApi.listEventTypes();
  } catch (error) {
    // A dead booking service must not produce a 500 on a marketing page.
    loadError = error instanceof Error ? error.message : "无法加载可预约项目";
  }

  return (
    <main
      style={{
        maxWidth: 720,
        margin: "0 auto",
        padding: "48px 20px 96px",
        lineHeight: 1.6,
      }}
    >
      <header style={{ marginBottom: 40 }}>
        <h1 style={{ fontSize: 32, lineHeight: 1.25, margin: "0 0 12px" }}>
          预约 1-1 咨询
        </h1>
        <p style={{ margin: 0, opacity: 0.72, fontSize: 16 }}>
          选择一个方便的时间，微信扫码支付后预约即刻确认，日历邀请与确认邮件会立即发出。
        </p>
      </header>

      {loadError ? (
        <p
          role="alert"
          style={{
            padding: 16,
            border: "1px solid currentColor",
            borderRadius: 8,
            opacity: 0.8,
          }}
        >
          预约服务暂时不可用，请稍后再试，或直接联系我们。
        </p>
      ) : eventTypes.length === 0 ? (
        <p style={{ opacity: 0.72 }}>当前没有开放预约的项目。</p>
      ) : (
        <BookingFlow eventTypes={eventTypes} payBase={payBase()} />
      )}
    </main>
  );
}
