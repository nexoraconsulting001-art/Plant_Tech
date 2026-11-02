// The shop's settings that the order screens need: its UPI IDs (one is the default) and whether bills go to
// WhatsApp by themselves. Any role may read them; only the owner changes them (Shop details).
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/src/api";
import type { Business, UpiEntry } from "@/src/types";

export const businessQuery = { queryKey: ["business"], queryFn: () => apiGet("/business") as Promise<Business> };
export const useBusiness = () => useQuery<Business>(businessQuery);

// name@bank, e.g. greenhouse@okaxis or 9811122334@ybl (the server checks the same)
const UPI_RE = /^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$/;
export const isUpiId = (v: string) => UPI_RE.test(v.trim());

// The list, default first. A server from before the list sends only upi_id.
export function upiList(b?: Business | null): UpiEntry[] {
  if (!b) return [];
  const list = b.upi_ids ?? (b.upi_id ? [{ id: "default", upi_id: b.upi_id, label: "" }] : []);
  const def = list.find((u) => u.id === b.default_upi) ?? list[0];
  return def ? [def, ...list.filter((u) => u !== def)] : [];
}

// On unless the owner turned it off
export const whatsappBill = (b?: Business | null) => b?.whatsapp_bill !== false;

// UPI pay link for the QR. tn is the note the payer's app and the shop's bank statement show.
export function upiLink(upiId: string, payee: string, amount: number, note: string) {
  const q = [`pa=${encodeURIComponent(upiId)}`, `pn=${encodeURIComponent(payee)}`, `am=${amount.toFixed(2)}`, "cu=INR"];
  if (note) q.push(`tn=${encodeURIComponent(note.slice(0, 50))}`);
  return `upi://pay?${q.join("&")}`;
}

export const newUpiKey = () => `u${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
