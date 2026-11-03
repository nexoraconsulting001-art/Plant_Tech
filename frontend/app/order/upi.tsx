// UPI: the shop's pay QR with the amount filled in. "Money received" makes the bill.
// The QR starts with the shop's default UPI ID; "Change" picks another of its UPI IDs (anyone billing may; only the
// owner edits the list, in Shop details). The UPI ID used is kept on the bill.
// short=1: the owner already agreed on the Pay screen to sell below the stock count.
import { useState } from "react";
import { ScrollView, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import QRCode from "react-native-qrcode-svg";
import { useTranslation } from "react-i18next";

import { upiLink, upiList, useBusiness } from "@/src/business";
import { useCart } from "@/src/cart-context";
import { NetStatus } from "@/src/components/net-status";
import { CorrectionBanner } from "@/src/components/order-customer";
import { OptionRow, Sheet } from "@/src/components/sheet";
import { BottomBar, Button, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency } from "@/src/format";
import { useCreateBill } from "@/src/order";
import { colors, radius, spacing } from "@/src/theme";

export default function OrderUpi() {
  const { t } = useTranslation();
  const router = useRouter();
  const { short } = useLocalSearchParams<{ short?: string }>();
  const cart = useCart();
  const bill = useCreateBill();
  const biz = useBusiness();
  const b = biz.data;
  const list = upiList(b);
  const [chosenId, setChosenId] = useState<string>();
  const [picking, setPicking] = useState(false);
  const upi = list.find((u) => u.id === chosenId) ?? list[0]; // the default until changed
  const note = cart.customer ? `Bill for ${cart.customer.name}` : ""; // on the customer's payment and the shop's bank statement

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Pay by UPI")} />
      {!b ? <LoadingView /> : !upi ? (
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center", padding: spacing.xl, gap: spacing.md }}>
          <Icon name="qrcode-remove" size={44} color={colors.muted} />
          <T v="body" style={{ textAlign: "center" }}>{t("This shop has no UPI ID yet. The owner can add one in Shop details.")}</T>
        </View>
      ) : (
        <ScrollView contentContainerStyle={{ alignItems: "center", padding: spacing.lg, gap: spacing.lg }}>
          <NetStatus />
          {!!cart.correcting && <View style={{ alignSelf: "stretch" }}><CorrectionBanner /></View>}
          <T v="body" color={colors.inkSoft} style={{ textAlign: "center" }}>{t("Show this code to the customer to scan with any UPI app.")}</T>
          <View style={{ padding: spacing.lg, borderRadius: radius.xl, borderWidth: 2, borderColor: colors.line, backgroundColor: colors.white }} testID="upi-qr">
            <QRCode value={upiLink(upi.upi_id, b.name, cart.total, note)} size={240} color={colors.ink} backgroundColor={colors.white} />
          </View>
          <T v="amount">{formatCurrency(cart.total)}</T>
          <View style={{ alignSelf: "stretch", flexDirection: "row", alignItems: "center", gap: spacing.md, padding: spacing.lg,
            borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.page }} testID="upi-payee">
            <View style={{ flex: 1, minWidth: 0 }}>
              <T v="caption">{t("Paying to")}</T>
              <T v="bold">{b.name}</T>
              <T v="small" testID="upi-id">{[upi.upi_id, upi.label].filter(Boolean).join(" · ")}</T>
            </View>
            {list.length > 1 && (
              <Button title={t("Change")} icon="swap-horizontal" variant="outline" size="md" onPress={() => setPicking(true)} testID="upi-change" />
            )}
          </View>
        </ScrollView>
      )}
      <BottomBar>
        <Button title={t("Payment received")} icon="check-bold" onPress={() => bill.create("UPI", short === "1", upi?.upi_id)} loading={bill.busy}
          disabled={!b || !cart.count} testID="upi-received" />
        <Button title={t("Change payment method")} variant="outline" size="md" onPress={() => router.back()} />
      </BottomBar>
      <Sheet visible={picking} onClose={() => setPicking(false)} title={t("Pay to which UPI ID?")} testID="upi-sheet">
        {list.map((u) => (
          <OptionRow key={u.id} label={u.label || u.upi_id} sub={[u.label ? u.upi_id : "", u.id === list[0].id ? t("Default") : ""].filter(Boolean).join(" · ") || undefined}
            selected={u.id === upi?.id} icon="bank-outline" testID={`upi-option-${u.upi_id}`}
            onPress={() => { setChosenId(u.id); setPicking(false); }} />
        ))}
      </Sheet>
    </Screen>
  );
}
