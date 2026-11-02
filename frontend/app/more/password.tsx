// Change your own password (owner, 8+ characters) or PIN (worker, 4+). Logs out your other phones (and takes
// them off the owner's notifications).
import { useState } from "react";
import { useRouter } from "expo-router";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { syncPush } from "@/src/push";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Field, Header, Screen, T } from "@/src/components/ui";
import { colors, spacing } from "@/src/theme";

export default function ChangePassword() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const { user, isOwner, setSession } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const min = isOwner ? 8 : 4;
  const word = isOwner ? t("password") : t("PIN");
  const ok = (!user?.has_password || current.length > 0) && next.length >= min && next === again;

  async function save() {
    setBusy(true);
    try {
      await setSession(await apiPost("/auth/password", { current_password: current, new_password: next }));
      syncPush(isOwner); // a new password takes all of the owner's phones off notifications; this one signs up again
      toast.show(t("Updated. You have been logged out on your other phones."), "success");
      router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const pinProps = isOwner ? {} : { keyboardType: "number-pad" as const };
  return (
    <Screen bg={colors.surface}>
      <Header title={user?.has_password ? (isOwner ? t("Change password") : t("Change PIN")) : t("Set a password")} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24}>
        {user?.has_password && (
          <Field label={t("Current {{word}}", { word })} value={current} onChangeText={setCurrent} secureTextEntry {...pinProps} testID="pw-current" />
        )}
        <Field label={t("New {{word}}", { word })} value={next} onChangeText={setNext} secureTextEntry {...pinProps} testID="pw-new"
          hint={t("At least {{n}} characters", { n: min })} />
        <Field label={t("New {{word}} again", { word })} value={again} onChangeText={setAgain} secureTextEntry {...pinProps} testID="pw-again"
          error={again && again !== next ? t("These do not match") : undefined} />
        <T v="small">{t("You stay logged in on this phone. Other phones using your login are logged out.")}</T>
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Save")} icon="lock-check" onPress={save} loading={busy} disabled={!ok} testID="pw-save" />
      </BottomBar>
    </Screen>
  );
}
