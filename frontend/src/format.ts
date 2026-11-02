import i18n from "i18next";

import { unitShort } from "@/src/units";

const locale = () => (i18n.language === "hi" ? "hi-IN" : "en-IN");

// ₹1,23,456 (no paise when whole), ₹22.88
export function formatCurrency(amount: number | null | undefined): string {
  const n = Number(amount) || 0;
  const whole = Math.abs(n - Math.round(n)) < 0.005;
  return "₹" + n.toLocaleString(locale(), whole ? { maximumFractionDigits: 0 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// 40, 1.5, 0.25 (up to 3 decimals, for kg / litre / metre; no trailing zeros)
export function formatQty(n: number): string {
  return String(Math.round((Number(n) || 0) * 1000) / 1000);
}

// "40 pcs", "1.5 kg"; just the number for lines saved before units existed
export function formatQtyUnit(n: number, unit?: string | null): string {
  return unit ? `${formatQty(n)} ${unitShort(unit)}` : formatQty(n);
}

// "1 item" / "3 items": plural(n, "1 item", "{{n}} items")
export function plural(n: number, one: string, many: string): string {
  return n === 1 ? i18n.t(one) : i18n.t(many, { n: formatQty(n) });
}

export function formatTime(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString(locale(), { hour: "numeric", minute: "2-digit" }).toLowerCase();
}

export function formatDate(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return i18n.t("Today, {{time}}", { time: formatTime(iso) });
  return d.toLocaleDateString(locale(), { day: "numeric", month: "short" });
}

// "YYYY-MM-DD" of the India day, `daysAgo` days back (the server files bills under this key).
export function todayKey(daysAgo = 0): string {
  const d = new Date(Date.now() + 5.5 * 3600 * 1000 - daysAgo * 86400 * 1000);
  return d.toISOString().slice(0, 10);
}

// Chip label for a day key: "Today", "Yesterday", else "Mon 13".
export function dayLabel(key: string, daysAgo: number): string {
  if (daysAgo === 0) return i18n.t("Today");
  if (daysAgo === 1) return i18n.t("Yesterday");
  const d = new Date(`${key}T12:00:00+05:30`);
  return d.toLocaleDateString(locale(), { weekday: "short", day: "numeric" });
}

// "Thursday, 16 Oct"
export function formatToday(): string {
  return new Date().toLocaleDateString(locale(), { weekday: "long", day: "numeric", month: "short" });
}

// '+919811122334' -> '+91 98111 22334'
export function formatPhone(phone?: string | null): string {
  if (!phone) return "";
  const m = phone.match(/^\+91(\d{5})(\d{5})$/);
  return m ? `+91 ${m[1]} ${m[2]}` : phone;
}

// Only the 10 digits a person types after +91; null if not an Indian mobile.
export function normalizePhone(raw: string): string | null {
  let d = raw.replace(/\D/g, "");
  if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
  if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
  return /^[6-9]\d{9}$/.test(d) ? d : null;
}
