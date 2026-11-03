// Turning the customer's order into a bill (the server then removes their saved order). Used by the Pay and UPI screens.
// allowShort: the owner saw "Stock will go to -2" and went ahead. If the stock ran out meanwhile (another phone
// sold it), the owner is asked once more; an employee is told to ask the owner.
// upiId: the shop's UPI ID shown in the QR, kept on the bill.
// Correcting a bill (cart.correcting): the same order goes to /invoices/{id}/correct, which cancels the old bill and
// makes this one with a new number. If the bill can no longer be corrected (cancelled meanwhile, or the days are
// over), the owner is told and the correction is dropped.
// Never billed twice: every try for this order (a retry after no answer, "Bill anyway") sends the same
// Idempotency-Key (cart.billKey), and the server answers a key it has seen with the bill it already made. Only a busy
// server (503) is retried by itself, by api.ts with the same key; if it stays busy, or after no answer, the order stays
// on screen and the worker taps the payment again. A 503 "busy" refused the bill before reading it; any other 503
// (the database was too slow) may have made it, so it is handled like no answer. If the order
// was changed meanwhile (an item added, another payment method) and the first try had been billed after all, the
// server says so (idempotency_mismatch): the worker is told, and that bill opens instead of the changed order.
// After the bill (afterBill): only what a bill changes is marked out of date, and the bill screen opens from the answer.
import { useState } from "react";
import { useRouter } from "expo-router";
import { QueryClient, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError, apiPost, noReply, serverBusy } from "@/src/api";
import { useAuth } from "@/src/auth";
import { isLoose, OrderCustomer, useCart } from "@/src/cart-context";
import { useToast } from "@/src/components/toast";
import { confirm, notice } from "@/src/confirm";
import { askOwner } from "@/src/stock";
import type { Business, Invoice } from "@/src/types";

export type PaymentMethod = Invoice["payment_method"];

// What a bill (made or cancelled) changes: today's numbers, the orders list and totals, reports, the customer, their
// line in customer searches, and the stock of what was sold. Marked out of date only, not reloaded now: each screen
// that shows them reloads when it is next shown (useRefreshOnFocus, or on opening), so a bill costs no extra calls.
export function billChanged(qc: QueryClient) {
  const stale = (queryKey: unknown[]) => qc.invalidateQueries({ queryKey, refetchType: "none" });
  // ["product"] and ["customer"]: every product and customer page, also those a correction moved away from
  for (const key of [["today"], ["orders"], ["orders-summary"], ["report"], ["employee-report"], ["customers"], ["customer"], ["products"],
    ["product"]]) stale(key);
  // customer searches; the recent list ("") is kept up to date by afterBill instead
  qc.invalidateQueries({ queryKey: ["customer-find"], predicate: (q) => q.queryKey[1] !== "", refetchType: "none" });
}

// A new bill (replaced: the bill a correction cancelled).
export function afterBill(qc: QueryClient, inv: Invoice, replaced?: string) {
  billChanged(qc);
  if (replaced) qc.invalidateQueries({ queryKey: ["invoice", replaced], refetchType: "none" }); // now Cancelled
  // The bill screen shows this bill at once, with the shop's lines as GET /invoices/{id} adds them (from Shop details
  // when they are loaded; else the bill is loaded once more for them).
  const biz = qc.getQueryData<Business>(["business"]);
  qc.setQueryData<Invoice>(["invoice", inv.id], biz ? { ...inv, shop: { name: biz.name ?? "", address: biz.address ?? "", phone: biz.phone ?? "" } } : inv,
    { updatedAt: biz ? Date.now() : 0 });
  // "Recent customers" on the New order screen: this customer moves to the top.
  const cid = inv.customer_id;
  if (cid) {
    qc.setQueryData<OrderCustomer[]>(["customer-find", ""], (list) => {
      if (!list) return list;
      const was = list.find((c) => c.id === cid);
      const me = { id: cid, name: inv.customer_name, phone: inv.customer_phone, purchase_count: (was?.purchase_count ?? 0) + (replaced ? 0 : 1) };
      return [me, ...list.filter((c) => c.id !== cid)].slice(0, Math.max(list.length, 1));
    });
  }
}

export function useCreateBill() {
  const { t } = useTranslation();
  const cart = useCart();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { isOwner } = useAuth();
  const [busy, setBusy] = useState(false);

  const fixing = cart.correcting;
  const send = (method: PaymentMethod, allowShort: boolean, key: string, upiId?: string): Promise<Invoice> =>
    apiPost(fixing ? `/invoices/${fixing.id}/correct` : "/invoices", {
      items: cart.items.map((i) => ({ product_id: isLoose(i.product_id) ? null : i.product_id, name: i.name, quantity: i.quantity, price: i.price,
        unit: i.unit ?? null })),
      customer_id: cart.customer!.id,
      discount: cart.discount,
      payment_method: method,
      gst_removed: cart.gstRemoved,
      ...(allowShort ? { allow_short: true } : {}),
      ...(method === "UPI" && upiId ? { upi_id: upiId } : {}),
    }, { headers: { "Idempotency-Key": key } });

  async function create(method: PaymentMethod, allowShort = false, upiId?: string) {
    if (busy || !cart.items.length || !cart.customer) return;
    setBusy(true);
    const key = cart.billKey();
    try {
      await cart.beforeBill();
      let inv: Invoice;
      try {
        inv = await send(method, allowShort, key, upiId);
      } catch (e) {
        if (!(e instanceof ApiError) || e.code !== "out_of_stock") throw e;
        if (allowShort) throw e;
        const name = String(e.params.name ?? "");
        if (!isOwner) throw new Error(askOwner(name, Number(e.params.n) || 0, cart.items.find((i) => i.name === name)?.unit));
        if (!(await confirm(t("Not enough stock"), `${e.message} ${t("Bill anyway? The stock will go below zero.")}`, t("Bill anyway")))) {
          cart.billFailed(false);
          return;
        }
        inv = await send(method, true, key, upiId);
      }
      cart.billed();
      afterBill(qc, inv, fixing?.id);
      // Close the order screens; the bill opens on top of Home.
      router.dismissTo("/(tabs)");
      router.push({ pathname: "/bill/[id]", params: { id: inv.id, done: "1" } });
    } catch (e: any) {
      const maybeBilled = noReply(e) || (serverBusy(e) && e.code !== "busy");
      cart.billFailed(maybeBilled);
      if (e instanceof ApiError && e.code === "idempotency_mismatch" && e.params.invoice_id) {
        await notice(t("Already billed"), e.message);
        cart.billed();
        billChanged(qc);
        if (fixing) qc.invalidateQueries({ queryKey: ["invoice", fixing.id], refetchType: "none" });
        router.dismissTo("/(tabs)");
        router.push({ pathname: "/bill/[id]", params: { id: String(e.params.invoice_id), done: "1" } });
        return;
      }
      if (fixing && e instanceof ApiError && (e.code === "bill_cancelled" || e.code === "bill_locked")) {
        await notice(t("This bill can't be corrected"), e.message);
        await cart.discard();
        router.dismissTo("/(tabs)");
        return;
      }
      // No answer: the bill may have been made. Trying again is safe (same key) and shows it if it was.
      if (noReply(e)) {
        await notice(t("No answer from the server"), t("The bill may or may not have been made. Tap the payment again: it will not be billed twice."));
      } else if (maybeBilled) {
        await notice(t("The server is busy"), t("The bill may or may not have been made. Tap the payment again: it will not be billed twice."));
      } else toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return { create, busy };
}
