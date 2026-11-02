// More → Notifications (owner only): low-stock alerts and the daily sales summary on the owner's phone, the
// summary time, and whether this phone gets them. The choices are the shop's (every phone the owner uses);
// the top card is about this phone. Employees never get these notifications.
import { useCallback, useEffect, useState } from "react";
import { AppState, Linking, ScrollView, View } from "react-native";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet, apiPost, apiPut } from "@/src/api";
import { OptionRow, SelectField, Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Button, Card, ErrorView, Header, Icon, LoadingView, Screen, T, Toggle } from "@/src/components/ui";
import { PushState, pushState, registerPush } from "@/src/push";
import { colors, spacing } from "@/src/theme";

type Settings = { low_stock: boolean; summary: boolean; summary_time: string; phones: number; push_on: boolean };

// 5 pm to 11 pm, every half hour (India time)
const TIMES = Array.from({ length: 13 }, (_, i) => `${String(17 + Math.floor(i / 2))}:${i % 2 ? "30" : "00"}`);

function useTimeLabel() {
  const { t } = useTranslation();
  return (hhmm: string) => {
    const [h, m] = hhmm.split(":").map(Number);
    const time = `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")}`;
    return h < 12 ? t("{{time}} am", { time }) : t("{{time}} pm", { time });
  };
}

export default function NotificationsScreen() {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const timeLabel = useTimeLabel();
  const s = useQuery<Settings>({ queryKey: ["notify-settings"], queryFn: () => apiGet("/notifications/settings") });
  const [phone, setPhone] = useState<PushState | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [testing, setTesting] = useState(false);

  // This phone's state, again when the owner comes back from the phone's settings.
  const check = useCallback(async (): Promise<PushState> => {
    const state = await pushState();
    const now = state === "on" ? await registerPush(false) : state; // allowed in the phone's settings: sign up now
    qc.invalidateQueries({ queryKey: ["notify-settings"] });
    return now;
  }, [qc]);
  useEffect(() => {
    let alive = true;
    const run = () => check().then((state) => alive && setPhone(state));
    run();
    const sub = AppState.addEventListener("change", (a) => a === "active" && run());
    return () => {
      alive = false;
      sub.remove();
    };
  }, [check]);

  async function save(patch: Partial<Settings>) {
    const before = s.data;
    if (before) qc.setQueryData<Settings>(["notify-settings"], { ...before, ...patch });
    try {
      qc.setQueryData(["notify-settings"], await apiPut("/notifications/settings", patch));
    } catch (e: any) {
      if (before) qc.setQueryData(["notify-settings"], before);
      toast.show(e.message, "error");
    }
  }

  async function allow() {
    const state = await registerPush(true);
    setPhone(state);
    if (state === "off") Linking.openSettings().catch(() => {}); // the phone no longer shows the prompt: its settings do
    qc.invalidateQueries({ queryKey: ["notify-settings"] });
  }

  async function test() {
    setTesting(true);
    try {
      const r: { sent: number; push_on: boolean } = await apiPost("/notifications/test");
      toast.show(!r.push_on ? t("Notifications are not running on the server yet.")
        : r.sent ? t("Test sent. It should arrive in a few seconds.") : t("This phone is not signed up yet. Please try again."),
        r.push_on && r.sent ? undefined : "error");
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setTesting(false);
    }
  }

  const d = s.data;
  return (
    <Screen bg={colors.surface}>
      <Header title={t("Notifications")} />
      <ScrollView contentContainerStyle={{ padding: spacing.lg, paddingTop: spacing.xs, gap: spacing.lg, paddingBottom: spacing.xl }}>
        <T v="body" color={colors.inkSoft}>{t("Alerts about your shop on your phone. Only you get them, not your employees.")}</T>

        {phone && <PhoneCard state={phone} onAllow={allow} onTest={test} testing={testing} />}

        {s.isLoading && <LoadingView />}
        {s.isError && <ErrorView message={(s.error as Error).message} onRetry={s.refetch} />}
        {d && (
          <View style={{ gap: spacing.md }}>
            <Toggle title={t("Low-stock alerts")} value={d.low_stock} onChange={(v) => save({ low_stock: v })} testID="notify-low"
              subtitle={t("When a product reaches its low-stock alert level or runs out. At most once a day for each product.")} />
            <Toggle title={t("Daily sales summary")} value={d.summary} onChange={(v) => save({ summary: v })} testID="notify-summary"
              subtitle={t("Today's sales by Cash, UPI and Card, and how many products are low. Not sent on a day without bills.")} />
            {d.summary && (
              <SelectField label={t("Send the summary at")} value={timeLabel(d.summary_time)} onPress={() => setChoosing(true)} testID="notify-time" />
            )}
          </View>
        )}
      </ScrollView>

      <Sheet visible={choosing} onClose={() => setChoosing(false)} title={t("Send the summary at")} testID="notify-time-sheet">
        {TIMES.map((x) => (
          <OptionRow key={x} label={timeLabel(x)} selected={d?.summary_time === x} testID={`notify-time-${x}`}
            onPress={() => { setChoosing(false); save({ summary_time: x }); }} />
        ))}
      </Sheet>
    </Screen>
  );
}

function PhoneCard({ state, onAllow, onTest, testing }: {
  state: PushState; onAllow: () => void; onTest: () => void; testing: boolean;
}) {
  const { t } = useTranslation();
  const on = state === "on";
  return (
    <Card style={{ gap: spacing.md }} testID="notify-phone">
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
        <Icon name={on ? "bell-ring-outline" : "bell-off-outline"} size={26} color={on ? colors.green : colors.inkSoft} />
        <View style={{ flex: 1 }}>
          <T v="bold">{on ? t("This phone gets notifications") : state === "off" ? t("Notifications are off on this phone")
            : t("Notifications come to the app on your phone")}</T>
          <T v="small">{on ? t("Send a test to check that they arrive.")
            : state === "off" ? t("Allow notifications for Hara Hisaab to get these alerts.")
            : t("They need the Hara Hisaab app on an Android phone, updated to the latest version.")}</T>
        </View>
      </View>
      {on && <Button title={t("Send a test notification")} icon="bell-outline" variant="secondary" size="md" loading={testing}
        onPress={onTest} testID="notify-test" />}
      {state === "off" && <Button title={t("Allow notifications")} icon="bell-outline" size="md" onPress={onAllow} testID="notify-allow" />}
    </Card>
  );
}
