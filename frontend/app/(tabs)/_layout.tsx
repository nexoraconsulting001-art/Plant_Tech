// Bottom tabs. Owner: Home · Orders · Products · Customers · More.
// Worker: the same, minus what the owner hasn't allowed, and "Me" instead of More.
import { useEffect } from "react";
import { Redirect, Tabs, useRouter } from "expo-router";
import type { ColorValue } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTranslation } from "react-i18next";

import { useAuth } from "@/src/auth";
import { Icon, IconName } from "@/src/components/ui";
import { takePendingOpen } from "@/src/nav";
import { syncPush, usePushTaps } from "@/src/push";
import { colors, fonts, spacing } from "@/src/theme";

const icon = (name: IconName) => function TabIcon({ color }: { color: ColorValue }) {
  return <Icon name={name} size={28} color={color as string} />;
};

export default function TabsLayout() {
  const { t, i18n } = useTranslation();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const { ready, user, isOwner, can } = useAuth();
  const showing = ready && !!user && !(isOwner && !user.business?.onboarding_done);

  useEffect(() => {
    if (!showing) return;
    const next = takePendingOpen(); // e.g. Add product, asked for on the last setup screen
    if (next) router.push(next);
  }, [showing, router]);

  // Phone notifications (owners only): sign this phone up after login and when the language changes.
  useEffect(() => {
    if (showing) syncPush(isOwner);
  }, [showing, isOwner, user?.id, i18n.language]);
  usePushTaps(showing && isOwner);

  if (!ready) return null;
  if (!user) return <Redirect href="/welcome" />;
  if (isOwner && !user.business?.onboarding_done) return <Redirect href="/setup/shop" />;

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.green,
        tabBarInactiveTintColor: colors.muted,
        tabBarStyle: { height: 72 + insets.bottom, paddingTop: spacing.sm, paddingBottom: insets.bottom + 8, borderTopColor: colors.line, backgroundColor: colors.surface },
        tabBarLabelStyle: { fontFamily: fonts.bold, fontSize: 13 },
        tabBarItemStyle: { paddingHorizontal: 0 },
        sceneStyle: { backgroundColor: colors.page },
      }}
    >
      <Tabs.Screen name="index" options={{ title: t("Home"), tabBarIcon: icon("home"), tabBarButtonTestID: "tab-home" }} />
      <Tabs.Screen name="orders" options={{ title: t("Orders"), tabBarIcon: icon("receipt-text"), tabBarButtonTestID: "tab-orders",
        href: can("sell") ? undefined : null }} />
      <Tabs.Screen name="products" options={{ title: t("Products"), tabBarIcon: icon("sprout"), tabBarButtonTestID: "tab-products",
        href: can("sell") || can("stock") ? undefined : null }} />
      <Tabs.Screen name="customers" options={{ title: t("Customers"), tabBarIcon: icon("account-group"), tabBarButtonTestID: "tab-customers",
        href: can("customers") ? undefined : null }} />
      <Tabs.Screen name="more" options={{ title: isOwner ? t("More") : t("Me"), tabBarButtonTestID: "tab-more",
        tabBarIcon: icon(isOwner ? "dots-horizontal-circle" : "account-circle") }} />
    </Tabs>
  );
}
