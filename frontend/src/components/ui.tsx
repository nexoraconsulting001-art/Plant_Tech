// UI kit matching the approved prototype: icon + label on actions, 56+ touch targets, big numbers.
import React, { createContext, useContext, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleProp,
  StyleSheet,
  Text,
  TextInput,
  TextInputProps,
  TextProps,
  View,
  ViewStyle,
} from "react-native";
import { Image } from "expo-image";
import * as Haptics from "expo-haptics";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTranslation } from "react-i18next";
import MaterialDesignIcons from "@react-native-vector-icons/material-design-icons";

import { fileUrl } from "@/src/api";
import { formatQty } from "@/src/format";
import { colors, fonts, hindiRoom, radius, spacing, touch, type, TextVariant } from "@/src/theme";
import { MAX_QTY, roundQty } from "@/src/units";

export type IconName = React.ComponentProps<typeof MaterialDesignIcons>["name"];

export function Icon(props: React.ComponentProps<typeof MaterialDesignIcons>) {
  return <MaterialDesignIcons {...props} />;
}

// Phones set to "large text" can scale ours up to 25% more; beyond that our big type breaks layouts.
export const MAX_FONT_SCALE = 1.25;

export function T({ v = "body", color, style, ...rest }: TextProps & { v?: TextVariant; color?: string }) {
  const flat = StyleSheet.flatten([type[v], style]);
  return <Text maxFontSizeMultiplier={MAX_FONT_SCALE} {...rest}
    style={[type[v], color ? { color } : null, style, hindiRoom(rest.children, flat.fontSize ?? 16, flat.lineHeight)]} />;
}

// ------------------------------ layout ------------------------------
// Keeps everything clear of the phone's status bar and its bottom buttons. Tab screens pass
// edgeBottom={false}: the tab bar already sits above the buttons.
const BottomEdge = createContext(0);

export function Screen({ children, bg = colors.page, edgeTop = true, edgeBottom = true, style }: {
  children: React.ReactNode; bg?: string; edgeTop?: boolean; edgeBottom?: boolean; style?: StyleProp<ViewStyle>;
}) {
  const insets = useSafeAreaInsets();
  const bottom = edgeBottom ? insets.bottom : 0;
  return (
    <BottomEdge.Provider value={bottom}>
      <View style={[{ flex: 1, backgroundColor: bg, paddingTop: edgeTop ? insets.top : 0, paddingBottom: bottom }, style]}>{children}</View>
    </BottomEdge.Provider>
  );
}

export function Header({ title, onBack, right, close, testID }: {
  title?: string; onBack?: () => void; right?: React.ReactNode; close?: boolean; testID?: string;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs, paddingHorizontal: spacing.sm, minHeight: 68, backgroundColor: colors.surface }}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={close ? t("Close") : t("Back")}
        testID={testID ?? "header-back"}
        onPress={onBack ?? (() => (router.canGoBack() ? router.back() : router.replace("/")))}
        style={{ width: touch.min, height: touch.min, alignItems: "center", justifyContent: "center" }}
      >
        <Icon name={close ? "close" : "chevron-left"} size={32} color={colors.ink} />
      </Pressable>
      {!!title && <T v="title" style={{ flex: 1 }}>{title}</T>}
      {right}
    </View>
  );
}

// The last thing on a Screen. Its white runs down behind the phone's buttons; its buttons stay above them.
export function BottomBar({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  const edge = useContext(BottomEdge);
  return (
    <View style={[{ gap: spacing.md, paddingHorizontal: spacing.lg, paddingTop: spacing.md, paddingBottom: edge + spacing.lg, marginBottom: -edge,
      backgroundColor: colors.surface, borderTopWidth: 1, borderTopColor: colors.line }, style]}>
      {children}
    </View>
  );
}

// ------------------------------ Button ------------------------------
type Variant = "primary" | "secondary" | "outline" | "danger" | "scan" | "whatsapp" | "dashed";
const VARIANTS: Record<Variant, { bg: string; fg: string; border?: string; dashed?: boolean }> = {
  primary: { bg: colors.green, fg: colors.white },
  secondary: { bg: colors.greenTint, fg: colors.greenDark },
  outline: { bg: colors.surface, fg: colors.ink, border: colors.lineStrong },
  danger: { bg: colors.surface, fg: colors.red, border: colors.redLine },
  scan: { bg: colors.surface, fg: colors.blue, border: colors.blue },
  whatsapp: { bg: colors.whatsapp, fg: colors.white },
  dashed: { bg: colors.surface, fg: colors.green, border: colors.green, dashed: true },
};

export function Button({ title, onPress, variant = "primary", icon, size = "lg", disabled, loading, testID, style }: {
  title: string; onPress: () => void; variant?: Variant; icon?: IconName; size?: "lg" | "md";
  disabled?: boolean; loading?: boolean; testID?: string; style?: StyleProp<ViewStyle>;
}) {
  const v = VARIANTS[variant];
  const off = disabled || loading;
  const fg = off && variant === "primary" ? colors.muted : v.fg;
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityState={{ disabled: !!off }}
      disabled={off}
      onPress={() => {
        Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
        onPress();
      }}
      style={({ pressed }) => [{
        minHeight: size === "lg" ? touch.primary : touch.min,
        borderRadius: size === "lg" ? radius.lg : radius.md,
        backgroundColor: off && variant === "primary" ? colors.line : v.bg,
        borderWidth: v.border ? 2 : 0, borderColor: v.border, borderStyle: v.dashed ? "dashed" : "solid",
        flexDirection: "row", alignItems: "center", justifyContent: "center", gap: spacing.md, paddingHorizontal: spacing.lg,
        opacity: pressed ? 0.85 : 1,
      }, style]}
    >
      {loading ? <ActivityIndicator color={fg} /> : icon ? <Icon name={icon} size={size === "lg" ? 28 : 24} color={fg} /> : null}
      <Text maxFontSizeMultiplier={MAX_FONT_SCALE}
        style={[{ fontFamily: fonts.bold, fontSize: size === "lg" ? 20 : 18, lineHeight: size === "lg" ? 26 : 23, color: fg, textAlign: "center", flexShrink: 1 },
          hindiRoom(title, size === "lg" ? 20 : 18)]}>{title}</Text>
    </Pressable>
  );
}

// ------------------------------ inputs ------------------------------
// The one search field on every list: magnifier, text, and a clear button (or a spinner while searching).
export function SearchBox({ value, onChangeText, placeholder, testID, autoFocus, loading, autoCapitalize = "none", ref }: {
  value: string; onChangeText: (v: string) => void; placeholder: string; testID?: string; autoFocus?: boolean; loading?: boolean;
  autoCapitalize?: TextInputProps["autoCapitalize"]; ref?: React.Ref<TextInput>;
}) {
  const { t } = useTranslation();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm, minHeight: 52, paddingLeft: spacing.md, paddingRight: spacing.xs,
      borderWidth: 2, borderColor: value ? colors.green : colors.lineStrong, borderRadius: radius.md, backgroundColor: colors.surface }}>
      <Icon name="magnify" size={22} color={colors.muted} />
      <TextInput ref={ref} value={value} onChangeText={onChangeText} placeholder={placeholder} placeholderTextColor={colors.muted}
        autoFocus={autoFocus} autoCapitalize={autoCapitalize} autoCorrect={false} maxFontSizeMultiplier={MAX_FONT_SCALE} testID={testID}
        style={{ flex: 1, fontFamily: fonts.semibold, fontSize: 18, color: colors.ink, minHeight: 48 }} />
      {loading ? <ActivityIndicator color={colors.green} style={{ width: 44 }} /> : !!value && (
        <Pressable accessibilityLabel={t("Clear")} onPress={() => onChangeText("")}
          style={{ width: 44, height: 44, alignItems: "center", justifyContent: "center" }}>
          <Icon name="close-circle" size={22} color={colors.muted} />
        </Pressable>
      )}
    </View>
  );
}

export function Field({ label, hint, error, style, inputStyle, ...props }: TextInputProps & {
  label?: string; hint?: string; error?: string; style?: StyleProp<ViewStyle>; inputStyle?: any;
}) {
  return (
    <View style={[{ gap: 6, justifyContent: "flex-end" }, style]}>
      {!!label && <T v="label">{label}</T>}
      <TextInput
        placeholderTextColor={colors.muted}
        maxFontSizeMultiplier={MAX_FONT_SCALE}
        {...props}
        style={[{ minHeight: touch.min + 2, borderWidth: 2, borderColor: error ? colors.red : colors.lineStrong, borderRadius: radius.md,
          paddingHorizontal: spacing.lg, fontFamily: fonts.semibold, fontSize: 20, color: colors.ink, backgroundColor: colors.surface },
          props.multiline ? { paddingVertical: spacing.md, minHeight: 96, textAlignVertical: "top" } : null, inputStyle]}
      />
      {!!(error || hint) && <T v="small" color={error ? colors.red : colors.muted}>{error || hint}</T>}
    </View>
  );
}

export function PhoneField({ label, value, onChangeText, error, hint, testID, style }: {
  label?: string; value: string; onChangeText: (v: string) => void; error?: string; hint?: string; testID?: string; style?: StyleProp<ViewStyle>;
}) {
  return (
    <View style={[{ gap: 6 }, style]}>
      {!!label && <T v="label">{label}</T>}
      <View style={{ flexDirection: "row", alignItems: "stretch", minHeight: touch.min + 2, borderWidth: 2, borderColor: error ? colors.red : colors.lineStrong,
        borderRadius: radius.md, overflow: "hidden", backgroundColor: colors.surface }}>
        <View style={{ paddingHorizontal: spacing.lg, justifyContent: "center", backgroundColor: colors.page, borderRightWidth: 2, borderRightColor: colors.lineStrong }}>
          <T v="bold" style={{ fontSize: 20 }}>+91</T>
        </View>
        <TextInput
          testID={testID}
          value={value}
          onChangeText={(t) => onChangeText(t.replace(/\D/g, "").slice(-10))}
          keyboardType="phone-pad"
          maxLength={10}
          maxFontSizeMultiplier={MAX_FONT_SCALE}
          placeholder="98765 43210"
          placeholderTextColor={colors.muted}
          style={{ flex: 1, paddingHorizontal: spacing.lg, fontFamily: fonts.semibold, fontSize: 22, color: colors.ink, letterSpacing: 1 }}
        />
      </View>
      {!!(error || hint) && <T v="small" color={error ? colors.red : colors.muted}>{error || hint}</T>}
    </View>
  );
}

export function Toggle({ title, subtitle, value, onChange, testID }: {
  title: string; subtitle?: string; value: boolean; onChange: (v: boolean) => void; testID?: string;
}) {
  return (
    <Pressable testID={testID} accessibilityRole="switch" accessibilityState={{ checked: value }} onPress={() => onChange(!value)}
      style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, minHeight: touch.min }}>
      <View style={{ flex: 1 }}>
        <T v="bodyLarge">{title}</T>
        {!!subtitle && <T v="small">{subtitle}</T>}
      </View>
      <View style={{ width: 60, height: 34, borderRadius: 17, padding: 3, backgroundColor: value ? colors.green : "#9DB5A6",
        alignItems: value ? "flex-end" : "flex-start", justifyContent: "center" }}>
        <View style={{ width: 28, height: 28, borderRadius: 14, backgroundColor: colors.white }} />
      </View>
    </Pressable>
  );
}

export function Chip({ label, active, onPress, right, testID, tone = "green" }: {
  label: string; active?: boolean; onPress?: () => void; right?: React.ReactNode; testID?: string; tone?: "green" | "amber";
}) {
  const on = tone === "amber" ? colors.amber : colors.green;
  return (
    <Pressable testID={testID} onPress={onPress} accessibilityRole="button" accessibilityState={{ selected: !!active }}
      style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm, minHeight: 52, paddingHorizontal: spacing.lg, borderRadius: 26, borderWidth: 2,
        borderColor: active ? on : colors.lineStrong, backgroundColor: active ? on : colors.surface }}>
      {active && tone === "green" && <Icon name="check" size={20} color={colors.white} />}
      <Text maxFontSizeMultiplier={MAX_FONT_SCALE} style={{ fontFamily: fonts.bold, fontSize: 17, color: active ? colors.white : colors.ink }}>{label}</Text>
      {right}
    </Pressable>
  );
}

export function Segmented<V extends string>({ options, value, onChange }: {
  options: { value: V; label: string }[]; value: V; onChange: (v: V) => void;
}) {
  return (
    <View style={{ flexDirection: "row", gap: 6, padding: 5, borderRadius: radius.md + 2, backgroundColor: "#E3EBE5" }}>
      {options.map((o) => (
        <Pressable key={o.value} testID={`seg-${o.value}`} onPress={() => onChange(o.value)} accessibilityRole="tab" accessibilityState={{ selected: o.value === value }}
          style={{ flex: 1, minHeight: 50, paddingHorizontal: spacing.sm, paddingVertical: 6, borderRadius: 12, alignItems: "center", justifyContent: "center",
            backgroundColor: o.value === value ? colors.green : "transparent" }}>
          <Text maxFontSizeMultiplier={MAX_FONT_SCALE}
            style={[{ fontFamily: fonts.bold, fontSize: 16, lineHeight: 20, textAlign: "center", color: o.value === value ? colors.white : colors.ink },
              hindiRoom(o.label, 16)]}>{o.label}</Text>
        </Pressable>
      ))}
    </View>
  );
}

// How many: − / + buttons, and the number itself can be typed (number pad for pieces, decimals for kg,
// litre, metre). A typed amount is rounded to the unit and kept between min and max (never above MAX_QTY);
// one over the max shows amber until the field is left, then snaps to the max.
export function Stepper({ value, onChange, min = 1, max = 9999, decimals, unit, testID }: {
  value: number; onChange: (v: number) => void; min?: number; max?: number; decimals?: boolean; unit?: string; testID?: string;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState<string | null>(null); // what is being typed; null when not typing
  const top = Math.min(max, MAX_QTY);
  const fit = (n: number) => Math.max(min, Math.min(decimals ? top : Math.floor(top), roundQty(n, !decimals)));
  const step = (n: number) => {
    setText(null);
    onChange(fit(n));
  };
  const shown = text ?? formatQty(value);
  const over = text !== null && Number(text) > top;
  const btn = { width: 52, height: 52, borderRadius: 14, alignItems: "center" as const, justifyContent: "center" as const };
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
      <Pressable testID={testID ? `${testID}-minus` : undefined} accessibilityLabel={t("Decrease quantity")}
        onPress={() => step(value - 1 >= min ? value - 1 : min === 0 ? 0 : value)}
        style={[btn, { borderWidth: 2, borderColor: colors.lineStrong, backgroundColor: colors.surface }]}>
        <Icon name="minus" size={24} color={colors.ink} />
      </Pressable>
      <View style={{ minWidth: 72, height: 52, paddingHorizontal: spacing.xs, borderWidth: 2, borderRadius: radius.md, alignItems: "center", justifyContent: "center",
        borderColor: over ? colors.amber : colors.lineStrong, backgroundColor: colors.surface }}>
        <TextInput value={shown} keyboardType={decimals ? "decimal-pad" : "number-pad"} selectTextOnFocus maxFontSizeMultiplier={MAX_FONT_SCALE}
          accessibilityLabel={t("Quantity")} testID={testID ? `${testID}-value` : undefined}
          onFocus={() => setText(formatQty(value))} onBlur={() => setText(null)}
          onChangeText={(v) => {
            const clean = decimals ? v.replace(/[^0-9.]/g, "").replace(/(\..*)\./g, "$1").replace(/(\.\d{3})\d+/, "$1") : v.replace(/\D/g, "");
            setText(clean);
            const n = Number(clean);
            if (clean !== "" && Number.isFinite(n) && n > 0) onChange(fit(n));
          }}
          style={{ width: Math.max(60, 14 * shown.length + 4), height: unit ? 30 : 44, padding: 0, textAlign: "center",
            fontFamily: fonts.display, fontSize: 24, color: colors.ink }} />
        {!!unit && <T v="caption" style={{ fontSize: 12, lineHeight: 15 }} testID={testID ? `${testID}-unit` : undefined}>{unit}</T>}
      </View>
      <Pressable testID={testID ? `${testID}-plus` : undefined} accessibilityLabel={t("Increase quantity")} onPress={() => step(value + 1)}
        style={[btn, { backgroundColor: colors.green }]}>
        <Icon name="plus" size={24} color={colors.white} />
      </Pressable>
    </View>
  );
}

// ------------------------------ display ------------------------------
export function Card({ children, onPress, style, testID }: {
  children: React.ReactNode; onPress?: () => void; style?: StyleProp<ViewStyle>; testID?: string;
}) {
  const base: ViewStyle = { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, padding: spacing.lg };
  return onPress ? (
    <Pressable testID={testID} onPress={onPress} style={({ pressed }) => [base, { opacity: pressed ? 0.85 : 1 }, style]}>{children}</Pressable>
  ) : (
    <View testID={testID} style={[base, style]}>{children}</View>
  );
}

export function Thumb({ path, size = 56, tint = colors.greenTint, icon = "sprout", contain }: {
  path?: string | null; size?: number; tint?: string; icon?: IconName; contain?: boolean; // contain: show all of it (logos)
}) {
  const { t } = useTranslation();
  return (
    <View style={{ width: size, height: size, borderRadius: size * 0.24, overflow: "hidden", backgroundColor: tint, alignItems: "center", justifyContent: "center" }}>
      {path ? <Image source={{ uri: fileUrl(path) }} style={{ width: "100%", height: "100%" }} contentFit={contain ? "contain" : "cover"} accessibilityLabel={contain ? t("Shop logo") : t("Product picture")} />
        : <Icon name={icon} size={size * 0.5} color={colors.greenMid} />}
    </View>
  );
}

const BADGE = {
  green: { bg: colors.greenTint, fg: colors.greenDark },
  amber: { bg: colors.amber, fg: colors.white },
  red: { bg: colors.red, fg: colors.white },
  blue: { bg: colors.blueTint, fg: colors.blueInk },
  grey: { bg: colors.page, fg: colors.inkSoft },
};
export function Badge({ text, tone = "green" }: { text: string; tone?: keyof typeof BADGE }) {
  return (
    <View style={{ alignSelf: "flex-start", paddingHorizontal: spacing.md, paddingVertical: spacing.xs, borderRadius: 12, backgroundColor: BADGE[tone].bg }}>
      <Text style={{ fontFamily: fonts.extrabold, fontSize: 14, color: BADGE[tone].fg }}>{text}</Text>
    </View>
  );
}

export function Row({ title, subtitle, left, right, onPress, testID, chevron }: {
  title: string; subtitle?: string; left?: React.ReactNode; right?: React.ReactNode; onPress?: () => void; testID?: string; chevron?: boolean;
}) {
  return (
    <Pressable testID={testID} disabled={!onPress} onPress={onPress}
      style={({ pressed }) => ({ flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.lg, paddingVertical: spacing.md, minHeight: 68,
        borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, opacity: pressed ? 0.85 : 1 })}>
      {left}
      <View style={{ flex: 1, minWidth: 0 }}>
        <T v="bold">{title}</T>
        {!!subtitle && <T v="small">{subtitle}</T>}
      </View>
      {right}
      {chevron && <Icon name="chevron-right" size={26} color={colors.muted} />}
    </Pressable>
  );
}

export function SectionTitle({ children, right }: { children: string; right?: React.ReactNode }) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", minHeight: 40 }}>
      <T v="label" style={{ fontFamily: fonts.bold, fontSize: 18 }}>{children}</T>
      {right}
    </View>
  );
}

export function EmptyState({ icon = "sprout", title, subtitle, action }: {
  icon?: IconName; title: string; subtitle?: string; action?: React.ReactNode;
}) {
  return (
    <View style={{ alignItems: "center", paddingVertical: spacing["2xl"], paddingHorizontal: spacing.lg, gap: spacing.md }}>
      <View style={{ width: 88, height: 88, borderRadius: 44, backgroundColor: colors.greenTint, alignItems: "center", justifyContent: "center" }}>
        <Icon name={icon} size={44} color={colors.green} />
      </View>
      <T v="bodyLarge" style={{ textAlign: "center" }}>{title}</T>
      {!!subtitle && <T v="small" style={{ textAlign: "center" }}>{subtitle}</T>}
      {action}
    </View>
  );
}

export function LoadingView({ label }: { label?: string }) {
  return (
    <View style={{ flex: 1, alignItems: "center", justifyContent: "center", padding: spacing.xl, gap: spacing.md }}>
      <ActivityIndicator size="large" color={colors.green} />
      {!!label && <T v="small">{label}</T>}
    </View>
  );
}

export function ErrorView({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <View style={{ alignItems: "center", padding: spacing.xl, gap: spacing.md }}>
      <Icon name="wifi-off" size={40} color={colors.red} />
      <T v="body" style={{ textAlign: "center" }}>{message}</T>
      {onRetry && <Button title={t("Try again")} icon="refresh" variant="outline" size="md" onPress={onRetry} />}
    </View>
  );
}
