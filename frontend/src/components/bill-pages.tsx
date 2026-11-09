// The pages of a bill or a written order, photographed one by one: each is uploaded as soon as it is added (a
// thumbnail with a spinner, then a retry if the upload failed) and can be removed. Used by the supplier bill and the
// customer's paper order.
import { ActivityIndicator, Pressable, View } from "react-native";
import { Image } from "expo-image";
import { useTranslation } from "react-i18next";

import { Icon, T } from "@/src/components/ui";
import { colors, radius } from "@/src/theme";

export type Page = { key: string; uri: string; base64: string; path: string | null; state: "uploading" | "done" | "failed" };

export function PageThumb({ page, n, onRemove, onRetry, testPrefix = "sb-page" }: {
  page: Page; n: number; onRemove: () => void; onRetry: () => void; testPrefix?: string;
}) {
  const { t } = useTranslation();
  return (
    <View testID={`${testPrefix}-${n}`} style={{ width: 96, gap: 6 }}>
      <View style={{ width: 96, height: 120, borderRadius: radius.md, overflow: "hidden", backgroundColor: colors.page, borderWidth: 1, borderColor: colors.line }}>
        <Image source={{ uri: page.uri }} style={{ width: "100%", height: "100%" }} contentFit="cover" />
        {page.state !== "done" && (
          <Pressable disabled={page.state !== "failed"} onPress={onRetry} accessibilityLabel={t("Try again")}
            style={{ position: "absolute", left: 0, top: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center",
              backgroundColor: "rgba(14,26,20,0.45)" }}>
            {page.state === "uploading" ? <ActivityIndicator color={colors.white} /> : <Icon name="refresh" size={30} color={colors.white} />}
          </Pressable>
        )}
        <Pressable onPress={onRemove} accessibilityLabel={t("Remove page {{n}}", { n })} hitSlop={8}
          style={{ position: "absolute", right: 4, top: 4, width: 30, height: 30, borderRadius: 15, backgroundColor: colors.surface,
            alignItems: "center", justifyContent: "center" }}>
          <Icon name="close" size={18} color={colors.ink} />
        </Pressable>
      </View>
      <T v="caption" style={{ textAlign: "center" }}>
        {page.state === "failed" ? t("Upload failed") : t("Page {{n}}", { n })}
      </T>
    </View>
  );
}
