// Bottom sheet (slides up over the screen) and the small pieces used inside it:
// option rows with a tick, a select field that opens a sheet, and filter chips.
// The sheet rises with the keyboard on Android too (the keyboard library's KeyboardAvoidingView, which works inside a
// Modal; React Native's own did nothing there, so a sheet's text field was under the keyboard, QA 10 Nov 2026).
import React from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTranslation } from "react-i18next";

import { Icon, IconName, MAX_FONT_SCALE, T } from "@/src/components/ui";
import { colors, fonts, radius, spacing, touch } from "@/src/theme";

export function Sheet({ visible, onClose, title, children, footer, testID }: {
  visible: boolean; onClose: () => void; title?: string; children: React.ReactNode; footer?: React.ReactNode; testID?: string;
}) {
  const { t } = useTranslation();
  const insets = useSafeAreaInsets();
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose} statusBarTranslucent navigationBarTranslucent>
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1, justifyContent: "flex-end" }}>
        <Pressable accessibilityLabel={t("Close")} onPress={onClose} style={[StyleSheet.absoluteFill, { backgroundColor: "rgba(16,32,26,0.45)" }]} />
        <View testID={testID} style={{ width: "100%", maxWidth: 480, alignSelf: "center", maxHeight: "90%", backgroundColor: colors.surface,
          borderTopLeftRadius: 24, borderTopRightRadius: 24, paddingBottom: insets.bottom + spacing.lg }}>
          <View style={{ alignItems: "center", paddingTop: spacing.sm }}>
            <View style={{ width: 44, height: 5, borderRadius: 3, backgroundColor: colors.lineStrong }} />
          </View>
          <View style={{ flexDirection: "row", alignItems: "center", paddingLeft: spacing.lg, paddingRight: spacing.xs, minHeight: 56 }}>
            <T v="title" style={{ flex: 1, fontSize: 22, lineHeight: 28 }}>{title}</T>
            <Pressable accessibilityRole="button" accessibilityLabel={t("Close")} onPress={onClose} testID="sheet-close"
              style={{ width: touch.min, height: touch.min, alignItems: "center", justifyContent: "center" }}>
              <Icon name="close" size={28} color={colors.ink} />
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={{ paddingHorizontal: spacing.lg, paddingBottom: spacing.sm, gap: spacing.md }} keyboardShouldPersistTaps="handled">
            {children}
          </ScrollView>
          {!!footer && <View style={{ paddingHorizontal: spacing.lg, paddingTop: spacing.md, gap: spacing.md }}>{footer}</View>}
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// One choice in a list: big row, tick on the chosen one.
export function OptionRow({ label, sub, selected, onPress, icon, testID }: {
  label: string; sub?: string; selected?: boolean; onPress: () => void; icon?: IconName; testID?: string;
}) {
  return (
    <Pressable testID={testID} accessibilityRole="radio" accessibilityState={{ selected: !!selected }} onPress={onPress}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, minHeight: 60, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm,
        borderRadius: radius.md, borderWidth: 2, borderColor: selected ? colors.green : colors.line,
        backgroundColor: selected ? colors.greenTint : pressed ? colors.page : colors.surface })}>
      {!!icon && <Icon name={icon} size={24} color={selected ? colors.green : colors.inkSoft} />}
      <View style={{ flex: 1 }}>
        <T v="bold" color={selected ? colors.greenDark : colors.ink}>{label}</T>
        {!!sub && <T v="small">{sub}</T>}
      </View>
      <Icon name={selected ? "check-circle" : "circle-outline"} size={26} color={selected ? colors.green : colors.lineStrong} />
    </Pressable>
  );
}

// Looks like a text field; tapping opens a sheet to choose.
export function SelectField({ label, value, placeholder, onPress, testID, right }: {
  label?: string; value?: string; placeholder?: string; onPress: () => void; testID?: string; right?: React.ReactNode;
}) {
  return (
    <View style={{ gap: 6 }}>
      {!!label && <T v="label">{label}</T>}
      <Pressable testID={testID} accessibilityRole="button" onPress={onPress}
        style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, minHeight: touch.min + 2, paddingHorizontal: spacing.lg,
          borderWidth: 2, borderColor: colors.lineStrong, borderRadius: radius.md, backgroundColor: pressed ? colors.page : colors.surface })}>
        <Text maxFontSizeMultiplier={MAX_FONT_SCALE} style={{ flex: 1, fontFamily: fonts.semibold, fontSize: 20, color: value ? colors.ink : colors.muted }}>
          {value || placeholder}
        </Text>
        {right}
        <Icon name="chevron-down" size={26} color={colors.inkSoft} />
      </Pressable>
    </View>
  );
}

// Filter button in a filter bar: shows the filter name, or what is chosen (highlighted). Compact, so three
// fit side by side on a 360 px phone.
export function FilterChip({ label, value, onPress, testID }: {
  label: string; value?: string; onPress: () => void; testID?: string;
}) {
  const on = !!value;
  return (
    <Pressable testID={testID} accessibilityRole="button" onPress={onPress}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: 2, minHeight: 44, paddingLeft: spacing.md, paddingRight: 6,
        borderRadius: 22, borderWidth: 2, borderColor: on ? colors.green : colors.lineStrong,
        backgroundColor: on ? colors.greenTint : pressed ? colors.page : colors.surface })}>
      <Text maxFontSizeMultiplier={MAX_FONT_SCALE} style={{ fontFamily: fonts.bold, fontSize: 15, color: on ? colors.greenDark : colors.ink }}>
        {value || label}
      </Text>
      <Icon name="chevron-down" size={18} color={on ? colors.green : colors.inkSoft} />
    </Pressable>
  );
}
