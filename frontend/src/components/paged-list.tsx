// Pieces shared by the lists that load a page at a time (Products, Customers; see src/hooks/usePagedList.ts).
import { ActivityIndicator, View } from "react-native";
import { useTranslation } from "react-i18next";

import { Button } from "@/src/components/ui";
import { colors, spacing } from "@/src/theme";

// Space between rows (FlashList has no `gap`).
export function RowGap() {
  return <View style={{ height: spacing.sm }} />;
}

// Under the list: a spinner while the next page loads, or Try again when it didn't arrive.
export function MoreFooter({ loading, failed, onRetry }: { loading: boolean; failed: boolean; onRetry: () => void }) {
  const { t } = useTranslation();
  if (loading) return <ActivityIndicator color={colors.green} style={{ paddingVertical: spacing.lg }} testID="list-more-loading" />;
  if (failed) return (
    <View style={{ alignItems: "center", paddingVertical: spacing.lg }}>
      <Button title={t("Try again")} icon="refresh" variant="outline" size="md" onPress={onRetry} testID="list-more-retry" />
    </View>
  );
  return null;
}
