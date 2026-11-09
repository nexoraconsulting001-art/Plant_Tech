// Sales reports for a chosen date, week (Monday to Sunday) or month: total, profit, chart,
// how customers paid, and every item sold. ◀ ▶ step through periods; tapping the date opens a picker.
// Export (the owner, 10 Nov 2026): the period on screen as Excel (Summary, Bills and Items sheets) or CSV (the bills),
// shared or saved on the phone (GET /api/reports/sales/export).
import { useState } from "react";
import { Platform, Pressable, ScrollView, View } from "react-native";
import { useLocalSearchParams } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useAuth } from "@/src/auth";
import { exportFile } from "@/src/export-file";
import { PeriodBar, usePeriod } from "@/src/components/period";
import { OptionRow, Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Button, ErrorView, Header, Icon, LoadingView, Screen, T } from "@/src/components/ui";
import { formatCurrency, formatQty, plural } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";

type Bucket = { label: string; sales: number };
type Report = {
  start: string; end: string; sales: number; orders: number; profit: number; buckets: Bucket[];
  by_method: Record<"Cash" | "UPI" | "Card", number>; items: { name: string; quantity: number; amount: number }[];
};

export default function Reports() {
  const { t } = useTranslation();
  const { date } = useLocalSearchParams<{ date?: string }>(); // from the daily summary notification
  const p = usePeriod("day", date);
  const q = useQuery<Report>({ queryKey: ["report", p.kind, p.anchor], queryFn: () => apiGet(`/reports/sales?range=${p.kind}&date=${p.anchor}`) });
  const r = q.data;
  const { isOwner } = useAuth();
  const [exporting, setExporting] = useState(false);

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Sales reports")} right={isOwner ? (
        <Pressable onPress={() => setExporting(true)} accessibilityRole="button" testID="report-export" hitSlop={4}
          style={{ flexDirection: "row", alignItems: "center", gap: 6, minHeight: 48, paddingHorizontal: spacing.md }}>
          <Icon name="tray-arrow-down" size={22} color={colors.green} />
          <T v="bold" color={colors.green}>{t("Export")}</T>
        </Pressable>
      ) : undefined} />
      <ScrollView contentContainerStyle={{ padding: spacing.lg, paddingTop: spacing.xs, gap: spacing.lg, paddingBottom: spacing.xl }}>
        <PeriodBar p={p} />

        {!r ? (q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : <LoadingView />) : (
          <>
            <View style={{ padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.greenTint, gap: 2 }}>
              <T v="label" color={colors.greenDark}>{t("Sales")}</T>
              <T v="amount" testID="report-total">{formatCurrency(r.sales)}</T>
              <T v="small" color={colors.greenDark}>
                {[plural(r.orders, "1 order", "{{n}} orders"), r.profit > 0 ? t("estimated profit {{p}}", { p: formatCurrency(Math.round(r.profit)) }) : ""]
                  .filter(Boolean).join(" · ")}
              </T>
            </View>

            <SalesChart key={`${p.key}:${r.start}`} buckets={r.buckets} />

            <View style={{ gap: spacing.sm }}>
              <T v="label" style={{ fontSize: 18 }}>{t("How customers paid")}</T>
              {(["Cash", "UPI", "Card"] as const).map((m) => {
                const share = r.sales ? Math.round((r.by_method[m] / r.sales) * 100) : 0;
                return (
                  <View key={m} style={{ gap: spacing.xs }}>
                    <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
                      <T v="bold">{t(m)}</T>
                      <T v="bold">{formatCurrency(r.by_method[m])} <T v="small">· {share}%</T></T>
                    </View>
                    <View style={{ height: 8, borderRadius: 4, backgroundColor: colors.page }}>
                      <View style={{ width: `${share}%`, height: 8, borderRadius: 4, backgroundColor: colors.green }} />
                    </View>
                  </View>
                );
              })}
            </View>

            <View style={{ gap: 2 }} testID="report-items">
              <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "baseline", marginBottom: spacing.xs }}>
                <T v="label" style={{ fontSize: 18 }}>{t("Items sold")}</T>
                {r.items.length > 0 && <T v="small">{plural(r.items.length, "1 kind", "{{n}} kinds")}</T>}
              </View>
              {r.items.length ? r.items.map((x, i) => (
                <View key={x.name} style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingVertical: spacing.md, borderBottomWidth: 1, borderBottomColor: colors.line }}>
                  <T v="bold" color={colors.muted} style={{ width: 28 }}>{i + 1}</T>
                  <View style={{ flex: 1 }}>
                    <T v="bold">{x.name}</T>
                    <T v="small">{t("{{n}} sold", { n: formatQty(x.quantity) })}</T>
                  </View>
                  <T v="bold">{formatCurrency(x.amount)}</T>
                </View>
              )) : <T v="small">{t("No sales in this period")}</T>}
            </View>
          </>
        )}
      </ScrollView>
      {isOwner && <ExportSheet visible={exporting} onClose={() => setExporting(false)} kind={p.kind} anchor={p.anchor} start={p.start} end={p.end} />}
    </Screen>
  );
}

// One series, one axis: sales per bucket. The chosen bar (the best one at first) carries its total;
// tap any bar to see its own.
function SalesChart({ buckets }: { buckets: Bucket[] }) {
  const { t } = useTranslation();
  const max = Math.max(1, ...buckets.map((b) => b.sales));
  const peak = buckets.reduce((best, b, i) => (b.sales > buckets[best].sales ? i : best), 0);
  const [picked, setPicked] = useState(peak);
  const last = buckets.length - 1;
  return (
    <View style={{ gap: 6 }}>
      <View style={{ flexDirection: "row", alignItems: "flex-end", gap: 6, height: 176, paddingTop: 26, borderBottomWidth: 1, borderBottomColor: colors.line }}>
        {buckets.map((b, i) => {
          const on = i === picked;
          return (
            <Pressable key={b.label} onPress={() => setPicked(i)} testID={`report-bar-${i}`}
              accessibilityRole="button" accessibilityLabel={`${b.label} ${formatCurrency(b.sales)}`} accessibilityState={{ selected: on }}
              style={{ flex: 1, alignItems: "center", justifyContent: "flex-end", height: "100%" }}>
              {on && (
                <T v="caption" color={colors.ink} testID="report-bar-total"
                  style={[{ position: "absolute", top: -22, width: 96, fontWeight: "700" },
                    i === 0 ? { left: 0, textAlign: "left" } : i === last ? { right: 0, textAlign: "right" } : { textAlign: "center" }]}>
                  {formatCurrency(b.sales)}
                </T>
              )}
              <View style={{ width: "70%", maxWidth: 28, height: `${Math.max(b.sales ? 3 : 0, (b.sales / max) * 100)}%`,
                borderTopLeftRadius: 4, borderTopRightRadius: 4, backgroundColor: on ? colors.green : colors.greenMid, opacity: on ? 1 : 0.55 }} />
            </Pressable>
          );
        })}
      </View>
      <View style={{ flexDirection: "row", gap: 6 }}>
        {buckets.map((b, i) => (
          <T key={b.label} v="caption" color={i === picked ? colors.ink : undefined} style={{ flex: 1, textAlign: "center", fontSize: 12 }}>{b.label}</T>
        ))}
      </View>
      <T v="caption" style={{ textAlign: "center" }}>{t("Tap a bar to see its total")}</T>
    </View>
  );
}

// Excel or CSV of the period on screen, then shared or saved on the phone (downloaded in a browser).
const FORMATS = {
  xlsx: { mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
  csv: { mime: "text/csv" },
} as const;

function ExportSheet({ visible, onClose, kind, anchor, start, end }: {
  visible: boolean; onClose: () => void; kind: string; anchor: string; start: string; end: string;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [format, setFormat] = useState<keyof typeof FORMATS>("xlsx");
  const [busy, setBusy] = useState<"share" | "save" | null>(null);
  const web = Platform.OS === "web";

  async function go(how: "share" | "save") {
    setBusy(how);
    try {
      const name = `Sales_${start}${start === end ? "" : `_to_${end}`}.${format}`; // as the server names it
      const done = await exportFile(`/reports/sales/export?range=${kind}&date=${anchor}&format=${format}`, name, FORMATS[format].mime, how);
      if (done === "saved") toast.show(web ? t("Downloaded") : t("Saved to your phone"), "success");
      if (done !== "cancelled") onClose();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Sheet visible={visible} onClose={onClose} title={t("Export sales")} testID="export-sheet"
      footer={web ? (
        <Button title={t("Download")} icon="tray-arrow-down" onPress={() => go("save")} loading={busy === "save"} testID="export-download" />
      ) : (
        <>
          <Button title={t("Share")} icon="share-variant" onPress={() => go("share")} loading={busy === "share"} disabled={!!busy} testID="export-share" />
          <Button title={t("Save to phone")} icon="tray-arrow-down" variant="outline" size="md" onPress={() => go("save")} loading={busy === "save"}
            disabled={!!busy} testID="export-save" />
        </>
      )}>
      <T v="small">{t("The bills of the period on screen, cancelled ones marked.")}</T>
      <OptionRow label={t("Excel")} sub={t("Summary, bills and items sold, in three sheets")} icon="microsoft-excel" selected={format === "xlsx"}
        onPress={() => setFormat("xlsx")} testID="export-xlsx" />
      <OptionRow label={t("CSV")} sub={t("The bills, for any spreadsheet or accounting app")} icon="file-delimited-outline" selected={format === "csv"}
        onPress={() => setFormat("csv")} testID="export-csv" />
    </Sheet>
  );
}
