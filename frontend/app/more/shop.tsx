// Shop details printed on every bill: name, logo, address, phone, GST. Also the shop's UPI IDs for the pay QR
// (one is the default) and whether bills open in WhatsApp by themselves.
import { useState } from "react";
import { View } from "react-native";
import { useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiGet, apiPut, uploadImage } from "@/src/api";
import { useAuth } from "@/src/auth";
import { newUpiKey } from "@/src/business";
import { useToast } from "@/src/components/toast";
import { UpiEditor } from "@/src/components/upi-editor";
import { BottomBar, Button, Field, Header, LoadingView, PhoneField, Screen, T, Thumb, Toggle } from "@/src/components/ui";
import { normalizePhone } from "@/src/format";
import { pickFromGallery } from "@/src/hooks/useImageCapture";
import { colors, radius, spacing } from "@/src/theme";
import type { Business, UpiEntry } from "@/src/types";

const GSTIN = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$/;

export default function ShopDetails() {
  const { t } = useTranslation();
  const q = useQuery<Business>({ queryKey: ["business"], queryFn: () => apiGet("/business") });
  if (!q.data) return <Screen bg={colors.surface}><Header title={t("Shop details")} /><LoadingView /></Screen>;
  return <ShopForm initial={q.data} />;
}

function ShopForm({ initial }: { initial: Business }) {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { refresh } = useAuth();
  const [b, setB] = useState<Business>(initial);
  const [phone, setPhone] = useState(normalizePhone(initial.phone || "") ?? "");
  const [profit, setProfit] = useState(String(initial.profit_percent ?? 60));
  const [upis, setUpis] = useState<{ list: UpiEntry[]; def: string | null }>(() => {
    const list = initial.upi_ids ?? (initial.upi_id ? [{ id: newUpiKey(), upi_id: initial.upi_id, label: "" }] : []);
    return { list, def: initial.default_upi ?? list[0]?.id ?? null };
  });
  const [whatsapp, setWhatsapp] = useState(initial.whatsapp_bill !== false);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const set = (patch: Partial<Business>) => setB((o) => ({ ...o, ...patch }));
  const gstinBad = b.gst_registered && !GSTIN.test(b.gstin || "");
  const phoneBad = !!phone && !normalizePhone(phone);
  const profitBad = profit === "" || Number(profit) > 1000;

  async function logo() {
    const out = await pickFromGallery({ hint: t("Drag the corners to fit your logo") });
    if (out.status !== "ok") return;
    setUploading(true);
    try {
      set({ logo_path: await uploadImage(out.result.base64) });
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setUploading(false);
    }
  }

  async function save() {
    setBusy(true);
    try {
      await apiPut("/business", { name: b.name.trim(), address: b.address, phone: phone ? `+91${phone}` : "",
        upi_ids: upis.list, default_upi: upis.def, whatsapp_bill: whatsapp, gst_registered: b.gst_registered, gstin: b.gst_registered ? b.gstin : "", logo_path: b.logo_path, footer_note: b.footer_note,
        profit_percent: Number(profit) });
      await Promise.all([refresh(), qc.invalidateQueries()]);
      toast.show(t("Saved"), "success");
      router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Shop details")} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
          <Thumb path={b.logo_path} size={80} icon="store" contain />
          <Button title={b.logo_path ? t("Change logo") : t("Add logo")} icon="image-outline" variant="outline" size="md" style={{ flex: 1 }}
            onPress={logo} loading={uploading} />
        </View>
        <Field label={t("Shop name (printed on every bill)")} value={b.name} onChangeText={(name) => set({ name })} autoCapitalize="words" testID="shop-name" />
        <Field label={t("Shop address")} value={b.address} onChangeText={(address) => set({ address })} multiline />
        <PhoneField label={t("Shop phone")} value={phone} onChangeText={setPhone} error={phoneBad && phone.length === 10 ? t("Enter a 10-digit mobile number") : undefined} />
        <UpiEditor list={upis.list} defaultId={upis.def} onChange={(list, def) => setUpis({ list, def })} />
        <View style={{ padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
          <Toggle title={t("Send bill on WhatsApp")} value={whatsapp} onChange={setWhatsapp} testID="shop-whatsapp"
            subtitle={whatsapp ? t("WhatsApp opens with the bill as soon as it is made.") : t("Off: send it from the bill only when the customer asks.")} />
        </View>
        <View style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
          <Toggle title={t("GST registered")} subtitle={t("Turn on to show GST on your bills. Leave off if your shop is not registered.")} value={b.gst_registered}
            onChange={(gst_registered) => set({ gst_registered })} testID="shop-gst" />
          {b.gst_registered && (
            <>
              <Field label={t("GST number")} value={b.gstin} onChangeText={(v) => set({ gstin: v.toUpperCase() })} autoCapitalize="characters"
                maxLength={15} error={b.gstin && gstinBad ? t("This does not look like a valid GST number") : undefined} />
              <T v="small">{t("Set the GST % for each category in Categories.")}</T>
            </>
          )}
        </View>
        <Field label={t("Profit margin on cost (%)")} hint={t("Used to price new products from supplier bills. For example, 60% turns a cost of ₹100 into ₹160.")} value={profit}
          onChangeText={(v) => setProfit(v.replace(/[^0-9.]/g, ""))} keyboardType="decimal-pad" maxLength={6} testID="shop-profit"
          error={profitBad ? t("Enter a percentage from 0 to 1000") : undefined} />
        <Field label={t("Note at the bottom of the bill")} value={b.footer_note} onChangeText={(footer_note) => set({ footer_note })}
          placeholder={t("Thank you, visit again!")} maxLength={200} />
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Save")} icon="content-save" onPress={save} loading={busy} disabled={!b.name.trim() || gstinBad || phoneBad || profitBad || uploading} testID="shop-save" />
      </BottomBar>
    </Screen>
  );
}
