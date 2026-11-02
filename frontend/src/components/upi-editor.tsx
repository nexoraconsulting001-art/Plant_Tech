// The shop's UPI IDs in Shop details (owner): add one, give it a name (e.g. "Owner GPay"), choose the default the
// pay QR starts with, remove one. Changes stay on the screen until Save.
import { useState } from "react";
import { View } from "react-native";
import { useTranslation } from "react-i18next";

import { isUpiId, newUpiKey } from "@/src/business";
import { Sheet } from "@/src/components/sheet";
import { Badge, Button, Field, Row, T } from "@/src/components/ui";
import { spacing } from "@/src/theme";
import type { UpiEntry } from "@/src/types";

const MAX_UPI_IDS = 10; // the server keeps at most this many

export function UpiEditor({ list, defaultId, onChange }: {
  list: UpiEntry[]; defaultId: string | null; onChange: (list: UpiEntry[], defaultId: string | null) => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<{ id: string; isNew: boolean } | null>(null);
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");
  const def = list.some((u) => u.id === defaultId) ? defaultId : list[0]?.id ?? null;

  const open = (u?: UpiEntry) => {
    setEditing(u ? { id: u.id, isNew: false } : { id: newUpiKey(), isNew: true });
    setValue(u?.upi_id ?? "");
    setLabel(u?.label ?? "");
  };
  const close = () => setEditing(null);

  const trimmed = value.trim();
  const bad = !isUpiId(trimmed);
  const dup = !!editing && list.some((u) => u.id !== editing.id && u.upi_id.toLowerCase() === trimmed.toLowerCase());

  // The list with the sheet's edits applied
  const edited = (): UpiEntry[] => {
    const entry = { id: editing!.id, upi_id: trimmed, label: label.trim() };
    return editing!.isNew ? [...list, entry] : list.map((u) => (u.id === entry.id ? entry : u));
  };
  const done = () => { onChange(edited(), def ?? editing!.id); close(); };
  const makeDefault = () => { onChange(edited(), editing!.id); close(); };
  const remove = () => {
    const rest = list.filter((u) => u.id !== editing!.id);
    onChange(rest, def === editing!.id ? rest[0]?.id ?? null : def);
    close();
  };

  return (
    <View style={{ gap: spacing.sm }}>
      <T v="label">{t("UPI IDs (for the payment QR code)")}</T>
      {list.map((u) => (
        <Row key={u.id} title={u.label || u.upi_id} subtitle={u.label ? u.upi_id : undefined} chevron onPress={() => open(u)}
          right={u.id === def ? <Badge text={t("Default")} /> : undefined} testID={`shop-upi-${u.upi_id}`} />
      ))}
      {list.length < MAX_UPI_IDS && (
        <Button title={t("Add UPI ID")} icon="plus" variant="dashed" size="md" onPress={() => open()} testID="shop-upi-add" />
      )}
      <T v="small">
        {list.length ? t("The QR code starts with the default. Anyone billing can switch to another UPI ID before showing it.")
          : t("Add a UPI ID to show customers a QR code with the amount filled in.")}
      </T>

      <Sheet visible={!!editing} onClose={close} title={editing?.isNew ? t("Add UPI ID") : t("UPI ID")} testID="upi-edit-sheet"
        footer={(
          <>
            {!!editing && !editing.isNew && (
              <View style={{ flexDirection: "row", gap: spacing.md }}>
                {editing.id !== def && (
                  <Button title={t("Make default")} icon="star-outline" variant="outline" size="md" style={{ flex: 1 }} onPress={makeDefault}
                    disabled={bad || dup} testID="upi-edit-default" />
                )}
                <Button title={t("Remove")} icon="delete-outline" variant="danger" size="md" style={{ flex: 1 }} onPress={remove} testID="upi-edit-remove" />
              </View>
            )}
            <Button title={t("Done")} icon="check" onPress={done} disabled={bad || dup} testID="upi-edit-done" />
          </>
        )}>
        <Field label={t("UPI ID")} value={value} onChangeText={setValue} autoCapitalize="none" autoCorrect={false} keyboardType="email-address"
          placeholder="shopname@okaxis" maxLength={80} testID="upi-edit-value"
          error={dup ? t("This UPI ID is already in the list") : trimmed && bad ? t("Enter a UPI ID like name@bank") : undefined} />
        <Field label={t("Name (optional)")} hint={t("Helps staff pick the right one, for example Owner GPay")} value={label} onChangeText={setLabel}
          autoCapitalize="words" maxLength={40} placeholder={t("Owner GPay")} testID="upi-edit-label" />
      </Sheet>
    </View>
  );
}
