// Picking a report period: Date / Week (Monday to Sunday) / Month tabs, ◀ label ▶, and a calendar or
// month sheet when the label (or the chosen tab again) is tapped. Used by Sales reports and Employees.
import { useState } from "react";
import { Pressable, View } from "react-native";
import { useTranslation } from "react-i18next";

import { CalendarSheet, MonthSheet } from "@/src/components/calendar";
import { Icon, Segmented, T } from "@/src/components/ui";
import { addDays, addMonths, dayLabelLong, mondayOf, monthLabel, monthStart, rangeLabel } from "@/src/dates";
import { todayKey } from "@/src/format";
import { colors, radius, touch, spacing } from "@/src/theme";

export type PeriodKind = "day" | "week" | "month";

// `from`: a day to open on (e.g. the day of a summary notification); today when missing or in the future.
export function usePeriod(initial: PeriodKind = "day", from?: string) {
  const today = todayKey();
  const [kind, setKind] = useState<PeriodKind>(initial);
  const [anchor, setAnchor] = useState(from && /^\d{4}-\d{2}-\d{2}$/.test(from) && from <= today ? from : today);
  const start = kind === "day" ? anchor : kind === "week" ? mondayOf(anchor) : monthStart(anchor);
  const end = kind === "day" ? anchor : kind === "week" ? addDays(start, 6) : addDays(addMonths(start, 1), -1);
  return { kind, setKind, anchor, setAnchor, today, start, end, key: `${kind}:${start}` };
}

export type Period = ReturnType<typeof usePeriod>;

export function PeriodBar({ p }: { p: Period }) {
  const { t } = useTranslation();
  const [picker, setPicker] = useState(false);
  const { kind, anchor, start, end, today } = p;
  const label = kind === "day" ? dayLabelLong(anchor) : kind === "week" ? rangeLabel(start, end) : monthLabel(anchor);
  const isCurrent = today >= start && today <= end;
  const step = (n: number) => p.setAnchor(kind === "day" ? addDays(anchor, n) : kind === "week" ? addDays(start, 7 * n) : addMonths(start, n));
  const next = kind === "day" ? addDays(anchor, 1) : kind === "week" ? addDays(start, 7) : addMonths(start, 1);
  const canNext = next <= today;
  const pickKind = (k: PeriodKind) => {
    if (k === kind) setPicker(true); // tapping the chosen tab again opens the picker
    p.setKind(k);
  };
  const navBtn = { width: touch.min, height: touch.min, borderRadius: 28, alignItems: "center" as const, justifyContent: "center" as const, backgroundColor: colors.page };

  return (
    <View style={{ gap: spacing.lg }}>
      <Segmented value={kind} onChange={pickKind}
        options={[{ value: "day", label: t("Date") }, { value: "week", label: t("Week") }, { value: "month", label: t("Month") }]} />
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
        <Pressable accessibilityLabel={t("Earlier")} onPress={() => step(-1)} style={navBtn} testID="report-prev">
          <Icon name="chevron-left" size={30} color={colors.ink} />
        </Pressable>
        <Pressable onPress={() => setPicker(true)} testID="report-pick"
          style={({ pressed }) => ({ flex: 1, minHeight: touch.min, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.sm,
            paddingHorizontal: spacing.md, borderRadius: radius.md, borderWidth: 2, borderColor: colors.green, backgroundColor: pressed ? colors.greenTint : colors.surface })}>
          <Icon name="calendar-month-outline" size={22} color={colors.green} />
          <T v="bold" color={colors.greenDark} style={{ textAlign: "center", flexShrink: 1 }}>{label}</T>
        </Pressable>
        <Pressable accessibilityLabel={t("Later")} onPress={() => step(1)} disabled={!canNext} style={[navBtn, { opacity: canNext ? 1 : 0.35 }]} testID="report-next">
          <Icon name="chevron-right" size={30} color={colors.ink} />
        </Pressable>
      </View>
      {!isCurrent && (
        <Pressable onPress={() => p.setAnchor(today)} style={{ alignSelf: "center", minHeight: 40, justifyContent: "center", marginTop: -8 }}>
          <T v="bold" color={colors.green}>{kind === "day" ? t("Back to today") : kind === "week" ? t("Back to this week") : t("Back to this month")}</T>
        </Pressable>
      )}

      {picker && kind === "month" && (
        <MonthSheet visible onClose={() => setPicker(false)} value={anchor} onPick={(k) => p.setAnchor(k)} />
      )}
      {picker && kind !== "month" && (
        <CalendarSheet visible onClose={() => setPicker(false)} title={kind === "day" ? t("Pick a date") : t("Pick a week")} mode={kind === "day" ? "day" : "week"}
          initial={{ from: start, to: end }} onPick={(rng) => p.setAnchor(rng.from)} />
      )}
    </View>
  );
}
