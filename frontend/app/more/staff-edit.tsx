// Add a worker (name, phone, PIN, what they can do) or change one. A new PIN or switching off
// logs the worker out on every phone.
import { useState } from "react";
import { Linking, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiGet, apiPatch, apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Field, Header, LoadingView, PhoneField, Screen, T, Toggle } from "@/src/components/ui";
import { formatPhone, normalizePhone } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";
import type { Worker } from "@/src/types";

type Perms = Worker["permissions"];
const DEFAULT: Perms = { sell: true, stock: true, customers: true, reports: false };
const MIN_PIN = 6; // as the server (auth.MIN_PIN): new and changed PINs; older 4-digit ones keep working until changed

export default function StaffEdit() {
  const { t } = useTranslation();
  const { id } = useLocalSearchParams<{ id?: string }>();
  const list = useQuery<Worker[]>({ queryKey: ["staff"], queryFn: () => apiGet("/staff"), enabled: !!id });
  const worker = list.data?.find((w) => w.id === id);
  if (id && !worker) return <Screen bg={colors.surface}><Header title={t("Employee")} /><LoadingView /></Screen>;
  return <WorkerForm worker={worker} />;
}

function WorkerForm({ worker }: { worker?: Worker }) {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { user } = useAuth();
  const id = worker?.id;
  const isNew = !worker;
  const [name, setName] = useState(worker?.name ?? "");
  const [phone, setPhone] = useState("");
  const [pin, setPin] = useState("");
  const [perms, setPerms] = useState<Perms>(worker?.permissions ?? DEFAULT);
  const [active, setActive] = useState(worker?.active ?? true);
  const [busy, setBusy] = useState(false);

  const phone10 = normalizePhone(phone);
  const ok = !!name.trim() && (isNew ? !!phone10 && pin.length >= MIN_PIN : pin.length === 0 || pin.length >= MIN_PIN);
  const first = name.trim().split(" ")[0] || t("this employee");

  const PERMS: { p: keyof Perms; label: string; hint: string }[] = [
    { p: "sell", label: t("Make bills"), hint: t("New orders and payments") },
    { p: "stock", label: t("Add stock"), hint: t("Add products and stock, supplier bills") },
    { p: "customers", label: t("Customers"), hint: t("See and add customers") },
    { p: "reports", label: t("Sales reports"), hint: t("All shop sales and profit") },
  ];

  function shareLogin(phoneE164: string) {
    const text = t("Your Hara Hisaab login for {{shop}}:\nPhone: {{phone}}\nPIN: {{pin}}\nOpen the Hara Hisaab app and tap Log in.",
      { shop: user?.business?.name ?? "", phone: formatPhone(phoneE164), pin });
    Linking.openURL(`https://wa.me/${phoneE164.replace(/\D/g, "")}?text=${encodeURIComponent(text)}`).catch(() => {});
  }

  async function save(share: boolean) {
    setBusy(true);
    try {
      if (isNew) {
        const w: Worker = await apiPost("/staff", { name: name.trim(), phone: `+91${phone10}`, password: pin, permissions: perms });
        if (share) shareLogin(w.phone);
      } else {
        await apiPatch(`/staff/${id}`, { name: name.trim(), permissions: perms, active, ...(pin ? { password: pin } : {}) });
        if (share && worker) shareLogin(worker.phone);
      }
      qc.invalidateQueries({ queryKey: ["staff"] });
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
      <Header title={isNew ? t("Add employee") : worker!.name} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24}>
        <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" testID="staff-name" />
        {isNew ? (
          <PhoneField label={t("Phone number (used to log in)")} value={phone} onChangeText={setPhone} testID="staff-phone" />
        ) : (
          <View><T v="label">{t("Phone number (used to log in)")}</T><T v="bodyLarge">{formatPhone(worker!.phone)}</T></View>
        )}
        <Field label={isNew ? t("PIN or password") : t("New PIN (leave blank to keep the current one)")} value={pin} onChangeText={setPin}
          keyboardType="number-pad" maxLength={12} placeholder={t("At least 6 digits")} testID="staff-pin" inputStyle={{ letterSpacing: 4, fontSize: 24 }} />

        <View style={{ gap: spacing.xs, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.page }}>
          <T v="label">{t("What can {{name}} do?", { name: first })}</T>
          {PERMS.map(({ p, label, hint }) => (
            <Toggle key={p} title={label} subtitle={hint} value={perms[p]} onChange={(v) => setPerms((o) => ({ ...o, [p]: v }))} testID={`staff-perm-${p}`} />
          ))}
        </View>
        {!isNew && (
          <Toggle title={t("Can log in")} subtitle={active ? t("Turn off when they leave") : t("Turned off: logged out on all devices")}
            value={active} onChange={setActive} testID="staff-active" />
        )}
      </KeyboardAwareScrollView>
      <BottomBar>
        {(isNew || pin) ? (
          <>
            <Button title={t("Save and share login on WhatsApp")} icon="whatsapp" variant="whatsapp" onPress={() => save(true)} loading={busy} disabled={!ok} testID="staff-save-share" />
            <Button title={t("Save only")} variant="outline" size="md" onPress={() => save(false)} disabled={!ok || busy} testID="staff-save" />
          </>
        ) : (
          <Button title={t("Save")} icon="content-save" onPress={() => save(false)} loading={busy} disabled={!ok} testID="staff-save" />
        )}
      </BottomBar>
    </Screen>
  );
}
