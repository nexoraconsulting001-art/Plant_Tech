// Delete my account (Play Store and App Store rule: an app that makes accounts must let people delete them).
// Owner: the shop closes now (everyone is logged out) and is erased after a waiting period; logging in again
// before then keeps it. Bills, supplier bills and stock records that GST law says must be kept go to a locked
// archive first. Employee: only their own login is erased, at once; the shop keeps the bills they made.
// Confirmed by typing the phone number and the password or PIN; an owner without a password signs in with Google
// again (the server checks the password or the Google account).
import { useState } from "react";
import { View } from "react-native";
import { Redirect, useRouter } from "expo-router";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { deleteAccount } from "@/src/account";
import { useAuth } from "@/src/auth";
import { confirm, notice } from "@/src/confirm";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Field, Header, Icon, PhoneField, Screen, T } from "@/src/components/ui";
import { googleAvailable, signInWithGoogle } from "@/src/google";
import { resetTo } from "@/src/nav";
import { colors, radius, spacing } from "@/src/theme";

const last10 = (phone?: string | null) => (phone ?? "").replace(/\D/g, "").slice(-10);

export default function DeleteAccount() {
  const { t, i18n } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const { ready, user, isOwner, logout } = useAuth();
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const myPhone = last10(user?.phone);
  const word = isOwner ? t("password") : t("PIN");
  const phoneOk = !myPhone || phone === myPhone;
  const ok = phoneOk && (!user?.has_password || password.length > 0);

  async function remove() {
    const sure = await confirm(t("Delete your account?"),
      isOwner ? t("Your shop closes now and everyone in it is logged out.") : t("Your login is deleted now. This cannot be undone."),
      t("Delete"), true);
    if (!sure) return;
    setBusy(true);
    try {
      const id_token = !user?.has_password && googleAvailable ? await signInWithGoogle() : undefined;
      const out = await deleteAccount({ confirm_phone: phone, password, id_token, reason: reason.trim() });
      const until = out.erase_after
        ? new Date(out.erase_after).toLocaleDateString(i18n.language === "hi" ? "hi-IN" : "en-IN", { day: "numeric", month: "long", year: "numeric" })
        : "";
      await notice(t("Account deleted"), out.status === "scheduled" && until
        ? t("Your shop will be deleted for good on {{date}}. To keep it, log in again before then.", { date: until })
        : t("Your account has been deleted."));
      await logout();
      resetTo(router, "/welcome");
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (ready && !user) return <Redirect href="/welcome" />;
  const pinProps = isOwner ? {} : { keyboardType: "number-pad" as const };
  return (
    <Screen bg={colors.surface}>
      <Header title={t("Delete account")} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24}>
        <View style={{ flexDirection: "row", gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.redTint }}>
          <Icon name="alert-circle-outline" size={28} color={colors.red} />
          <View style={{ flex: 1, gap: spacing.sm }}>
            {isOwner ? (
              <>
                <T v="bold">{t("Your shop and everything in it will be deleted: products, customers, bills and employees' logins.")}</T>
                <T v="small" color={colors.inkSoft}>{t("The shop closes now and is deleted for good after a waiting period. Log in again before then if you change your mind.")}</T>
                <T v="small" color={colors.inkSoft}>{t("Bills and stock records that GST law requires are kept in a locked archive for as long as the law says. Nobody can see them in the app.")}</T>
              </>
            ) : (
              <>
                <T v="bold">{t("Your login will be deleted at once.")}</T>
                <T v="small" color={colors.inkSoft}>{t("Bills you made stay with the shop.")}</T>
              </>
            )}
          </View>
        </View>
        {!!myPhone && (
          <PhoneField label={t("Type your phone number to confirm")} value={phone} onChangeText={setPhone} testID="delete-phone"
            error={phone.length === 10 && !phoneOk ? t("This is not your phone number") : undefined} />
        )}
        {user?.has_password && (
          <Field label={t("Your {{word}}", { word })} value={password} onChangeText={setPassword} secureTextEntry {...pinProps} testID="delete-password" />
        )}
        <Field label={t("Why are you leaving? (optional)")} value={reason} onChangeText={setReason} multiline maxLength={500} testID="delete-reason" />
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Delete my account")} variant="danger" icon="delete-outline" onPress={remove} loading={busy} disabled={!ok} testID="delete-confirm" />
      </BottomBar>
    </Screen>
  );
}
