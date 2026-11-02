// A bill. Right after payment (done=1) it leads with "paid" and Send on WhatsApp.
// WhatsApp: straight to the customer's chat if we have their number, else WhatsApp asks for a contact.
import { useState } from "react";
import { Linking, Pressable, ScrollView, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import i18n from "i18next";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { BillFileResult, downloadBillPdf, shareBillPdf } from "@/src/bill-file";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, ErrorView, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatDate, formatPhone, formatQtyUnit, formatTime } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";
import type { Invoice } from "@/src/types";

export default function Bill() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const { user, isOwner } = useAuth();
  const { id, done } = useLocalSearchParams<{ id: string; done?: string }>();
  const justPaid = done === "1";
  const q = useQuery<Invoice>({ queryKey: ["invoice", id], queryFn: () => apiGet(`/invoices/${id}`) });
  const [busy, setBusy] = useState<"" | "wa" | "pdf" | "download">("");
  const inv = q.data;
  const home = () => (router.canGoBack() ? router.back() : router.replace("/(tabs)"));

  async function run(kind: "wa" | "pdf" | "download", fn: () => Promise<unknown>) {
    setBusy(kind);
    try {
      await fn();
    } catch (e: any) {
      toast.show(e.message || t("Something went wrong"), "error");
    } finally {
      setBusy("");
    }
  }

  const whatsapp = () => run("wa", async () => {
    const { url } = await apiGet(`/invoices/${id}/share`);
    const greet = inv!.customer_phone && inv!.customer_name !== "Walk-in customer" ? ` ${inv!.customer_name}` : "";
    const en = i18n.getFixedT("en"); // customers get the message in English, like the bill
    const text = en("Namaste{{name}}! Your bill from {{shop}}: {{amount}} ({{no}}).\nSee the bill: {{url}}", {
      name: greet, shop: user?.business?.name ?? "", amount: formatCurrency(inv!.grand_total), no: inv!.invoice_number, url });
    const digits = inv!.customer_phone.replace(/\D/g, "");
    await Linking.openURL(`https://wa.me/${digits}?text=${encodeURIComponent(text)}`);
  });

  const pdf = () => run("pdf", () => shareBillPdf(id, inv!.invoice_number));

  const download = () => run("download", async () => {
    const r: BillFileResult = await downloadBillPdf(id, inv!.invoice_number);
    if (r === "saved") toast.show(t("Saved Bill {{no}}.pdf", { no: inv!.invoice_number }), "success");
  });

  if (!inv) {
    return (
      <Screen bg={colors.surface}>
        <Header title={t("Bill")} onBack={home} />
        {q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />}
      </Screen>
    );
  }

  const who = inv.customer_phone
    ? t("To {{name}} · {{phone}}", { name: inv.customer_name.split(" ")[0], phone: formatPhone(inv.customer_phone) })
    : t("Choose the customer in WhatsApp");

  return (
    <Screen bg={colors.surface}>
      {justPaid ? <View style={{ height: 8 }} /> : <Header title={t("Bill {{no}}", { no: inv.invoice_number })} onBack={home} />}
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
        {justPaid && (
          <View style={{ alignItems: "center", gap: spacing.xs, paddingVertical: spacing.sm }} testID="bill-paid">
            <View style={{ width: 88, height: 88, borderRadius: 44, backgroundColor: colors.green, alignItems: "center", justifyContent: "center", marginBottom: 6 }}>
              <Icon name="check-bold" size={50} color={colors.white} />
            </View>
            <T v="amount">{t("{{amount}} paid", { amount: formatCurrency(inv.grand_total) })}</T>
            <T v="body" color={colors.inkSoft}>{[t(inv.payment_method), t("Bill {{no}}", { no: inv.invoice_number }), formatTime(inv.created_at)].join(" · ")}</T>
            <T v="small">{t("Stock updated automatically")}</T>
          </View>
        )}

        <Pressable testID="bill-whatsapp" disabled={!!busy} onPress={whatsapp}
          style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.lg, padding: spacing.lg, minHeight: 84, borderRadius: radius.lg,
            backgroundColor: pressed ? "#0E7A3F" : colors.whatsapp, opacity: busy && busy !== "wa" ? 0.6 : 1 })}>
          <Icon name="whatsapp" size={40} color={colors.white} />
          <View style={{ flex: 1 }}>
            <T v="bodyLarge" color={colors.white}>{busy === "wa" ? t("Opening WhatsApp") : t("Send bill on WhatsApp")}</T>
            <T v="small" color="#DDF5E6">{who}</T>
          </View>
        </Pressable>
        <View style={{ flexDirection: "row", gap: spacing.md }}>
          <Button title={t("Share PDF")} icon="file-pdf-box" variant="outline" size="md" style={{ flex: 1 }} onPress={pdf} loading={busy === "pdf"} testID="bill-pdf" />
          <Button title={t("Download")} icon="download" variant="outline" size="md" style={{ flex: 1 }} onPress={download} loading={busy === "download"} testID="bill-download" />
        </View>

        <View style={{ gap: spacing.sm, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, marginTop: spacing.xs }}>
          <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
            <View style={{ flex: 1 }}>
              <T v="bold">{inv.customer_name}</T>
              {!!inv.customer_phone && <T v="small">{formatPhone(inv.customer_phone)}</T>}
            </View>
            <View style={{ alignItems: "flex-end" }}>
              <T v="bold">{inv.invoice_number}</T>
              <T v="small">{formatDate(inv.created_at)}</T>
            </View>
          </View>
          <View style={{ height: 1, backgroundColor: colors.line, marginVertical: spacing.xs }} />
          {inv.items.map((it, i) => (
            <View key={i} style={{ flexDirection: "row", gap: spacing.sm }}>
              <View style={{ flex: 1 }}>
                <T v="bold">{it.name}</T>
                <T v="small">{formatQtyUnit(it.quantity, it.unit)} × {formatCurrency(it.price)}{it.gst_percent ? ` · GST ${it.gst_percent}%` : ""}</T>
                {it.list_price != null && it.list_price !== it.price && (
                  <T v="caption" testID={`bill-shop-price-${it.name}`}>
                    {t("Shop price")} <T v="caption" style={{ textDecorationLine: "line-through" }}>{formatCurrency(it.list_price)}</T>
                  </T>
                )}
                {isOwner && !!it.short_qty && (
                  <T v="caption" color={colors.amberInk} testID={`bill-short-${it.name}`}>
                    {t("{{n}} sold without stock", { n: formatQtyUnit(it.short_qty, it.unit ?? "pcs") })}
                  </T>
                )}
              </View>
              <T v="bold">{formatCurrency(it.amount)}</T>
            </View>
          ))}
          <View style={{ height: 1, backgroundColor: colors.line, marginVertical: spacing.xs }} />
          {inv.discount > 0 && <Line label={t("Discount")} value={`- ${formatCurrency(inv.discount)}`} />}
          {inv.gst_breakup.map((g) => (
            <Line key={g.rate} label={t("Includes GST {{rate}}%", { rate: g.rate })} value={formatCurrency(g.tax)} />
          ))}
          <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}>
            <T v="title">{t("Total")}</T>
            <T v="title" testID="bill-total">{formatCurrency(inv.grand_total)}</T>
          </View>
          <T v="small">{t("Paid by {{method}} · made by {{name}}", { method: t(inv.payment_method), name: inv.created_by?.name ?? "" })}</T>
        </View>
      </ScrollView>
      {justPaid && (
        <BottomBar>
          <Button title={t("Next order")} icon="cart-plus" onPress={() => router.replace("/order/scan")} testID="bill-next" />
          <Button title={t("Back to home")} variant="outline" size="md" onPress={home} testID="bill-home" />
        </BottomBar>
      )}
    </Screen>
  );
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
      <T v="body" color={colors.inkSoft}>{label}</T>
      <T v="body" color={colors.inkSoft}>{value}</T>
    </View>
  );
}
