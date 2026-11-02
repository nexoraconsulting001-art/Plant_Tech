// UPI: the shop's pay QR with the amount filled in. "Money received" makes the bill.
// short=1: the owner already agreed on the Pay screen to sell below the stock count.
import { View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import QRCode from "react-native-qrcode-svg";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useCart } from "@/src/cart-context";
import { BottomBar, Button, Header, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency } from "@/src/format";
import { useCreateBill } from "@/src/order";
import { colors, radius, spacing } from "@/src/theme";
import type { Business } from "@/src/types";

function upiLink(upiId: string, payee: string, amount: number) {
  const q = [`pa=${encodeURIComponent(upiId)}`, `pn=${encodeURIComponent(payee)}`, `am=${amount.toFixed(2)}`, "cu=INR"];
  return `upi://pay?${q.join("&")}`;
}

export default function OrderUpi() {
  const { t } = useTranslation();
  const router = useRouter();
  const { short } = useLocalSearchParams<{ short?: string }>();
  const cart = useCart();
  const bill = useCreateBill();
  const biz = useQuery<Business>({ queryKey: ["business"], queryFn: () => apiGet("/business") });
  const b = biz.data;

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Pay by UPI")} />
      {!b ? <LoadingView /> : (
        <View style={{ flex: 1, alignItems: "center", padding: spacing.lg, gap: spacing.lg }}>
          <T v="body" color={colors.inkSoft} style={{ textAlign: "center" }}>{t("Show this code to the customer to scan with any UPI app.")}</T>
          <View style={{ padding: spacing.lg, borderRadius: radius.xl, borderWidth: 2, borderColor: colors.line, backgroundColor: colors.white }} testID="upi-qr">
            <QRCode value={upiLink(b.upi_id, b.name, cart.total)} size={240} color={colors.ink} backgroundColor={colors.white} />
          </View>
          <T v="amount">{formatCurrency(cart.total)}</T>
          <T v="small" style={{ textAlign: "center" }}>{t("To: {{shop}} · {{upi}}", { shop: b.name, upi: b.upi_id })}</T>
        </View>
      )}
      <BottomBar>
        <Button title={t("Payment received")} icon="check-bold" onPress={() => bill.create("UPI", short === "1")} loading={bill.busy}
          disabled={!b || !cart.count} testID="upi-received" />
        <Button title={t("Change payment method")} variant="outline" size="md" onPress={() => router.back()} />
      </BottomBar>
    </Screen>
  );
}
