// The app's own camera (owner, 10 Nov 2026: "skip the camera's review screen, go straight to the crop screen").
// The phone's camera app, which expo-image-picker opens, shows its own review screen (X / tick) after every photo
// and that can't be switched off; this full-screen camera takes the photo and hands it straight to the crop screen.
// <CameraHost /> sits once in app/_layout.tsx; useImageCapture calls openCamera(). Not on the web (the browser's file
// picker stays). If the camera fails to start, openCamera answers "unavailable" and the phone's camera app opens
// instead, as before.
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Modal, Platform, Pressable, View } from "react-native";
import { CameraView } from "expo-camera";
import { requireOptionalNativeModule } from "expo-modules-core";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTranslation } from "react-i18next";

import { Icon, T } from "@/src/components/ui";
import { colors, spacing } from "@/src/theme";

export type Shot = { kind: "photo"; uri: string } | { kind: "cancel" } | { kind: "unavailable" };

// The camera module is in every app build (it was in the first one); checked anyway so an app without it falls back.
export const inAppCamera = Platform.OS !== "web" && !!requireOptionalNativeModule("ExpoCamera");

let open: ((hint?: string) => Promise<Shot>) | null = null;

export function openCamera(hint?: string): Promise<Shot> {
  return open && inAppCamera ? open(hint) : Promise.resolve<Shot>({ kind: "unavailable" });
}

export function CameraHost() {
  const [job, setJob] = useState<{ hint?: string } | null>(null);
  const [shown, setShown] = useState(false);
  const done = useRef<((s: Shot) => void) | null>(null); // the waiting openCamera(); answered once
  const shot = useRef<Shot>({ kind: "cancel" });

  useEffect(() => {
    open = (hint) => new Promise<Shot>((resolve) => {
      done.current = resolve;
      setJob({ hint });
      setShown(true);
    });
    return () => {
      open = null;
    };
  }, []);

  const answer = () => {
    const resolve = done.current;
    done.current = null;
    setJob(null);
    resolve?.(shot.current);
  };
  // iOS can't open the crop screen while the camera is still closing: the answer waits for onDismiss there.
  const finish = (s: Shot) => {
    if (!done.current || !shown) return;
    shot.current = s;
    setShown(false);
    if (Platform.OS !== "ios") answer();
  };

  if (!inAppCamera) return null;
  return (
    <Modal visible={shown} animationType="fade" statusBarTranslucent navigationBarTranslucent
      onDismiss={Platform.OS === "ios" ? answer : undefined} onRequestClose={() => finish({ kind: "cancel" })}>
      {shown && job && <CameraScreen hint={job.hint} onShot={finish} />}
    </Modal>
  );
}

function CameraScreen({ hint, onShot }: { hint?: string; onShot: (s: Shot) => void }) {
  const { t } = useTranslation();
  const insets = useSafeAreaInsets();
  const cam = useRef<CameraView>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState(false);

  async function shoot() {
    if (!cam.current || busy || !ready) return;
    setBusy(true);
    try {
      const pic = await cam.current.takePictureAsync({ quality: 0.9, exif: false });
      if (pic?.uri) onShot({ kind: "photo", uri: pic.uri });
      else setBusy(false);
    } catch {
      onShot({ kind: "unavailable" });
    }
  }

  return (
    <View style={{ flex: 1, backgroundColor: "#000" }}>
      <CameraView ref={cam} style={{ flex: 1 }} facing="back" flash={flash ? "on" : "off"} animateShutter
        onCameraReady={() => setReady(true)} onMountError={() => onShot({ kind: "unavailable" })} />
      <View style={{ position: "absolute", top: insets.top + spacing.sm, left: spacing.sm, right: spacing.sm, flexDirection: "row",
        justifyContent: "space-between" }}>
        <RoundButton icon="close" label={t("Close camera")} onPress={() => onShot({ kind: "cancel" })} testID="camera-close" />
        <RoundButton icon={flash ? "flash" : "flash-off"} label={flash ? t("Flash on") : t("Flash off")} onPress={() => setFlash((f) => !f)}
          testID="camera-flash" />
      </View>
      <View style={{ position: "absolute", left: 0, right: 0, bottom: insets.bottom + spacing.xl, alignItems: "center", gap: spacing.lg }}>
        {!!hint && (
          <View style={{ paddingHorizontal: spacing.md, paddingVertical: 6, borderRadius: 999, backgroundColor: "rgba(0,0,0,0.55)" }}>
            <T v="small" color={colors.white}>{hint}</T>
          </View>
        )}
        <Pressable onPress={shoot} disabled={!ready || busy} accessibilityRole="button" accessibilityLabel={t("Take photo")} testID="camera-shutter"
          style={{ width: 84, height: 84, borderRadius: 42, borderWidth: 5, borderColor: colors.white, alignItems: "center",
            justifyContent: "center", opacity: ready ? 1 : 0.5 }}>
          {({ pressed }) => busy || !ready ? <ActivityIndicator color={colors.white} />
            : <View style={{ width: 64, height: 64, borderRadius: 32, backgroundColor: pressed ? "#D5E9DB" : colors.white }} />}
        </Pressable>
      </View>
    </View>
  );
}

function RoundButton({ icon, label, onPress, testID }: {
  icon: "close" | "flash" | "flash-off"; label: string; onPress: () => void; testID: string;
}) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label} testID={testID} hitSlop={8}
      style={{ width: 52, height: 52, borderRadius: 26, alignItems: "center", justifyContent: "center", backgroundColor: "rgba(0,0,0,0.45)" }}>
      <Icon name={icon} size={28} color={colors.white} />
    </Pressable>
  );
}
