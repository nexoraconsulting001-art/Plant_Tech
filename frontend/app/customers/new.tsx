// Add a customer from the Customers tab. "Save and start order" goes straight to adding items for them.
import { useState } from "react";
import { useRouter } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { apiPost } from "@/src/api";
import { useAuth } from "@/src/auth";
import { useToast } from "@/src/components/toast";
import { BottomBar, Button, Field, Header, PhoneField, Screen } from "@/src/components/ui";
import { useStartOrder } from "@/src/hooks/useStartOrder";
import { normalizePhone } from "@/src/format";
import { colors, spacing } from "@/src/theme";

export default function NewCustomer() {
  const { t } = useTranslation();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { can } = useAuth();
  const startOrder = useStartOrder();
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [address, setAddress] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "order">("");
  const phone10 = normalizePhone(phone);
  const phoneBad = !!phone && !phone10;

  async function save(then: "back" | "order") {
    setBusy(then === "order" ? "order" : "save");
    try {
      const c = await apiPost("/customers", { name: name.trim(), phone: `+91${phone10}`, address, notes });
      qc.invalidateQueries({ queryKey: ["customers"] });
      toast.show(t("Saved"), "success");
      if (then === "order") await startOrder({ id: c.id, name: c.name, phone: c.phone, purchase_count: 0 }, true);
      else router.back();
    } catch (e: any) {
      toast.show(e.message, "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Add customer")} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.lg }} bottomOffset={24}>
        <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" autoFocus testID="customer-name" />
        <PhoneField label={t("Phone (WhatsApp)")} value={phone} onChangeText={setPhone} testID="customer-phone"
          error={phoneBad && phone.length === 10 ? t("Enter a 10-digit mobile number") : undefined} />
        <Field label={t("Address (optional)")} value={address} onChangeText={setAddress} multiline />
        <Field label={t("Notes (optional)")} value={notes} onChangeText={setNotes} multiline placeholder={t("e.g. Likes flowering plants")} />
      </KeyboardAwareScrollView>
      <BottomBar>
        <Button title={t("Save customer")} icon="content-save" onPress={() => save("back")} loading={busy === "save"}
          disabled={!name.trim() || !phone10 || !!busy} testID="customer-save" />
        {can("sell") && (
          <Button title={t("Save and start order")} icon="cart-plus" variant="outline" size="md" onPress={() => save("order")} loading={busy === "order"}
            disabled={!name.trim() || !phone10 || !!busy} testID="customer-save-order" />
        )}
      </BottomBar>
    </Screen>
  );
}
