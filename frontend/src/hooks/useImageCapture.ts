// Taking or picking a photo: camera / gallery → the app's own crop screen → a small JPEG (base64)
// ready to upload. The phone's own editor is not used (it adds rotate and flip and can't be styled).
import * as ImagePicker from "expo-image-picker";
import i18n from "i18next";

import { CropOptions, cropImage } from "@/src/components/crop-host";
import { cropAndShrink, normalize } from "@/src/image-tools";

export type CaptureResult = { uri: string; base64: string };
export type CaptureOutcome =
  | { status: "ok"; result: CaptureResult }
  | { status: "denied"; canAskAgain: boolean }
  | { status: "cancelled" };
// aspect 1 = square (product photos). longEdge: LONG_EDGE.plant for plant and product photos; bills keep the default.
export type CaptureOptions = Pick<CropOptions, "aspect" | "title" | "hint"> & { longEdge?: number };

const PICK: ImagePicker.ImagePickerOptions = { mediaTypes: ["images"], quality: 1, allowsEditing: false, exif: false };

async function allowed(source: "camera" | "gallery") {
  return source === "camera" ? ImagePicker.requestCameraPermissionsAsync() : ImagePicker.requestMediaLibraryPermissionsAsync();
}

async function capture(source: "camera" | "gallery", options: CaptureOptions = {}): Promise<CaptureOutcome> {
  const perm = await allowed(source);
  if (!perm.granted) return { status: "denied", canAskAgain: perm.canAskAgain };
  for (;;) {
    const res = source === "camera" ? await ImagePicker.launchCameraAsync(PICK) : await ImagePicker.launchImageLibraryAsync(PICK);
    if (res.canceled || !res.assets?.[0]) return { status: "cancelled" };
    const picked = await normalize(res.assets[0].uri);
    const answer = await cropImage(picked, options);
    if (answer.kind === "use") return { status: "ok", result: await cropAndShrink(picked, answer.rect, options.longEdge) };
    if (answer.kind === "cancel") return { status: "cancelled" };
    // Retake: open the camera (or gallery) again.
  }
}

export const takePhoto = (options?: CaptureOptions) => capture("camera", options);
export const pickFromGallery = (options?: CaptureOptions) => capture("gallery", options);

// Several pages from the gallery at once (a long supplier bill); each is cropped in turn and can be skipped.
export async function pickPages(limit: number, options: CaptureOptions = {}):
  Promise<{ status: "ok"; results: CaptureResult[] } | { status: "denied"; canAskAgain: boolean } | { status: "cancelled" }> {
  const perm = await allowed("gallery");
  if (!perm.granted) return { status: "denied", canAskAgain: perm.canAskAgain };
  const res = await ImagePicker.launchImageLibraryAsync({ ...PICK, allowsMultipleSelection: true, selectionLimit: limit, orderedSelection: true });
  if (res.canceled || !res.assets?.length) return { status: "cancelled" };
  const assets = res.assets.slice(0, limit);
  const results: CaptureResult[] = [];
  for (const [i, asset] of assets.entries()) {
    const picked = await normalize(asset.uri);
    const answer = await cropImage(picked, {
      ...options,
      title: assets.length > 1 ? i18n.t("Page {{n}} of {{total}}", { n: i + 1, total: assets.length }) : options.title,
      secondary: assets.length > 1 ? i18n.t("Skip page") : undefined,
    });
    if (answer.kind === "cancel") break;
    if (answer.kind === "use") results.push(await cropAndShrink(picked, answer.rect, options.longEdge));
  }
  return results.length ? { status: "ok", results } : { status: "cancelled" };
}
