// A customer: start an order for them, call or WhatsApp them, their address, what they've bought. Edit in the header.
import { useState } from "react";
import { Linking, Pressable, ScrollView, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { Button, ErrorView, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatDate, formatPhone, formatQty } from "@/src/format";
import { useStartOrder } from "@/src/hooks/useStartOrder";
import { colors, radius, spacing, touch } from "@/src/theme";
import type { Customer, Invoice } from "@/src/types";

type PastBill = {
  id: string; invoice_number: string; grand_total: number; created_at: string; payment_method?: Invoice["payment_method"];
  items: { name: string; quantity: number }[];
  status?: Invoice["status"]; // a cancelled bill is listed greyed; it is not in the customer's totals
};
const off = (b: PastBill) => b.status === "Cancelled";

export default function CustomerDetail() {
  const { t } = useTranslation();
  const router = useRouter();
  const { can } = useAuth();
  const startOrder = useStartOrder();
  const [starting, setStarting] = useState(false);
  const { id } = useLocalSearchParams<{ id: string }>();
  const q = useQuery<Customer & { invoices: PastBill[] }>({ queryKey: ["customer", id], queryFn: () => apiGet(`/customers/${id}`) });
  const c = q.data;
  const digits = c?.phone.replace(/\D/g, "") ?? "";
  const edit = (focus?: "address") => router.push({ pathname: "/customers/edit", params: focus ? { id, focus } : { id } });

  async function newOrder() {
    if (!c || starting) return;
    setStarting(true);
    try {
      await startOrder({ id: c.id, name: c.name, phone: c.phone, purchase_count: c.purchase_count });
    } finally {
      setStarting(false);
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Customer")} right={c ? (
        <Pressable onPress={() => edit()} testID="customer-edit"
          style={{ flexDirection: "row", alignItems: "center", gap: 6, minHeight: touch.min, paddingHorizontal: spacing.lg }}>
          <Icon name="pencil-outline" size={22} color={colors.green} />
          <T v="bold" color={colors.green}>{t("Edit")}</T>
        </Pressable>
      ) : undefined} />
      {!c ? (q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />) : (
        <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
          <View style={{ alignItems: "center", gap: spacing.xs }}>
            <View style={{ width: 80, height: 80, borderRadius: 40, backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
              <T v="heading" color={colors.greenDark}>{c.name.charAt(0).toUpperCase()}</T>
            </View>
            <T v="title">{c.name}</T>
            {!!c.phone && <T v="body" color={colors.inkSoft}>{formatPhone(c.phone)}</T>}
          </View>
          {can("sell") && (
            <Button title={t("New order")} icon="cart-plus" onPress={newOrder} loading={starting} testID="customer-new-order" />
          )}
          {!!c.phone && (
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <Button title={t("Call")} icon="phone" variant="outline" size="md" style={{ flex: 1 }} onPress={() => Linking.openURL(`tel:${c.phone}`)} />
              <Button title={t("WhatsApp")} icon="whatsapp" variant="whatsapp" size="md" style={{ flex: 1 }}
                onPress={() => Linking.openURL(`https://wa.me/${digits}`)} />
            </View>
          )}
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
              <T v="label">{t("Total bought")}</T>
              <T v="title">{formatCurrency(c.total_purchases)}</T>
            </View>
            <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
              <T v="label">{t("Visits")}</T>
              <T v="title">{c.purchase_count}</T>
            </View>
          </View>
          {c.address ? (
            <View style={{ flexDirection: "row", gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}
              testID="customer-address">
              <Icon name="map-marker-outline" size={24} color={colors.green} />
              <View style={{ flex: 1, gap: 2 }}>
                <T v="label">{t("Address")}</T>
                <T v="body">{c.address}</T>
              </View>
            </View>
          ) : (
            <Button title={t("Add address")} icon="map-marker-plus-outline" variant="dashed" size="md" onPress={() => edit("address")}
              testID="customer-add-address" />
          )}
          {!!c.notes && <T v="small">{c.notes}</T>}

          <View style={{ gap: spacing.sm }}>
            <T v="label" style={{ fontSize: 18 }}>{t("Past bills")}</T>
            {c.invoices.length ? c.invoices.map((b) => (
              <Pressable key={b.id} onPress={() => router.push({ pathname: "/bill/[id]", params: { id: b.id } })}
                style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md, minHeight: 68, borderRadius: radius.lg,
                  borderWidth: 1, borderColor: colors.line, backgroundColor: off(b) ? colors.page : undefined, opacity: pressed ? 0.85 : 1 })}>
                <View style={{ flex: 1, minWidth: 0 }}>
                  <T v="bold">{formatDate(b.created_at)}</T>
                  <T v="small">
                    {b.items.map((i) => (i.quantity > 1 ? `${i.name} × ${formatQty(i.quantity)}` : i.name)).join(", ")}
                  </T>
                </View>
                <View style={{ alignItems: "flex-end" }}>
                  <T v="number" color={off(b) ? colors.muted : undefined} style={off(b) ? { textDecorationLine: "line-through" } : undefined}>
                    {formatCurrency(b.grand_total)}
                  </T>
                  <T v="caption" testID={`customer-bill-${b.invoice_number}`} color={off(b) ? colors.red : undefined}>
                    {[b.invoice_number, off(b) ? t("Cancelled") : b.payment_method ? t(b.payment_method) : ""].filter(Boolean).join(" · ")}
                  </T>
                </View>
              </Pressable>
            )) : <T v="small">{t("No bills yet")}</T>}
          </View>
        </ScrollView>
      )}
    </Screen>
  );
}
