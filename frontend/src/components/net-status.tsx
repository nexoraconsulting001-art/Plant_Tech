// The connection, where it matters for billing: a quiet line under the header of the order screens and the bill.
// - Offline (NetInfo through React Query's onlineManager): the order is kept on this phone; bills wait for the internet.
// - Online but this order's last save didn't land (cart.notSaved): "Not saved", tap to try again now.
// Shows nothing while all is well. order=false (the bill screen): only the offline line.
import { useSyncExternalStore } from "react";
import { Pressable, View } from "react-native";
import { onlineManager } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { useCart } from "@/src/cart-context";
import { Icon, T } from "@/src/components/ui";
import { colors, radius, spacing } from "@/src/theme";

const subscribe = (fn: () => void) => onlineManager.subscribe(fn);
export const useOnline = () => useSyncExternalStore(subscribe, () => onlineManager.isOnline());

export function NetStatus({ order = true }: { order?: boolean }) {
  const { t } = useTranslation();
  const online = useOnline();
  const cart = useCart();
  if (!online) {
    return (
      <View testID="net-offline" accessibilityRole="alert" style={{ alignSelf: "stretch", flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.md,
        borderRadius: radius.md, backgroundColor: colors.amberTint, borderWidth: 1, borderColor: colors.amber }}>
        <Icon name="wifi-off" size={22} color={colors.amberInk} />
        <View style={{ flex: 1, gap: 2 }}>
          <T v="bold" color={colors.amberInk}>{t("No internet")}</T>
          <T v="small" color={colors.amberInk}>
            {order ? t("The order is kept on this phone. Bills can be made when the internet is back.") : t("Bills can be sent when the internet is back.")}
          </T>
        </View>
      </View>
    );
  }
  if (!order || !cart.notSaved) return null;
  return (
    <Pressable testID="net-not-saved" accessibilityRole="button" accessibilityLabel={t("Not saved. Try again")} onPress={cart.saveNow}
      style={({ pressed }) => ({ alignSelf: "stretch", flexDirection: "row", alignItems: "center", gap: spacing.sm, paddingHorizontal: spacing.md, paddingVertical: spacing.sm,
        borderRadius: radius.md, backgroundColor: pressed ? colors.redLine : colors.redTint })}>
      <Icon name="cloud-alert-outline" size={20} color={colors.red} />
      <T v="small" color={colors.red} style={{ flex: 1 }}>{t("Not saved. The order is kept on this phone.")}</T>
      <T v="bold" color={colors.red} style={{ fontSize: 15 }}>{t("Try again")}</T>
    </Pressable>
  );
}
