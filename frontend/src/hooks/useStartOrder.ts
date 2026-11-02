// Start (or pick up) a customer's order and go straight to adding items. Used by the New order picker and
// the Customers tab. If the customer has a saved order (items added earlier, by anyone in the shop), it opens
// with those items and the order is shown on top. The order on screen before stays saved for its customer.
import { useRouter } from "expo-router";
import { useTranslation } from "react-i18next";

import { OrderCustomer, useCart } from "@/src/cart-context";
import { useToast } from "@/src/components/toast";
import { plural } from "@/src/format";

export function useStartOrder() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const cart = useCart();
  // replace: the screen it is called from is done (a picker or a form), so Back skips it.
  return async (c: OrderCustomer, replace = false) => {
    const saved = await cart.openFor(c);
    if (replace) router.replace("/order/scan");
    else router.push("/order/scan");
    if (saved) {
      toast.show(t("{{name}}'s saved order: {{items}}", { name: c.name, items: plural(saved, "1 item", "{{n}} items") }), "success");
      router.push("/order/cart");
    }
  };
}
