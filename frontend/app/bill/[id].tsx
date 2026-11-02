// A bill. Right after payment (done=1) it leads with "paid" and Send on WhatsApp.
// WhatsApp: straight to the customer's chat if we have their number, else WhatsApp asks for a contact.
// Shop setting "Send bill on WhatsApp" on (the default): right after payment WhatsApp opens by itself, once per bill
// (not when an old bill is opened again), and the big button stays. Off: no auto-open, and a small button for
// when the customer asks for the bill.
// The owner can correct or cancel a bill until correct_until (7 days from the first bill): Correct loads it into the
// order screens (see cart-context), Cancel asks why. A cancelled bill says so at the top and links to the bill that
// replaced it; a corrected bill links to the one it replaces.
import { useEffect, useState } from "react";
import { Linking, Pressable, ScrollView, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import i18n from "i18next";
import { useTranslation } from "react-i18next";

import { apiGet, apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { BillFileResult, downloadBillPdf, shareBillPdf } from "@/src/bill-file";
import { useBusiness, whatsappBill } from "@/src/business";
import { useCart } from "@/src/cart-context";
import { notice } from "@/src/confirm";
import { Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Chip, ErrorView, Field, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatDate, formatPhone, formatQtyUnit, formatTime } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";
import type { BillRef, Invoice, Product } from "@/src/types";

const autoSent = new Set<string>(); // bills WhatsApp already opened for by itself (while the app is open)

export default function Bill() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const { user, isOwner } = useAuth();
  const { id, done } = useLocalSearchParams<{ id: string; done?: string }>();
  const justPaid = done === "1";
  const q = useQuery<Invoice>({ queryKey: ["invoice", id], queryFn: () => apiGet(`/invoices/${id}`) });
  const [busy, setBusy] = useState<"" | "wa" | "pdf" | "download" | "correct">("");
  const [cancelling, setCancelling] = useState(false);
  const cart = useCart();
  const qc = useQueryClient();
  const [openedAt] = useState(() => Date.now()); // the correct/cancel buttons go by when this bill was opened
  const inv = q.data;
  const biz = useBusiness();
  const waOn = whatsappBill(biz.data);
  const home = () => (router.canGoBack() ? router.back() : router.replace("/(tabs)"));

  async function run(kind: "wa" | "pdf" | "download" | "correct", fn: () => Promise<unknown>) {
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
    const vars = { name: greet, shop: user?.business?.name ?? "", amount: formatCurrency(inv!.grand_total), no: inv!.invoice_number, url,
      old: inv!.replaces?.invoice_number ?? "" };
    const text = inv!.status === "Cancelled" ? en("Namaste{{name}}! Your bill {{no}} from {{shop}} has been cancelled.\nSee the bill: {{url}}", vars)
      : inv!.replaces ? en("Namaste{{name}}! Your corrected bill from {{shop}}: {{amount}} ({{no}}, replaces {{old}}).\nSee the bill: {{url}}", vars)
      : en("Namaste{{name}}! Your bill from {{shop}}: {{amount}} ({{no}}).\nSee the bill: {{url}}", vars);
    const digits = inv!.customer_phone.replace(/\D/g, "");
    await Linking.openURL(`https://wa.me/${digits}?text=${encodeURIComponent(text)}`);
  });

  useEffect(() => {
    if (!justPaid || !inv || !biz.data || !waOn || !inv.customer_phone || autoSent.has(inv.id)) return;
    const billId = inv.id;
    autoSent.add(billId);
    let fired = false;
    const timer = setTimeout(() => { fired = true; whatsapp(); }, 500); // the paid tick shows first
    return () => {
      if (!fired) { clearTimeout(timer); autoSent.delete(billId); } // left (or re-run) before it opened
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [justPaid, inv, biz.data, waOn]);

  const pdf = () => run("pdf", () => shareBillPdf(id, inv!.invoice_number));

  const download = () => run("download", async () => {
    const r: BillFileResult = await downloadBillPdf(id, inv!.invoice_number);
    if (r === "saved") toast.show(t("Saved Bill {{no}}.pdf", { no: inv!.invoice_number }), "success");
  });

  // The bill's lines become the order (prices as billed, stock and GST as they are now); Pay makes the corrected bill.
  const correct = () => run("correct", async () => {
    const ids = [...new Set(inv!.items.map((i) => i.product_id).filter(Boolean))].join(",");
    const products: Product[] = ids ? await apiGet(`/products?ids=${ids}`) : [];
    const skipped = await cart.startCorrection(inv!, products);
    router.push("/order/scan"); // under the order, for "Add more items"
    router.push("/order/cart");
    if (!inv!.customer_id) router.push({ pathname: "/order/customer", params: { change: "1" } });
    if (skipped.length) {
      await notice(t("Some items left out"), t("These items are no longer in your products, so they are not in the corrected bill: {{names}}",
        { names: skipped.join(", ") }));
    }
  });

  const open = (b: BillRef) => router.push({ pathname: "/bill/[id]", params: { id: b.id } });

  // A fresh order, like Home: a correction left open with Back (not Stop) must not become the next order.
  const nextOrder = () => {
    cart.startNew();
    router.replace("/order/customer");
  };

  if (!inv) {
    return (
      <Screen bg={colors.surface}>
        <Header title={t("Bill")} onBack={home} />
        {q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />}
      </Screen>
    );
  }

  const cancelled = inv.status === "Cancelled";
  const canFix = isOwner && !cancelled && !!inv.correct_until && openedAt < Date.parse(inv.correct_until);
  const bigWa = waOn && !cancelled; // a cancelled bill gets the small button only
  const shopLine = [inv.shop?.address, inv.shop?.phone ? formatPhone(inv.shop.phone) : ""].filter(Boolean).join(" · ");
  const who = inv.customer_phone
    ? t("To {{name}} · {{phone}}", { name: inv.customer_name.split(" ")[0], phone: formatPhone(inv.customer_phone) })
    : t("Choose the customer in WhatsApp");

  return (
    <Screen bg={colors.surface}>
      {justPaid ? <View style={{ height: 8 }} /> : <Header title={t("Bill {{no}}", { no: inv.invoice_number })} onBack={home} />}
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
        {cancelled && (
          <View testID="bill-cancelled" style={{ gap: spacing.sm, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1,
            borderColor: colors.redLine, backgroundColor: colors.redTint }}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <Icon name="close-circle-outline" size={24} color={colors.red} />
              <T v="bodyLarge" color={colors.red} style={{ flex: 1 }}>{t("Cancelled")}</T>
            </View>
            <T v="small" color={colors.red}>
              {[inv.cancelled_at ? formatDate(inv.cancelled_at) : "", inv.cancelled_by?.name ? t("by {{name}}", { name: inv.cancelled_by.name }) : ""]
                .filter(Boolean).join(" · ")}
            </T>
            {!!inv.cancel_reason && <T v="small" color={colors.red}>{t("Reason: {{reason}}", { reason: inv.cancel_reason })}</T>}
            <T v="small" color={colors.red}>{t("Not counted in sales, reports or the customer's total.")}</T>
            {!!inv.replaced_by && (
              <Button title={t("Open {{no}}, the corrected bill", { no: inv.replaced_by.invoice_number })} icon="file-document-outline" variant="outline"
                size="md" onPress={() => open(inv.replaced_by!)} testID="bill-replaced-by" />
            )}
          </View>
        )}
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

        {bigWa && <Pressable testID="bill-whatsapp" disabled={!!busy} onPress={whatsapp}
          style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.lg, padding: spacing.lg, minHeight: 84, borderRadius: radius.lg,
            backgroundColor: pressed ? "#0E7A3F" : colors.whatsapp, opacity: busy && busy !== "wa" ? 0.6 : 1 })}>
          <Icon name="whatsapp" size={40} color={colors.white} />
          <View style={{ flex: 1 }}>
            <T v="bodyLarge" color={colors.white}>{busy === "wa" ? t("Opening WhatsApp") : t("Send bill on WhatsApp")}</T>
            <T v="small" color="#DDF5E6">{who}</T>
          </View>
        </Pressable>}
        <View style={{ flexDirection: "row", gap: spacing.md }}>
          <Button title={t("Share PDF")} icon="file-pdf-box" variant="outline" size="md" style={{ flex: 1 }} onPress={pdf} loading={busy === "pdf"} testID="bill-pdf" />
          <Button title={t("Download")} icon="download" variant="outline" size="md" style={{ flex: 1 }} onPress={download} loading={busy === "download"} testID="bill-download" />
        </View>
        {!bigWa && (
          <Button title={busy === "wa" ? t("Opening WhatsApp") : t("Send bill on WhatsApp")} icon="whatsapp" variant="outline" size="md"
            onPress={whatsapp} disabled={!!busy && busy !== "wa"} loading={busy === "wa"} testID="bill-whatsapp" />
        )}

        <View style={{ gap: spacing.sm, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, marginTop: spacing.xs }}>
          {!!inv.shop?.name && (
            <>
              <View testID="bill-shop">
                <T v="bold">{inv.shop.name}</T>
                {!!shopLine && <T v="small">{shopLine}</T>}
              </View>
              <View style={{ height: 1, backgroundColor: colors.line, marginVertical: spacing.xs }} />
            </>
          )}
          <View style={{ flexDirection: "row", justifyContent: "space-between", gap: spacing.md }}>
            <View style={{ flex: 1 }}>
              {!!inv.shop?.name && <T v="caption">{t("Bill to")}</T>}
              <T v="bold">{inv.customer_name}</T>
              {!!inv.customer_phone && <T v="small">{formatPhone(inv.customer_phone)}</T>}
              {!!inv.customer_address && <T v="small" testID="bill-customer-address">{inv.customer_address}</T>}
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
          {!!inv.upi_id && <T v="small" testID="bill-upi">{t("Paid to UPI ID {{upi}}", { upi: inv.upi_id })}</T>}
          {!!inv.corrected_by?.name && <T v="small">{t("Corrected by {{name}}", { name: inv.corrected_by.name })}</T>}
          {!!inv.replaces && (
            <Pressable onPress={() => open(inv.replaces!)} testID="bill-replaces" accessibilityRole="link"
              style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs, minHeight: 44 }}>
              <Icon name="history" size={20} color={colors.green} />
              <T v="bold" color={colors.green} style={{ flex: 1 }}>{t("Replaces {{no}} (cancelled)", { no: inv.replaces.invoice_number })}</T>
              <Icon name="chevron-right" size={22} color={colors.green} />
            </Pressable>
          )}
        </View>

        {canFix && (
          <View testID="bill-fix" style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
            <View>
              <T v="bold">{t("Something wrong on this bill?")}</T>
              <T v="small">{t("You can correct or cancel it until {{date}}.", { date: formatDate(inv.correct_until) })}</T>
            </View>
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <Button title={t("Correct bill")} icon="pencil-outline" variant="outline" size="md" style={{ flex: 1 }} onPress={correct}
                loading={busy === "correct"} disabled={!!busy && busy !== "correct"} testID="bill-correct" />
              <Button title={t("Cancel bill")} icon="close-circle-outline" variant="danger" size="md" style={{ flex: 1 }}
                onPress={() => setCancelling(true)} disabled={!!busy} testID="bill-cancel" />
            </View>
          </View>
        )}
      </ScrollView>
      {justPaid && (
        <BottomBar>
          <Button title={t("Next order")} icon="cart-plus" onPress={nextOrder} testID="bill-next" />
          <Button title={t("Back to home")} variant="outline" size="md" onPress={home} testID="bill-home" />
        </BottomBar>
      )}
      <CancelSheet visible={cancelling} onClose={() => setCancelling(false)} inv={inv}
        onDone={() => {
          setCancelling(false);
          qc.invalidateQueries();
          toast.show(t("Bill {{no}} cancelled", { no: inv.invoice_number }), "success");
        }} />
    </Screen>
  );
}

// Why the bill is cancelled (kept on the bill for the owner; the customer's copy only says Cancelled).
function CancelSheet({ visible, onClose, inv, onDone }: { visible: boolean; onClose: () => void; inv: Invoice; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const reasons = [t("Entered by mistake"), t("Customer returned the items"), t("Billed twice")];

  async function cancel() {
    setBusy(true);
    try {
      await apiPost(`/invoices/${inv.id}/cancel`, { reason: reason.trim() });
      setReason("");
      onDone();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet visible={visible} onClose={onClose} title={t("Cancel bill {{no}}?", { no: inv.invoice_number })} testID="bill-cancel-sheet"
      footer={(
        <>
          <Button title={t("Cancel bill")} icon="close-circle-outline" variant="danger" onPress={cancel} loading={busy}
            disabled={!reason.trim()} testID="bill-cancel-confirm" />
          <Button title={t("Keep the bill")} variant="outline" size="md" onPress={onClose} />
        </>
      )}>
      <T v="body">{t("The stock comes back and the bill no longer counts in sales or the customer's total. It stays in Orders, marked Cancelled.")}</T>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
        {reasons.map((r, i) => (
          <Chip key={r} label={r} active={reason === r} onPress={() => setReason(r)} testID={`bill-cancel-reason-${i}`} />
        ))}
      </View>
      <Field label={t("Reason")} value={reason} onChangeText={setReason} maxLength={200} multiline testID="bill-cancel-reason" />
    </Sheet>
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
