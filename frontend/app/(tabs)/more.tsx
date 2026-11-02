// Owner: More (reports, staff, shop, notifications, categories, supplier bill, shared pictures).
// Worker: Me (what the owner lets them do, change PIN, log out).
// Everyone: privacy policy and terms (links from GET /api/config or the build, src/app-config.ts), Delete account.
import { useState } from "react";
import { ScrollView, View } from "react-native";
import Constants from "expo-constants";
import { useRouter } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiGet } from "@/src/api";
import { useLegalLinks } from "@/src/app-config";
import { Permission, useAuth } from "@/src/auth";
import { BrandMark } from "@/src/components/brand";
import { confirm } from "@/src/confirm";
import { LanguageSheet, useLanguage } from "@/src/components/language";
import { Icon, IconName, Row, Screen, T } from "@/src/components/ui";
import { formatPhone, plural } from "@/src/format";
import { colors, radius, spacing } from "@/src/theme";
import { resetTo } from "@/src/nav";

function MenuIcon({ name, tint = colors.greenTint, color = colors.green }: { name: IconName; tint?: string; color?: string }) {
  return (
    <View style={{ width: 46, height: 46, borderRadius: 14, backgroundColor: tint, alignItems: "center", justifyContent: "center" }}>
      <Icon name={name} size={26} color={color} />
    </View>
  );
}

export default function More() {
  const { t } = useTranslation();
  const router = useRouter();
  const { user, isOwner, can, logout } = useAuth();
  const language = useLanguage();
  const [choosing, setChoosing] = useState(false);
  const legal = useLegalLinks();
  const open = (url: string) => WebBrowser.openBrowserAsync(url).catch(() => {});
  const staff = useQuery<any[]>({ queryKey: ["staff"], queryFn: () => apiGet("/staff"), enabled: isOwner });
  const shop = user?.business?.name ?? "";

  async function out() {
    if (await confirm(t("Log out?"), t("You will need your phone number and password to log in again."), t("Log out"))) {
      await logout();
      resetTo(router, "/welcome");
    }
  }

  const PERMS: { p: Permission; label: string }[] = [
    { p: "sell", label: t("Make bills") }, { p: "stock", label: t("Add stock") },
    { p: "customers", label: t("Customers") }, { p: "reports", label: t("Sales reports") },
  ];

  return (
    <Screen edgeBottom={false}>
      <ScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.md, paddingBottom: spacing["2xl"] }}>
        <T v="title" style={{ marginBottom: spacing.xs }}>{isOwner ? t("More") : t("Me")}</T>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.surface,
          borderWidth: 1, borderColor: colors.line }}>
          <View style={{ width: 60, height: 60, borderRadius: 30, backgroundColor: colors.green, alignItems: "center", justifyContent: "center" }}>
            <T v="title" color={colors.white}>{(user?.name || "?").charAt(0).toUpperCase()}</T>
          </View>
          <View style={{ flex: 1 }}>
            <T v="bodyLarge">{user?.name}</T>
            <T v="small">{isOwner ? t("Owner · {{shop}}", { shop }) : t("Employee at {{shop}}", { shop })}</T>
            {!!user?.phone && <T v="small">{formatPhone(user.phone)}</T>}
          </View>
        </View>

        {isOwner ? (
          <>
            <Row title={t("Sales reports")} subtitle={t("Daily, weekly and monthly sales")} left={<MenuIcon name="chart-bar" />} chevron
              onPress={() => router.push("/more/reports")} testID="more-reports" />
            <Row title={t("Employees")} left={<MenuIcon name="account-multiple" />} chevron testID="more-staff"
              subtitle={staff.data ? [plural(staff.data.length, "1 employee", "{{n}} employees"), t("Sales by employee")].join(" · ") : t("Sales by employee and login access")}
              onPress={() => router.push("/more/staff")} />
            <Row title={t("Shop details")} subtitle={t("Name, address, UPI, GST")} left={<MenuIcon name="store" />} chevron
              onPress={() => router.push("/more/shop")} testID="more-shop" />
            <Row title={t("Notifications")} subtitle={t("Low stock and the daily sales summary")} left={<MenuIcon name="bell-outline" />} chevron
              onPress={() => router.push("/more/notifications")} testID="more-notifications" />
            <Row title={t("Categories")} subtitle={t("Names and GST rates")} left={<MenuIcon name="shape" />} chevron
              onPress={() => router.push("/more/categories")} testID="more-categories" />
            <Row title={t("Supplier bill")} subtitle={t("Add stock from a supplier bill")} chevron testID="more-supplier"
              left={<MenuIcon name="file-document-outline" tint={colors.blueTint} color={colors.blue} />} onPress={() => router.push("/more/supplier-bill")} />
            <Row title={t("Suppliers")} subtitle={t("Contacts and bills from each supplier")} chevron testID="more-suppliers"
              left={<MenuIcon name="truck-outline" tint={colors.blueTint} color={colors.blue} />} onPress={() => router.push("/more/suppliers")} />
            {user?.is_admin && (
              <Row title={t("Shared pictures")} subtitle={t("Visible to app administrators only")} chevron testID="more-library"
                left={<MenuIcon name="image-multiple" tint={colors.amberTint} color={colors.amber} />} onPress={() => router.push("/more/library")} />
            )}
          </>
        ) : (
          <View style={{ gap: spacing.md, padding: spacing.lg, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line }}>
            <T v="label">{t("You can")}</T>
            {PERMS.map(({ p, label }) => (
              <View key={p} style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
                <Icon name={can(p) ? "check-circle" : "close-circle-outline"} size={26} color={can(p) ? colors.green : colors.muted} />
                <T v="bold" color={can(p) ? colors.ink : colors.muted}>{label}</T>
              </View>
            ))}
            <T v="small" style={{ marginTop: spacing.xs }}>{t("Forgot your PIN? Please ask the shop owner to set a new one.")}</T>
          </View>
        )}
        {!isOwner && can("reports") && (
          <Row title={t("Sales reports")} left={<MenuIcon name="chart-bar" />} chevron onPress={() => router.push("/more/reports")} />
        )}

        <Row title={user?.has_password ? (isOwner ? t("Change password") : t("Change PIN")) : t("Set a password")}
          left={<MenuIcon name="lock-reset" tint={colors.page} color={colors.inkSoft} />} chevron onPress={() => router.push("/more/password")} />
        <Row title={t("Language")} subtitle={language.label} left={<MenuIcon name="translate" tint={colors.page} color={colors.inkSoft} />}
          chevron onPress={() => setChoosing(true)} testID="more-language" />
        {!!legal.privacy && (
          <Row title={t("Privacy policy")} left={<MenuIcon name="shield-lock-outline" tint={colors.page} color={colors.inkSoft} />}
            right={<Icon name="open-in-new" size={22} color={colors.muted} />} onPress={() => open(legal.privacy)} testID="more-privacy" />
        )}
        {!!legal.terms && (
          <Row title={t("Terms of use")} left={<MenuIcon name="file-document-check-outline" tint={colors.page} color={colors.inkSoft} />}
            right={<Icon name="open-in-new" size={22} color={colors.muted} />} onPress={() => open(legal.terms)} testID="more-terms" />
        )}
        <Row title={t("Log out")} left={<MenuIcon name="logout" tint="#FDECEC" color={colors.red} />} onPress={out} testID="more-logout" />
        <Row title={t("Delete account")} subtitle={isOwner ? t("Close the shop and delete its data") : t("Delete your login")}
          left={<MenuIcon name="account-remove-outline" tint={colors.page} color={colors.red} />} chevron
          onPress={() => router.push("/account/delete")} testID="more-delete-account" />

        <View style={{ alignItems: "center", gap: 2, marginTop: spacing.lg }}>
          <BrandMark size={36} />
          <T v="bold" color={colors.greenDark}>हरा हिसाब · Hara Hisaab</T>
          <T v="caption">{t("Version {{v}}", { v: Constants.expoConfig?.version ?? "" })}</T>
        </View>
      </ScrollView>
      <LanguageSheet visible={choosing} onClose={() => setChoosing(false)} />
    </Screen>
  );
}
