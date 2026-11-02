// Turning the customer's order into a bill (the server then removes their saved order). Used by the Pay and UPI screens.
import { useState } from "react";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";

import { apiPost } from "@/src/api";
import { isLoose, useCart } from "@/src/cart-context";
import { useToast } from "@/src/components/toast";
import type { Invoice } from "@/src/types";

export type PaymentMethod = Invoice["payment_method"];

export function useCreateBill() {
  const cart = useCart();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  async function create(method: PaymentMethod) {
    if (busy || !cart.items.length || !cart.customer) return;
    setBusy(true);
    try {
      await cart.beforeBill();
      const inv: Invoice = await apiPost("/invoices", {
        items: cart.items.map((i) => ({ product_id: isLoose(i.product_id) ? null : i.product_id, name: i.name, quantity: i.quantity, price: i.price,
          unit: i.unit ?? null })),
        customer_id: cart.customer.id,
        discount: cart.discount,
        payment_method: method,
        gst_removed: cart.gstRemoved,
      });
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
