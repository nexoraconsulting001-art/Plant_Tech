import { Pressable, View } from "react-native";
import { useRouter } from "expo-router";
import { useTranslation } from "react-i18next";

import { Badge, T } from "@/src/components/ui";
import { formatCurrency, formatQty, formatTime } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";
import type { BillRef } from "@/src/types";

export type OrderSummary = {
  id: string; invoice_number: string; customer_name: string; customer_phone: string; grand_total: number;
  payment_method: "Cash" | "UPI" | "Card"; created_at: string; created_by?: { id: string; name: string }; items_count: number;
  status?: "Paid" | "Cancelled"; replaced_by?: BillRef | null; // none from servers before corrections existed
};

// One bill in a list: who, how many items, when, how paid, amount. Tap opens the bill.
// A cancelled bill is greyed, its amount struck through (it counts nowhere), with what replaced it.
export function OrderRow({ o, showBy }: { o: OrderSummary; showBy?: boolean }) {
  const { t } = useTranslation();
  const router = useRouter();
  const n = o.items_count;
  const meta = [
    n === 1 ? t("1 item") : t("{{n}} items", { n: formatQty(n) }),
    formatTime(o.created_at),
    t(o.payment_method),
    showBy && o.created_by?.name ? t("by {{name}}", { name: o.created_by.name }) : "",
  ].filter(Boolean).join(" · ");
  const cancelled = o.status === "Cancelled";
  return (
    <Pressable testID={`order-${o.invoice_number}`} onPress={() => router.push({ pathname: "/bill/[id]", params: { id: o.id } })}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.lg, paddingVertical: spacing.md, minHeight: 68,
        borderRadius: radius.lg, backgroundColor: cancelled ? colors.page : colors.surface, borderWidth: 1, borderColor: colors.line,
        opacity: pressed ? 0.85 : 1 })}>
      <View style={{ flex: 1, minWidth: 0, gap: cancelled ? 2 : 0 }}>
        <T v="bold" color={cancelled ? colors.muted : undefined}>{o.customer_name || t("Walk-in customer")}</T>
        <T v="small">{meta}</T>
        {cancelled && (
          <View style={{ flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm }} testID={`order-cancelled-${o.invoice_number}`}>
            <Badge text={t("Cancelled")} tone="grey" />
            {!!o.replaced_by && <T v="caption">{t("Replaced by {{no}}", { no: o.replaced_by.invoice_number })}</T>}
          </View>
        )}
      </View>
      <T v="number" color={cancelled ? colors.muted : undefined} style={cancelled ? { textDecorationLine: "line-through" } : undefined}>
        {formatCurrency(o.grand_total)}
      </T>
    </Pressable>
  );
}
