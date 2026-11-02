// "Anita Sharma · +91 98111 22334   Change" — the order's customer, shown on every order step.
// DiscardOrder: the small "Discard" in the order headers; a discarded order is not kept. While correcting a bill
// it stops the correction instead (the bill stays as it was).
// CorrectionBanner: "Correcting INV-0012" on the order screens while a bill is being corrected.
import { Pressable, View } from "react-native";
import { useRouter } from "expo-router";
import { useTranslation } from "react-i18next";

import { useCart } from "@/src/cart-context";
import { confirm } from "@/src/confirm";
import { Icon, T } from "@/src/components/ui";
import { formatDate, formatPhone } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";

export function OrderCustomerBar() {
  const { t } = useTranslation();
  const router = useRouter();
  const { customer } = useCart();
  if (!customer) return null;
  return (
    <Pressable testID="order-customer" accessibilityRole="button" accessibilityLabel={t("Change customer")}
      onPress={() => router.push({ pathname: "/order/customer", params: { change: "1" } })}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm, minHeight: 56,
        borderRadius: radius.md, backgroundColor: pressed ? colors.greenTint : colors.page, borderWidth: 1, borderColor: colors.line })}>
      <Icon name="account" size={24} color={colors.green} />
      <View style={{ flex: 1 }}>
        <T v="bold">{customer.name}</T>
        <T v="small">{formatPhone(customer.phone)}</T>
      </View>
      <T v="bold" color={colors.green}>{t("Change")}</T>
    </Pressable>
  );
}

export function DiscardOrder() {
  const { t } = useTranslation();
  const router = useRouter();
  const cart = useCart();
  if (!cart.customer && !cart.correcting) return null;
  async function discard() {
    const yes = cart.correcting
      ? await confirm(t("Stop correcting?"), t("Bill {{no}} stays as it was. The changes made here are not kept.", { no: cart.correcting.number }),
        t("Stop correcting"), true)
      : await confirm(t("Discard this order?"), t("The items are removed and the order is not kept. The customer stays saved."), t("Discard"), true);
    if (!yes) return;
    await cart.discard();
    router.dismissTo("/(tabs)");
  }
  return (
    <Pressable testID="order-discard" accessibilityRole="button" accessibilityLabel={t("Discard order")} onPress={discard}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.xs, minHeight: 44, paddingHorizontal: spacing.md, marginRight: 6,
        borderRadius: radius.md, backgroundColor: pressed ? colors.redTint : "transparent" })}>
      <Icon name="trash-can-outline" size={20} color={colors.red} />
      <T v="bold" color={colors.red} style={{ fontSize: 15 }}>{cart.correcting ? t("Stop") : t("Discard")}</T>
    </Pressable>
  );
}

export function CorrectionBanner() {
  const { t } = useTranslation();
  const { correcting } = useCart();
  if (!correcting) return null;
  return (
    <View testID="correcting-banner" style={{ flexDirection: "row", gap: spacing.md, padding: spacing.md, borderRadius: radius.md,
      backgroundColor: colors.blueTint, borderWidth: 1, borderColor: colors.blue }}>
      <Icon name="file-document-edit-outline" size={24} color={colors.blueInk} />
      <View style={{ flex: 1, gap: 2 }}>
        <T v="bold" color={colors.blueInk}>{t("Correcting {{no}}", { no: correcting.number })}</T>
        <T v="small" color={colors.blueInk}>
          {t("On payment, {{no}} is cancelled and the corrected bill gets a new number.", { no: correcting.number })}
          {correcting.until ? ` ${t("Possible until {{date}}.", { date: formatDate(correcting.until) })}` : ""}
        </T>
      </View>
    </View>
  );
}
