// Payment: who is paying (the order's customer) and how. Cash and Card make the bill straight away;
// UPI first shows the shop's pay QR (when the shop has a UPI ID; the settings are fetched first if not loaded yet,
// so a quick tap never bills UPI without the QR).
// The stock of the order's products is checked again here. Not enough: the owner sees what each stock will go
// to and confirms before billing; an employee is asked to change the order or get the stock updated.
// Correcting a bill: what the old bill took counts as back in stock, and the bill made is the corrected one.
import { useState } from "react";
import { Pressable, ScrollView, View } from "react-native";
import { Redirect, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { businessQuery, upiList, useBusiness } from "@/src/business";
import { isLoose, useCart } from "@/src/cart-context";
import { confirm, notice } from "@/src/confirm";
import { CorrectionBanner, OrderCustomerBar } from "@/src/components/order-customer";
import { useToast } from "@/src/components/toast";
import { Button, Header, Icon, IconName, Screen, T } from "@/src/components/ui";
import { formatCurrency, plural } from "@/src/format";
import { PaymentMethod, useCreateBill } from "@/src/order";
import { askOwner, shortWarning } from "@/src/stock";
import { colors, radius, spacing } from "@/src/theme";
import type { Product } from "@/src/types";

const METHODS: { m: PaymentMethod; icon: IconName }[] = [
  { m: "Cash", icon: "cash" },
  { m: "UPI", icon: "qrcode" },
  { m: "Card", icon: "credit-card-outline" },
];

export default function OrderPay() {
  const { t } = useTranslation();
  const router = useRouter();
  const cart = useCart();
  // Opened with no customer (a reload, an old link): pick one first. Not when the order is cleared while open (Discard).
  const [openedWithout] = useState(() => !cart.customer);
  const bill = useCreateBill();
  const { isOwner } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const biz = useBusiness();
  const [checking, setChecking] = useState(false); // reading the shop's UPI IDs before opening the QR
  // how much of each product this order takes, against its stock right now
  const want = new Map<string, number>();
  for (const i of cart.items) if (!isLoose(i.product_id)) want.set(i.product_id, (want.get(i.product_id) ?? 0) + i.quantity);
  const ids = [...want.keys()].sort().join(",");
  const fresh = useQuery<Product[]>({ queryKey: ["products", "stock", ids], queryFn: () => apiGet(`/products?ids=${ids}`), enabled: !!ids,
    staleTime: 0 }); // always the latest count
  const stockOf = (p: Product) => p.stock + cart.returning(p.id); // a correction gives the old bill's quantity back first
  const short = (fresh.data ?? []).filter((p) => (want.get(p.id) ?? 0) > stockOf(p) + 1e-9)
    .map((p) => (isOwner ? shortWarning(p.name, stockOf(p), want.get(p.id)!, p.unit) : askOwner(p.name, stockOf(p), p.unit)));

  if (openedWithout) return <Redirect href={cart.correcting ? { pathname: "/order/customer", params: { change: "1" } } : "/order/customer"} />;

  async function pay(m: PaymentMethod) {
    if (short.length && !isOwner) {
      await notice(t("Not enough stock"), short.join("\n"));
      return;
    }
    if (short.length && !(await confirm(t("Sell below stock?"), `${short.join("\n")}\n\n${t("Bill anyway? The stock will go below zero.")}`,
      t("Bill anyway")))) return;
    if (m === "UPI") {
      let b = biz.data;
      if (!b) {
        setChecking(true);
        try {
          b = await qc.fetchQuery(businessQuery);
        } catch (e: any) {
          toast.show(e.message, "error");
          return;
        } finally {
          setChecking(false);
        }
      }
      if (upiList(b).length) {
        router.push({ pathname: "/order/upi", params: short.length ? { short: "1" } : {} });
        return;
      }
    }
    bill.create(m, short.length > 0);
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Payment")} />
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
        <CorrectionBanner />
        <View style={{ alignItems: "center", padding: spacing.lg, borderRadius: radius.xl, backgroundColor: colors.greenTint }}>
          <T v="label" color={colors.greenDark}>{t("Customer pays")}</T>
          <T v="amount" style={{ fontSize: 52, lineHeight: 58 }} testID="pay-total">{formatCurrency(cart.total)}</T>
          <T v="small" color={colors.greenDark}>
            {[plural(cart.count, "1 item", "{{n}} items"),
              cart.gstIncluded > 0.004 ? t("includes GST {{gst}}", { gst: formatCurrency(cart.gstIncluded) })
                : cart.gstRemoved ? t("no GST on this bill") : ""].filter(Boolean).join(" · ")}
          </T>
          {cart.correcting?.paid != null && (
            <T v="small" color={colors.greenDark} testID="pay-difference" style={{ marginTop: spacing.sm, textAlign: "center", fontWeight: "700" }}>
              {[t("Already paid {{amount}} on {{number}}", { amount: formatCurrency(cart.correcting.paid), number: cart.correcting.number }),
                cart.correcting.paid - cart.total > 0.004 ? t("Give back {{amount}}", { amount: formatCurrency(cart.correcting.paid - cart.total) })
                  : cart.total - cart.correcting.paid > 0.004 ? t("Collect {{amount}} more", { amount: formatCurrency(cart.total - cart.correcting.paid) })
                    : t("Nothing to give back or collect")].join(" · ")}
            </T>
          )}
        </View>

        <View style={{ gap: spacing.sm }}>
          <T v="label">{t("Customer")}</T>
          <OrderCustomerBar />
        </View>

        {short.length > 0 && (
          <View testID="pay-short" style={{ gap: spacing.sm, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1,
            borderColor: isOwner ? colors.amber : colors.redLine, backgroundColor: isOwner ? colors.amberTint : colors.redTint }}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Icon name="alert-circle-outline" size={22} color={isOwner ? colors.amberInk : colors.red} />
              <T v="bold" color={isOwner ? colors.amberInk : colors.red} style={{ flex: 1 }}>
                {isOwner ? t("Selling more than the stock count") : t("Not enough stock")}
              </T>
            </View>
            {short.map((s) => <T key={s} v="small" color={isOwner ? colors.amberInk : colors.red}>{s}</T>)}
            {!isOwner && <Button title={t("Change the order")} icon="pencil-outline" variant="outline" size="md" onPress={() => router.back()} testID="pay-change-order" />}
          </View>
        )}

        <View style={{ gap: spacing.md }}>
          <T v="label">{t("How is the customer paying?")}</T>
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            {METHODS.map(({ m, icon }) => (
              <Pressable key={m} testID={`pay-${m}`} disabled={bill.busy || checking || !cart.customer} onPress={() => pay(m)}
                style={({ pressed }) => ({ flex: 1, alignItems: "center", justifyContent: "center", gap: 6, minHeight: 112, borderRadius: radius.lg,
                  borderWidth: 2, borderColor: colors.green, backgroundColor: pressed ? colors.greenTint : colors.surface,
                  opacity: bill.busy || checking ? 0.5 : 1 })}>
                <Icon name={icon} size={40} color={colors.green} />
                <T v="bodyLarge" color={colors.greenDark}>{t(m)}</T>
              </Pressable>
            ))}
          </View>
          <T v="small" style={{ textAlign: "center" }}>
            {bill.busy ? t("Creating the bill") : cart.correcting ? t("Select the payment method. The corrected bill is created immediately.")
              : t("Select the payment method. The bill is created immediately.")}
          </T>
        </View>
      </ScrollView>
    </Screen>
  );
}
