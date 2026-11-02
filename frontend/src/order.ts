// Turning the customer's order into a bill (the server then removes their saved order). Used by the Pay and UPI screens.
// allowShort: the owner saw "Stock will go to -2" and went ahead. If the stock ran out meanwhile (another phone
// sold it), the owner is asked once more; an employee is told to ask the owner.
// upiId: the shop's UPI ID shown in the QR, kept on the bill.
import { useState } from "react";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError, apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { isLoose, useCart } from "@/src/cart-context";
import { useToast } from "@/src/components/toast";
import { confirm } from "@/src/confirm";
import { askOwner } from "@/src/stock";
import type { Invoice } from "@/src/types";

export type PaymentMethod = Invoice["payment_method"];

export function useCreateBill() {
  const { t } = useTranslation();
  const cart = useCart();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { isOwner } = useAuth();
  const [busy, setBusy] = useState(false);

  const send = (method: PaymentMethod, allowShort: boolean, upiId?: string): Promise<Invoice> => apiPost("/invoices", {
    items: cart.items.map((i) => ({ product_id: isLoose(i.product_id) ? null : i.product_id, name: i.name, quantity: i.quantity, price: i.price,
      unit: i.unit ?? null })),
    customer_id: cart.customer!.id,
    discount: cart.discount,
    payment_method: method,
    gst_removed: cart.gstRemoved,
    ...(allowShort ? { allow_short: true } : {}),
    ...(method === "UPI" && upiId ? { upi_id: upiId } : {}),
  });

  async function create(method: PaymentMethod, allowShort = false, upiId?: string) {
    if (busy || !cart.items.length || !cart.customer) return;
    setBusy(true);
    try {
      await cart.beforeBill();
      let inv: Invoice;
      try {
        inv = await send(method, allowShort, upiId);
      } catch (e) {
        if (!(e instanceof ApiError) || e.code !== "out_of_stock") throw e;
        if (allowShort) throw e;
        const name = String(e.params.name ?? "");
        if (!isOwner) throw new Error(askOwner(name, Number(e.params.n) || 0, cart.items.find((i) => i.name === name)?.unit));
        if (!(await confirm(t("Not enough stock"), `${e.message} ${t("Bill anyway? The stock will go below zero.")}`, t("Bill anyway")))) return;
        inv = await send(method, true, upiId);
      }
      cart.billed();
      qc.invalidateQueries();
      // Close the order screens; the bill opens on top of Home.
      router.dismissTo("/(tabs)");
      router.push({ pathname: "/bill/[id]", params: { id: inv.id, done: "1" } });
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return { create, busy };
}
