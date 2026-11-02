import "@/src/i18n";

import { QueryClientProvider } from "@tanstack/react-query";
import { Stack, useNavigationContainerRef } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { LogBox, Platform, View } from "react-native";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import { useFonts, Baloo2_700Bold, Baloo2_800ExtraBold } from "@expo-google-fonts/baloo-2";
import { Mukta_400Regular, Mukta_600SemiBold, Mukta_700Bold, Mukta_800ExtraBold } from "@expo-google-fonts/mukta";
import { useEffect } from "react";

import { AuthProvider, useAuth } from "@/src/auth";
import { CartProvider } from "@/src/cart-context";
import { ConfirmHost } from "@/src/confirm";
import { AppGate } from "@/src/components/app-gate";
import { CropHost } from "@/src/components/crop-host";
import { ErrorBoundary, RouteErrorBoundary } from "@/src/components/error-boundary";
import { ToastProvider } from "@/src/components/toast";
import { queryClient } from "@/src/query-client";
import { setReportUser, trackNavigation, wrapRoot } from "@/src/sentry";
import { colors } from "@/src/theme";

// Development builds only: hide these known, harmless warnings by their exact opening words. Everything else
// still shows, so a new warning gets noticed. Add to the list rather than silencing all of them.
LogBox.ignoreLogs([
  "[expo-notifications] Push notifications (remote notifications) are unavailable in Expo Go", // Expo Go only
]);
const WEB = Platform.OS === "web";
if (WEB && typeof document !== "undefined") {
  // The browser draws its own focus ring inside our bordered boxes; phones don't, so match them.
  const style = document.createElement("style");
  style.textContent = "input:focus, textarea:focus { outline: none; }";
  document.head.appendChild(style);
}
SplashScreen.preventAutoHideAsync().catch(() => {});

// Error reports carry the logged-in user's id and shop id (src/sentry.ts).
function ReportUser() {
  const { user } = useAuth();
  useEffect(() => setReportUser(user), [user]);
  return null;
}

function RootLayout() {
  const navigationRef = useNavigationContainerRef();
  useEffect(() => trackNavigation(navigationRef), [navigationRef]);
  const [fontsLoaded, fontError] = useFonts({
    Baloo2_700Bold, Baloo2_800ExtraBold, Mukta_400Regular, Mukta_600SemiBold, Mukta_700Bold, Mukta_800ExtraBold,
  });
  const ready = fontsLoaded || !!fontError;

  useEffect(() => {
    if (ready) SplashScreen.hideAsync().catch(() => {});
  }, [ready]);

  if (!ready) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: WEB ? "#DCE6DF" : colors.page }}>
      {/* On a computer's browser, show the app at phone width so testing there matches the phone. */}
      <View style={WEB ? { flex: 1, width: "100%", maxWidth: 480, alignSelf: "center", overflow: "hidden", backgroundColor: colors.page } : { flex: 1 }}>
      <ErrorBoundary>
        <QueryClientProvider client={queryClient}>
          <SafeAreaProvider>
            <KeyboardProvider>
              <AuthProvider>
                <ReportUser />
                <CartProvider>
                  <ToastProvider>
                    <StatusBar style="dark" />
                    <AppGate>
                      {/* A crash inside one screen (tabs included) shows RouteErrorBoundary there; the rest keeps working. */}
                      <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.page } }}
                        unstable_screenErrorBoundary={RouteErrorBoundary} />
                    </AppGate>
                    <ConfirmHost />
                    <CropHost />
                  </ToastProvider>
                </CartProvider>
              </AuthProvider>
            </KeyboardProvider>
          </SafeAreaProvider>
        </QueryClientProvider>
      </ErrorBoundary>
      </View>
    </GestureHandlerRootView>
  );
}

export default wrapRoot(RootLayout);
