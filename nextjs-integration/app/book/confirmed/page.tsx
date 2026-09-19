import type { Metadata } from "next";
import Link from "next/link";

import { bookingApi, formatFen, formatSlot, type BookingStatusResponse } from "@/lib/booking-api";

/**
 * `/book/confirmed?reference=BK7Q2M4X` — the page the payment page sends the
 * customer back to once WeChat has confirmed the money.
 *
 * It re-reads the booking from the service rather than trusting the query string,
 * so a bookmarked or hand-edited URL cannot show a "paid" page for an unpaid
 * booking.
 *
 * TODO(site): wrap in the existing site frame and use the site's design tokens.
 */

export const metadata: Metadata = {
  title: "预约已确认 · 智拓源",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ reference?: string }> };

export default async function ConfirmedPage({ searchParams }: Props) {
  const { reference } = await searchParams;

  if (!reference) {
    return (
      <Shell title="缺少预约编号">
        <p style={{ opacity: 0.75 }}>
          请通过确认邮件中的链接打开本页，或
          <Link href="/book" style={{ marginLeft: 4 }}>
            重新预约
          </Link>
          。
        </p>
      </Shell>
    );
  }

  let booking: BookingStatusResponse | null = null;
  let failed = false;

  try {
    booking = await bookingApi.getBooking(reference);
  } catch {
    failed = true;
  }

  if (failed || !booking) {
    return (
      <Shell title="没有找到这个预约">
        <p style={{ opacity: 0.75 }}>
          预约编号 <code>{reference}</code> 不存在。如果刚刚完成支付，请稍等片刻后刷新，
          或直接联系我们。
        </p>
      </Shell>
    );
  }

  if (booking.status === "pending_payment") {
    return (
      <Shell title="等待支付">
        <p style={{ opacity: 0.75 }}>
          预约 <code>{booking.reference}</code> 尚未完成支付。请回到支付页面扫码付款，
          超时后该时段会自动释放。
        </p>
      </Shell>
    );
  }

  if (booking.status !== "paid") {
    return (
      <Shell title={booking.status === "cancelled" ? "预约已取消" : "预约已失效"}>
        <p style={{ opacity: 0.75 }}>
          该时段已释放。
          <Link href="/book" style={{ marginLeft: 4 }}>
            重新预约
          </Link>
          。
        </p>
      </Shell>
    );
  }

  return (
    <Shell title="预约已确认">
      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "10px 24px",
          margin: "0 0 28px",
        }}
      >
        <Row label="预约编号">
          <code>{booking.reference}</code>
        </Row>
        <Row label="时间">{formatSlot(booking.slot_start, "Asia/Shanghai")}（北京时间）</Row>
        <Row label="金额">{formatFen(booking.amount_fen, booking.currency)}</Row>
      </dl>
      <p style={{ opacity: 0.75, margin: 0 }}>
        确认邮件已发送到你的邮箱，日历邀请也会同步送达。如果邮件没有出现，请检查垃圾邮件目录。
      </p>
    </Shell>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt style={{ opacity: 0.66, fontSize: 14 }}>{label}</dt>
      <dd style={{ margin: 0 }}>{children}</dd>
    </>
  );
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main
      style={{
        maxWidth: 640,
        margin: "0 auto",
        padding: "64px 20px 96px",
        lineHeight: 1.6,
      }}
    >
      <h1 style={{ fontSize: 28, margin: "0 0 20px" }}>{title}</h1>
      {children}
    </main>
  );
}
