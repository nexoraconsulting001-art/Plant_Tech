// Error boundaries, both mounted in app/_layout.tsx.
// - RouteErrorBoundary: one per screen (the root Stack's unstable_screenErrorBoundary, which expo-router passes down
//   to the tab screens too). A crash in one screen shows "Try again / Go back" in that screen only; the tabs and
//   every other screen keep working.
// - ErrorBoundary: the whole app, for a crash outside any screen (a provider, a layout). Shows a reload screen
//   instead of a blank app.
// Both log the error (Metro output) and report it to Sentry (src/sentry.ts; nothing is sent when it is off).

import { reloadAppAsync } from "expo";
import type { ErrorBoundaryProps } from "expo-router";
import { useRouter } from "expo-router";
import { Component, type ErrorInfo, type PropsWithChildren, useEffect, useState } from "react";
import { Platform, Pressable, ScrollView, Text, View } from "react-native";

import i18n from "i18next";

import { reportError } from "@/src/sentry";
import { makeStyles } from "@/src/theme";

type ErrorBoundaryState = { error: Error | null };

export class ErrorBoundary extends Component<PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary] render crash:", error, info.componentStack ?? "");
    reportError(error, info.componentStack);
  }

  resetError = (): void => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return <ErrorFallback error={this.state.error} resetError={this.resetError} />;
    }
    return this.props.children;
  }
}

// One screen crashed. expo-router's boundary does not tell anyone, so report it here (once per error).
export function RouteErrorBoundary({ error, retry }: ErrorBoundaryProps) {
  const styles = useStyles();
  const router = useRouter();

  useEffect(() => {
    console.error("[RouteErrorBoundary] screen crash:", error);
    reportError(error);
  }, [error]);

  const leave = () => {
    if (router.canGoBack()) router.back();
    else router.replace("/");
  };

  return (
    <View style={styles.container} testID="route-error">
      <View style={styles.content}>
        <Text style={styles.title}>{i18n.t("This screen could not be opened")}</Text>
        <Text style={styles.message}>{i18n.t("Please try again. If it keeps happening, go back and open it again.")}</Text>
        {__DEV__ ? <Text style={styles.devMessage}>{error.message}</Text> : null}
        <Pressable onPress={() => { retry().catch(() => {}); }} testID="route-error-retry" accessibilityRole="button"
          style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}>
          <Text style={styles.buttonText}>{i18n.t("Try again")}</Text>
        </Pressable>
        <Pressable onPress={leave} testID="route-error-back" accessibilityRole="button" hitSlop={8}>
          <Text style={styles.detailsToggle}>{i18n.t("Go back")}</Text>
        </Pressable>
      </View>
    </View>
  );
}

function ErrorFallback({ error, resetError }: { error: Error; resetError: () => void }) {
  const styles = useStyles();
  const [showDetails, setShowDetails] = useState(false);

  const handleReload = async () => {
    try {
      await reloadAppAsync();
    } catch {
      // Reload is unavailable in some environments; retry the render instead.
      resetError();
    }
  };

  return (
    <View style={styles.container} testID="error-fallback">
      <View style={styles.content}>
        <Text style={styles.title}>{i18n.t("Something went wrong")}</Text>
        <Text style={styles.message}>{i18n.t("Please reload the app to continue.")}</Text>
        {__DEV__ ? <Text style={styles.devMessage}>{error.message}</Text> : null}
        <Pressable
          onPress={handleReload}
          testID="error-fallback-reload"
          accessibilityRole="button"
          style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
        >
          <Text style={styles.buttonText}>{i18n.t("Reload app")}</Text>
        </Pressable>
        {__DEV__ ? (
          <Pressable onPress={() => setShowDetails((v) => !v)} accessibilityRole="button" hitSlop={8}>
            <Text style={styles.detailsToggle}>{showDetails ? "Hide details" : "Show details"}</Text>
          </Pressable>
        ) : null}
      </View>
      {__DEV__ && showDetails ? (
        <ScrollView style={styles.details} contentContainerStyle={styles.detailsContent}>
          <Text selectable style={styles.detailsText}>
            {error.stack ?? error.message}
          </Text>
        </ScrollView>
      ) : null}
    </View>
  );
}

const useStyles = makeStyles((colors) => ({
  container: {
    flex: 1,
    backgroundColor: colors.surface,
    justifyContent: "center",
    padding: 24,
  },
  content: {
    alignItems: "center",
    gap: 12,
  },
  title: {
    color: colors.ink,
    fontSize: 22,
    fontWeight: "700",
    textAlign: "center",
  },
  message: {
    color: colors.muted,
    fontSize: 15,
    textAlign: "center",
  },
  devMessage: {
    color: colors.red,
    fontSize: 13,
    textAlign: "center",
  },
  button: {
    marginTop: 8,
    backgroundColor: colors.green,
    borderRadius: 12,
    paddingHorizontal: 24,
    paddingVertical: 14,
    minWidth: 180,
  },
  buttonPressed: {
    opacity: 0.85,
  },
  buttonText: {
    color: colors.white,
    fontSize: 15,
    fontWeight: "600",
    textAlign: "center",
  },
  detailsToggle: {
    color: colors.muted,
    fontSize: 13,
    textDecorationLine: "underline",
    paddingVertical: 8,
  },
  details: {
    marginTop: 16,
    maxHeight: 260,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.page,
  },
  detailsContent: {
    padding: 12,
  },
  detailsText: {
    color: colors.ink,
    fontSize: 12,
    lineHeight: 18,
    fontFamily: Platform.select({ ios: "Menlo", default: "monospace" }),
  },
}));
