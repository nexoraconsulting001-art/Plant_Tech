// Stands in front of the whole app (app/_layout.tsx) in the two cases where it must not be used:
// - the build has no server address (a broken local setup; release builds refuse to build without one);
// - the server says this version is too old (GET /api/config min_app_version): "Update the app" with a button
//   to the store, or to the server's update_url. Until the server answers, or when offline, the app runs as usual.
import type { ReactNode } from "react";
import { Linking, View } from "react-native";
import { useTranslation } from "react-i18next";

import { APP_VERSION, BACKEND_URL, isTooOld, updateUrl, useAppConfig } from "@/src/app-config";
import { BrandMark } from "@/src/components/brand";
import { Button, Screen, T } from "@/src/components/ui";
import { colors, spacing } from "@/src/theme";

export function AppGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const config = useAppConfig();

  if (!BACKEND_URL) {
    return (
      <Blocked title={t("This app is not set up")} testID="gate-no-server"
        message={t("This copy of the app has no server address. Please install the app again from the link you were given.")} />
    );
  }
  if (isTooOld(config.data)) {
    const url = updateUrl(config.data);
    return (
      <Blocked title={t("Please update the app")} testID="gate-update"
        message={t("This version ({{v}}) is no longer supported. Update to the latest version to keep billing.", { v: APP_VERSION })}
        action={url ? <Button title={t("Update now")} icon="download" onPress={() => Linking.openURL(url).catch(() => {})} testID="gate-update-open" /> : null} />
    );
  }
  return <>{children}</>;
}

function Blocked({ title, message, action, testID }: { title: string; message: string; action?: ReactNode; testID: string }) {
  return (
    <Screen bg={colors.surface}>
      <View testID={testID} style={{ flex: 1, justifyContent: "center", alignItems: "center", gap: spacing.lg, padding: spacing.xl }}>
        <BrandMark size={56} />
        <T v="title" style={{ textAlign: "center" }}>{title}</T>
        <T v="body" color={colors.inkSoft} style={{ textAlign: "center" }}>{message}</T>
        {action ? <View style={{ alignSelf: "stretch", marginTop: spacing.md }}>{action}</View> : null}
      </View>
    </Screen>
  );
}
