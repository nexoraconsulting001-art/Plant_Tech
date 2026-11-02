// One product: picture, price, stock; Add stock / Remove open a sheet; and what happened to its stock.
import { useState } from "react";
import { Pressable, ScrollView, TextInput, View } from "react-native";
import { Image } from "expo-image";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet, apiPost, fileUrl } from "@/src/api";
import { useAuth } from "@/src/auth";
import { Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Badge, BottomBar, Button, Chip, ErrorView, Header, Icon, LoadingView, MAX_FONT_SCALE, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatDate, formatQty, formatQtyUnit } from "@/src/format";
import { colors, fonts, radius, spacing, touch } from "@/src/theme";
import type { Product, StockEntry } from "@/src/types";
import { roundQty, unitShort, wholeUnit } from "@/src/units";

type Change = "add" | "remove";

export default function ProductDetail() {
  const { t } = useTranslation();
  const router = useRouter();
  const { can, isOwner } = useAuth();
  const { id } = useLocalSearchParams<{ id: string }>();
  const q = useQuery<Product & { history: StockEntry[] }>({ queryKey: ["product", id], queryFn: () => apiGet(`/products/${id}`) });
  const [change, setChange] = useState<Change | null>(null);
  const p = q.data;

  const what = (h: StockEntry) =>
    h.type === "initial" ? t("Opening stock") : h.type === "sale" ? t("Sold") : h.type === "purchase" ? t("Supplier bill")
      : h.type === "bill_cancel" ? t("Bill {{no}} cancelled", { no: h.bill ?? "" }) : h.type === "bill_correct" ? t("Bill corrected ({{no}})", { no: h.bill ?? "" })
      : h.change > 0 ? t("Stock added") : h.reason && h.reason !== "Removed" ? h.reason : t("Removed");

  return (
    <Screen>
      <Header title={t("Product")} right={p && can("stock") ? (
        <Pressable onPress={() => router.push({ pathname: "/products/edit", params: { id } })} testID="product-edit"
          style={{ flexDirection: "row", alignItems: "center", gap: 6, minHeight: touch.min, paddingHorizontal: spacing.lg }}>
          <Icon name="pencil-outline" size={22} color={colors.green} />
          <T v="bold" color={colors.green}>{t("Edit")}</T>
        </Pressable>
      ) : undefined} />
      {!p ? (q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />) : (
        <>
          <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }}>
            <View style={{ flexDirection: "row", gap: spacing.lg, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line }}>
              <View style={{ width: 104, height: 104, borderRadius: radius.lg, overflow: "hidden", backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
                {p.image_path ? <Image source={{ uri: fileUrl(p.image_path) }} style={{ width: "100%", height: "100%" }} contentFit="cover" />
                  : <Icon name="sprout" size={52} color={colors.greenMid} />}
              </View>
              <View style={{ flex: 1, gap: spacing.xs, justifyContent: "center" }}>
                <T v="title" style={{ fontSize: 24, lineHeight: 30 }} testID="product-title">{p.name}</T>
                <T v="small">{[p.category ? t(p.category) : t("No category"), p.gst_percent ? t("{{n}}% GST", { n: p.gst_percent }) : "",
                  wholeUnit(p.unit) ? "" : t("Sold by {{unit}}", { unit: unitShort(p.unit) })].filter(Boolean).join(" · ")}</T>
                {p.image_source === "library" && <T v="caption">{t("Shared picture")}</T>}
              </View>
            </View>

            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, gap: 2 }}>
                <T v="label">{t("Price")}</T>
                <T v="title">{formatCurrency(p.selling_price)}</T>
                {!wholeUnit(p.unit) && <T v="caption">{t("per {{unit}}", { unit: unitShort(p.unit) })}</T>}
                {isOwner && !!p.cost_price && <T v="caption">{t("Cost {{c}} · visible only to you", { c: formatCurrency(p.cost_price) })}</T>}
              </View>
              <View style={{ flex: 1, padding: spacing.lg, borderRadius: radius.lg, borderWidth: 1, gap: 2,
                borderColor: p.out_of_stock ? colors.redLine : p.low_stock ? colors.amber : colors.line,
                backgroundColor: p.out_of_stock ? "#FDECEC" : p.low_stock ? colors.amberTint : colors.surface }}>
                <T v="label">{t("In stock")}</T>
                <T v="title" testID="product-stock" color={p.out_of_stock ? colors.red : colors.ink}>{formatQtyUnit(p.stock, p.unit)}</T>
                {p.stock < 0 ? <T v="caption" color={colors.red} testID="product-below-zero">{t("{{n}} sold without stock", { n: formatQtyUnit(-p.stock, p.unit) })}</T>
                  : p.out_of_stock ? <Badge text={t("Out of stock")} tone="red" />
                  : p.low_stock ? <T v="caption" color={colors.amberInk}>{t("Low · alert below {{n}}", { n: formatQty(p.min_stock) })}</T>
                  : <T v="caption">{t("Alert below {{n}}", { n: formatQty(p.min_stock) })}</T>}
              </View>
            </View>

            <View style={{ gap: 6 }}>
              <T v="label" style={{ fontSize: 14, letterSpacing: 0.6, textTransform: "uppercase", color: colors.muted }}>{t("Stock history")}</T>
              {p.history.length ? (
                <View style={{ borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, paddingHorizontal: spacing.lg }}>
                  {p.history.map((h, i) => (
                    <View key={h.id} style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingVertical: spacing.md, borderTopWidth: i ? 1 : 0, borderTopColor: colors.line }}>
                      <View style={{ width: 36, height: 36, borderRadius: 18, alignItems: "center", justifyContent: "center",
                        backgroundColor: h.change > 0 ? colors.greenTint : "#FDECEC" }}>
                        <Icon name={h.type === "sale" ? "cart-outline" : h.change > 0 ? "arrow-down-bold" : "arrow-up-bold"} size={20}
                          color={h.change > 0 ? colors.green : colors.red} />
                      </View>
                      <View style={{ flex: 1 }}>
                        <T v="bold">{what(h)}</T>
                        <T v="small">{[formatDate(h.created_at), h.by?.name].filter(Boolean).join(" · ")}</T>
                      </View>
                      <View style={{ alignItems: "flex-end" }}>
                        <T v="number" color={h.change > 0 ? colors.green : colors.red}>{h.change > 0 ? "+" : "−"}{formatQty(Math.abs(h.change))}</T>
                        <T v="caption">{t("left {{n}}", { n: formatQty(h.new_stock) })}</T>
                      </View>
                    </View>
                  ))}
                </View>
              ) : <T v="small">{t("No stock changes yet")}</T>}
            </View>
          </ScrollView>

          {can("stock") && (
            <BottomBar style={{ flexDirection: "row" }}>
              <Button title={t("Remove")} icon="minus-circle-outline" variant="danger" size="md" style={{ flex: 1, paddingHorizontal: spacing.md }}
                onPress={() => setChange("remove")} disabled={p.stock <= 0} testID="stock-remove" />
              <Button title={t("Add stock")} icon="plus-circle-outline" size="md" style={{ flex: 1.3, paddingHorizontal: spacing.md }}
                onPress={() => setChange("add")} testID="stock-add" />
            </BottomBar>
          )}
          {change && <StockSheet product={p} kind={change} onClose={() => setChange(null)} onDone={() => q.refetch()} />}
        </>
      )}
    </Screen>
  );
}

// Add or remove stock: big number with − / +, quick amounts, and for removing a reason.
function StockSheet({ product: p, kind, onClose, onDone }: { product: Product; kind: Change; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const adding = kind === "add";
  const REASONS = [t("Damaged"), t("Plant died"), t("Lost"), t("Count correction")];
  const [text, setText] = useState("1");
  const [reason, setReason] = useState(REASONS[0]);
  const [busy, setBusy] = useState(false);
  const whole = wholeUnit(p.unit); // pieces: whole numbers only
  const n = Number(text) || 0;
  const max = adding ? 100000 : p.stock;
  const ok = n > 0 && n <= max;
  const after = roundQty(adding ? p.stock + n : p.stock - n, false);
  const set = (v: number) => setText(formatQty(Math.max(0, Math.min(whole ? Math.floor(max) : max, roundQty(v, whole)))));

  async function save() {
    setBusy(true);
    try {
      await apiPost(`/products/${p.id}/stock`, { change: adding ? n : -n, reason: adding ? "Added" : reason });
      qc.invalidateQueries({ queryKey: ["products"] });
      onDone();
      toast.show(adding ? t("Added {{n}}. Stock is now {{s}}", { n: formatQtyUnit(n, p.unit), s: formatQtyUnit(after, p.unit) })
        : t("Removed {{n}}. Stock is now {{s}}", { n: formatQtyUnit(n, p.unit), s: formatQtyUnit(after, p.unit) }), "success");
      onClose();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const round = { width: 64, height: 64, borderRadius: 32, alignItems: "center" as const, justifyContent: "center" as const };
  return (
    <Sheet visible onClose={onClose} title={adding ? t("Add stock") : t("Remove stock")} testID="stock-sheet"
      footer={
        <Button title={adding ? t("Add {{n}}", { n: formatQtyUnit(n, p.unit) }) : t("Remove {{n}}", { n: formatQtyUnit(n, p.unit) })} icon={adding ? "plus" : "minus"}
          variant={adding ? "primary" : "danger"} onPress={save} loading={busy} disabled={!ok} testID="stock-save" />
      }>
      <T v="body" color={colors.inkSoft}>{p.name}</T>
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.lg, paddingVertical: 6 }}>
        <Pressable accessibilityLabel={t("Decrease quantity")} onPress={() => set(n - 1)} style={[round, { borderWidth: 2, borderColor: colors.lineStrong }]} testID="stock-minus">
          <Icon name="minus" size={32} color={colors.ink} />
        </Pressable>
        <View style={{ alignItems: "center", gap: 2 }}>
          <TextInput value={text} onChangeText={(v) => setText(whole ? v.replace(/\D/g, "") : v.replace(/[^0-9.]/g, ""))}
            keyboardType={whole ? "number-pad" : "decimal-pad"} selectTextOnFocus
            maxFontSizeMultiplier={MAX_FONT_SCALE} testID="stock-qty" accessibilityLabel={t("How many")}
            style={{ width: 120, height: 72, textAlign: "center", fontFamily: fonts.display, fontSize: 40, color: colors.ink,
              borderWidth: 2, borderColor: ok ? colors.green : colors.red, borderRadius: radius.lg }} />
          <T v="caption" testID="stock-unit">{unitShort(p.unit)}</T>
        </View>
        <Pressable accessibilityLabel={t("Increase quantity")} onPress={() => set(n + 1)} style={[round, { backgroundColor: adding ? colors.green : colors.red }]} testID="stock-plus">
          <Icon name="plus" size={32} color={colors.white} />
        </Pressable>
      </View>
      {adding ? (
        <View style={{ flexDirection: "row", gap: spacing.sm, justifyContent: "center" }}>
          {[5, 10, 25].map((x) => <Chip key={x} label={`+${x}`} onPress={() => set(n + x)} testID={`stock-quick-${x}`} />)}
        </View>
      ) : (
        <View style={{ gap: spacing.sm }}>
          <T v="label">{t("Reason")}</T>
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
            {REASONS.map((r) => <Chip key={r} label={r} active={reason === r} onPress={() => setReason(r)} />)}
          </View>
        </View>
      )}
      <View style={{ flexDirection: "row", justifyContent: "center", alignItems: "center", gap: spacing.md, padding: spacing.md, borderRadius: radius.md, backgroundColor: colors.page }}>
        <T v="bold">{formatQty(p.stock)}</T>
        <Icon name="arrow-right" size={22} color={colors.inkSoft} />
        <T v="number" color={after < 0 ? colors.red : colors.greenDark}>{formatQtyUnit(adding ? after : Math.max(after, 0), p.unit)}</T>
        <T v="small">{t("in stock after this")}</T>
      </View>
      {!adding && n > p.stock && <T v="small" color={colors.red}>{t("Only {{n}} in stock", { n: formatQty(p.stock) })}</T>}
    </Sheet>
  );
}
