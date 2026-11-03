import { useState } from "react";
import { View } from "react-native";
import { useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet, apiPut } from "@/src/api";
import { isUpiId } from "@/src/business";
import { SetupFrame } from "@/src/components/setup-frame";
import { useToast } from "@/src/components/toast";
import { Field, LoadingView, Screen, T, Toggle } from "@/src/components/ui";
import { colors, radius, spacing } from "@/src/theme";
import type { Business } from "@/src/types";

const GSTIN = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$/;

export default function SetupBilling() {
  const biz = useQuery<Business>({ queryKey: ["business"], queryFn: () => apiGet("/business") });
  if (!biz.data) return <Screen bg={colors.surface}><LoadingView /></Screen>;
  return <BillingStep biz={biz.data} />;
}

function BillingStep({ biz }: { biz: Business }) {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const [upi, setUpi] = useState(biz.upi_id || "");
  const [gst, setGst] = useState(!!biz.gst_registered);
  const [gstin, setGstin] = useState(biz.gstin || "");
  const [profit, setProfit] = useState(String(biz.profit_percent ?? 60));
  const [whatsapp, setWhatsapp] = useState(biz.whatsapp_bill !== false);
  const [busy, setBusy] = useState(false);

  const gstinBad = gst && gstin.length > 0 && !GSTIN.test(gstin.toUpperCase());
  const profitBad = profit === "" || Number(profit) > 1000;
  const upiBad = !!upi.trim() && !isUpiId(upi);

  async function next() {
    setBusy(true);
    try {
      await apiPut("/business", { upi_id: upi.trim(), gst_registered: gst, gstin: gst ? gstin.toUpperCase() : "",
        profit_percent: Number(profit), whatsapp_bill: whatsapp }); // upi_id: the default; more are added in Shop details
      await qc.invalidateQueries({ queryKey: ["business"] });
      router.push("/setup/categories");
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <SetupFrame step={3} title={t("Bills and payments")} subtitle={t("You can change these later in Shop details.")}
      onNext={next} busy={busy} nextDisabled={gstinBad || (gst && !gstin) || profitBad || upiBad}>
      <Field label={t("UPI ID (shows a payment QR code to customers)")} value={upi} onChangeText={setUpi} autoCapitalize="none"
        placeholder="shopname@okaxis" testID="setup-upi" error={upiBad ? t("Enter a UPI ID like name@bank") : undefined} />
      <View style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
        <Toggle title={t("GST registered")} subtitle={t("Turn on to show GST on your bills. Leave off if your shop is not registered.")} value={gst} onChange={setGst} testID="setup-gst" />
        {gst && (
          <>
            <Field label={t("GST number")} value={gstin} onChangeText={(v) => setGstin(v.toUpperCase())} autoCapitalize="characters"
              maxLength={15} placeholder="06ABCDE1234F1Z4" error={gstinBad ? t("This does not look like a valid GST number") : undefined} />
            <T v="small" style={{ color: colors.inkSoft }}>
              {t("The GST % is set per category in the next step. Prices you type include GST.")}
            </T>
          </>
        )}
      </View>
      <View style={{ padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
        <Toggle title={t("Send bill on WhatsApp")} value={whatsapp} onChange={setWhatsapp} testID="setup-whatsapp"
          subtitle={whatsapp ? t("WhatsApp opens with the bill as soon as it is made.") : t("Off: send it from the bill only when the customer asks.")} />
      </View>
      <Field label={t("Profit margin on cost (%)")} hint={t("Used to price new products from supplier bills. For example, 60% turns a cost of ₹100 into ₹160.")} value={profit}
        onChangeText={(v) => setProfit(v.replace(/[^0-9.]/g, ""))} keyboardType="decimal-pad" maxLength={6} testID="setup-profit"
        error={profitBad ? t("Enter a percentage from 0 to 1000") : undefined} />
    </SetupFrame>
  );
}
