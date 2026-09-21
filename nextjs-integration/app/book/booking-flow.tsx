"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  BookingApiError,
  clientBookingApi,
  formatFen,
  formatTime,
  type EventType,
  type Slot,
} from "@/lib/booking-api";

/**
 * The booking flow: event type → date → slot → contact details → pay.
 *
 * On submit we create the booking and then REDIRECT to the payment page on the
 * ICP-filed domain. We deliberately do not render the QR here:
 *
 * - the QR is rendered server-side there, so the WeChat `code_url` never reaches
 *   client JS (it is a payment token);
 * - the whole payment path stays on the domain declared to WeChat;
 * - there is no overseas hop in the middle of a payment.
 *
 * TODO(site): replace the inline styles with the site's design tokens/components.
 */

type Props = {
  eventTypes: EventType[];
  payBase: string;
};

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];
const DATE_WINDOW_DAYS = 60; // matches the seeded max_days_ahead

function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function BookingFlow({ eventTypes, payBase }: Props) {
  const [eventTypeId, setEventTypeId] = useState(eventTypes[0]?.id ?? "");
  const [date, setDate] = useState(() => isoDate(new Date()));
  const [slots, setSlots] = useState<Slot[]>([]);
  const [slotStart, setSlotStart] = useState<string | null>(null);
  const [loadingSlots, setLoadingSlots] = useState(false);
  const [form, setForm] = useState({
    customer_name: "",
    customer_email: "",
    customer_phone: "",
    customer_note: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const eventType = useMemo(
    () => eventTypes.find((candidate) => candidate.id === eventTypeId),
    [eventTypes, eventTypeId],
  );

  const days = useMemo(() => {
    const today = new Date();
    return Array.from({ length: DATE_WINDOW_DAYS }, (_, offset) => {
      const value = new Date(today);
      value.setDate(today.getDate() + offset);
      return value;
    });
  }, []);

  const loadSlots = useCallback(async () => {
    if (!eventTypeId) return;
    setLoadingSlots(true);
    setError(null);
    setSlotStart(null);
    try {
      const response = await clientBookingApi.listSlots(eventTypeId, date);
      setSlots(response.slots);
    } catch (caught) {
      setSlots([]);
      // 429 = throttled (BUILD_PLAN §10).  Distinguish it so the visitor is told to
      // wait rather than to "retry", which is the advice that makes it worse.
      setError(
        caught instanceof BookingApiError && caught.status === 429
          ? "查询过于频繁，请稍等片刻再刷新时段。"
          : "无法加载可预约时段，请稍后重试。",
      );
    } finally {
      setLoadingSlots(false);
    }
  }, [eventTypeId, date]);

  useEffect(() => {
    void loadSlots();
  }, [loadSlots]);

  const canSubmit =
    Boolean(slotStart) &&
    form.customer_name.trim().length > 0 &&
    /.+@.+\..+/.test(form.customer_email) &&
    !submitting;

  async function submit(formEvent: React.FormEvent) {
    formEvent.preventDefault();
    if (!canSubmit || !slotStart || !eventType) return;

    setSubmitting(true);
    setError(null);

    try {
      const booking = await clientBookingApi.createBooking({
        event_type_id: eventType.id,
        slot_start: slotStart,
        customer_name: form.customer_name.trim(),
        customer_email: form.customer_email.trim(),
        customer_phone: form.customer_phone.trim() || undefined,
        customer_note: form.customer_note.trim() || undefined,
      });

      if (!payBase) {
        setError("支付页面地址未配置，请联系我们。");
        return;
      }

      // Hand off to the payment page, which renders the QR server-side.
      window.location.href = `${payBase}/book/${encodeURIComponent(booking.reference)}`;
    } catch (caught) {
      if (caught instanceof BookingApiError && caught.status === 409) {
        setError("该时段刚刚被预订，请另选一个时间。");
        await loadSlots(); // the grid is stale now — refresh it
      } else if (caught instanceof BookingApiError && caught.status === 422) {
        setError("该时段不可预约，请另选一个时间。");
      } else if (caught instanceof BookingApiError && caught.status === 429) {
        // Throttled (BUILD_PLAN §10).  Retrying immediately makes it worse, so say
        // so plainly and leave the slot selection intact.
        setError("提交过于频繁，请稍等片刻再试。如持续出现，请通过官网联系我们。");
      } else {
        setError("提交失败，请稍后重试。");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ display: "grid", gap: 32 }}>
      {eventTypes.length > 1 && (
        <Step index={1} title="选择咨询项目">
          <div style={{ display: "grid", gap: 8 }}>
            {eventTypes.map((candidate) => (
              <label
                key={candidate.id}
                style={{
                  display: "flex",
                  gap: 12,
                  alignItems: "baseline",
                  padding: 14,
                  borderRadius: 8,
                  cursor: "pointer",
                  outline:
                    candidate.id === eventTypeId ? "2px solid currentColor" : "1px solid rgba(128,128,128,.4)",
                }}
              >
                <input
                  type="radio"
                  name="event_type"
                  value={candidate.id}
                  checked={candidate.id === eventTypeId}
                  onChange={() => setEventTypeId(candidate.id)}
                />
                <span style={{ flex: 1 }}>
                  <strong>{candidate.title}</strong>
                  <span style={{ display: "block", opacity: 0.72, fontSize: 14 }}>
                    {candidate.duration_minutes} 分钟 ·{" "}
                    {formatFen(candidate.price_fen, candidate.currency)}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </Step>
      )}

      <Step index={eventTypes.length > 1 ? 2 : 1} title="选择日期">
        <div
          style={{
            display: "flex",
            gap: 8,
            overflowX: "auto",
            paddingBottom: 8,
            scrollbarWidth: "thin",
          }}
        >
          {days.map((day) => {
            const value = isoDate(day);
            const selected = value === date;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setDate(value)}
                aria-pressed={selected}
                style={{
                  flex: "0 0 auto",
                  minWidth: 64,
                  padding: "10px 8px",
                  borderRadius: 8,
                  cursor: "pointer",
                  background: selected ? "currentColor" : "transparent",
                  color: selected ? "Canvas" : "inherit",
                  border: selected ? "1px solid currentColor" : "1px solid rgba(128,128,128,.4)",
                }}
              >
                <span style={{ display: "block", fontSize: 12, opacity: 0.8 }}>
                  周{WEEKDAYS[day.getDay()]}
                </span>
                <span style={{ display: "block", fontSize: 16, fontVariantNumeric: "tabular-nums" }}>
                  {day.getMonth() + 1}/{day.getDate()}
                </span>
              </button>
            );
          })}
        </div>
      </Step>

      <Step index={eventTypes.length > 1 ? 3 : 2} title="选择时间">
        {loadingSlots ? (
          <p style={{ opacity: 0.72 }} aria-live="polite">
            正在加载可预约时段…
          </p>
        ) : slots.length === 0 ? (
          <p style={{ opacity: 0.72 }}>
            这一天没有可预约的时段，请换一天。
          </p>
        ) : (
          <div
            role="radiogroup"
            aria-label="可预约时间"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(84px, 1fr))",
              gap: 8,
            }}
          >
            {slots.map((slot) => {
              const selected = slot.start === slotStart;
              return (
                <button
                  key={slot.start}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => setSlotStart(slot.start)}
                  style={{
                    padding: "12px 8px",
                    borderRadius: 8,
                    cursor: "pointer",
                    fontVariantNumeric: "tabular-nums",
                    background: selected ? "currentColor" : "transparent",
                    color: selected ? "Canvas" : "inherit",
                    border: selected ? "1px solid currentColor" : "1px solid rgba(128,128,128,.4)",
                  }}
                >
                  {formatTime(slot.start, eventType?.timezone ?? "Asia/Shanghai")}
                </button>
              );
            })}
          </div>
        )}
      </Step>

      <Step index={eventTypes.length > 1 ? 4 : 3} title="填写联系方式">
        <div style={{ display: "grid", gap: 16 }}>
          <Field label="姓名" required>
            <input
              required
              value={form.customer_name}
              onChange={(change) => setForm({ ...form, customer_name: change.target.value })}
              autoComplete="name"
              style={inputStyle}
            />
          </Field>

          <Field label="邮箱" required hint="确认邮件会发送到这个邮箱">
            <input
              required
              type="email"
              value={form.customer_email}
              onChange={(change) => setForm({ ...form, customer_email: change.target.value })}
              autoComplete="email"
              style={inputStyle}
            />
          </Field>

          <Field label="手机号（选填）">
            <input
              value={form.customer_phone}
              onChange={(change) => setForm({ ...form, customer_phone: change.target.value })}
              autoComplete="tel"
              style={inputStyle}
            />
          </Field>

          <Field label="想聊的问题（选填）">
            <textarea
              rows={3}
              value={form.customer_note}
              onChange={(change) => setForm({ ...form, customer_note: change.target.value })}
              style={{ ...inputStyle, resize: "vertical" }}
            />
          </Field>
        </div>
      </Step>

      {error && (
        <p role="alert" style={{ margin: 0, padding: 14, borderRadius: 8, background: "rgba(200,60,60,.12)" }}>
          {error}
        </p>
      )}

      <div style={{ display: "grid", gap: 12 }}>
        {eventType && (
          <p style={{ margin: 0, opacity: 0.8 }}>
            应付金额{" "}
            <strong style={{ fontSize: 20 }}>
              {formatFen(eventType.price_fen, eventType.currency)}
            </strong>
            {slotStart && (
              <span style={{ marginLeft: 8 }}>
                · {formatTime(slotStart, eventType.timezone)}（
                {eventType.timezone === "Asia/Shanghai" ? "北京时间" : eventType.timezone}）
              </span>
            )}
          </p>
        )}
        <button
          type="submit"
          disabled={!canSubmit}
          style={{
            padding: "16px 24px",
            fontSize: 16,
            fontWeight: 600,
            borderRadius: 8,
            border: "none",
            cursor: canSubmit ? "pointer" : "not-allowed",
            opacity: canSubmit ? 1 : 0.5,
            background: "currentColor",
            color: "Canvas",
          }}
        >
          {submitting ? "正在创建订单…" : "去支付"}
        </button>
      </div>
    </form>
  );
}

function Step({
  index,
  title,
  children,
}: {
  index: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <h2 style={{ fontSize: 18, margin: 0, display: "flex", gap: 10, alignItems: "center" }}>
        <span
          aria-hidden
          style={{
            display: "inline-grid",
            placeItems: "center",
            width: 26,
            height: 26,
            borderRadius: "50%",
            border: "1px solid currentColor",
            fontSize: 13,
          }}
        >
          {index}
        </span>
        {title}
      </h2>
      {children}
    </section>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span style={{ fontSize: 14, fontWeight: 500 }}>
        {label}
        {required && <span aria-hidden> *</span>}
      </span>
      {children}
      {hint && <span style={{ fontSize: 13, opacity: 0.66 }}>{hint}</span>}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "12px 14px",
  fontSize: 16,
  borderRadius: 8,
  border: "1px solid rgba(128,128,128,.5)",
  background: "transparent",
  color: "inherit",
};
