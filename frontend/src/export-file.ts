// Files for the phone: a report export downloaded from the server (with the login), then shared (WhatsApp, Drive,
// email) or saved into a folder on the phone. Android asks once which folder to save in (it opens on Downloads) and
// remembers it; iPhone has no Downloads folder, so "save" opens the share sheet (its "Save to Files"). In a computer's
// browser the file simply downloads. Also used for the bill PDF (src/bill-file.ts).
import { Platform } from "react-native";
import * as Sharing from "expo-sharing";
import { Directory, File, Paths } from "expo-file-system";
import i18n from "i18next";

import { API, ApiError, authHeaders } from "@/src/api";
import { storage } from "@/src/utils/storage";

const FOLDER_KEY = "nurserypos_bill_folder_v1"; // one remembered folder for bills and exports
const DOWNLOADS = "content://com.android.externalstorage.documents/tree/primary%3ADownload";

export type FileResult = "saved" | "shared" | "opened" | "cancelled";

// The file into the phone's folder (asked once), or the share sheet where there is no such folder.
export async function saveToPhone(file: File, name: string, mime: string, uti?: string): Promise<FileResult> {
  if (Platform.OS !== "android") {
    await Sharing.shareAsync(file.uri, { mimeType: mime, UTI: uti });
    return "shared";
  }
  const bytes = await file.bytes();
  const saved = await storage.getItem<string | null>(FOLDER_KEY, null);
  if (saved) {
    try {
      new Directory(saved).createFile(name, mime).write(bytes);
      return "saved";
    } catch {
      // The phone forgot the folder permission: ask again below.
    }
  }
  let folder: Directory;
  try {
    folder = await Directory.pickDirectoryAsync(DOWNLOADS);
  } catch {
    return "cancelled";
  }
  folder.createFile(name, mime).write(bytes);
  await storage.setItem(FOLDER_KEY, folder.uri);
  return "saved";
}

const failed = () => new ApiError(i18n.t("The file could not be downloaded. Please try again."), 0, "download");

// GET `path` (a server export) as the file `name`: shared or saved on the phone, downloaded in a browser.
export async function exportFile(path: string, name: string, mime: string, how: "share" | "save"): Promise<FileResult> {
  if (Platform.OS === "web") {
    let res: Response;
    try {
      res = await fetch(`${API}${path}`, { headers: authHeaders() });
    } catch {
      throw failed();
    }
    if (!res.ok) throw failed();
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    return "saved";
  }
  let file: File;
  try {
    file = await File.downloadFileAsync(`${API}${path}`, new File(Paths.cache, name), { headers: authHeaders(), idempotent: true });
  } catch {
    throw failed();
  }
  if (how === "save") return saveToPhone(file, name, mime);
  await Sharing.shareAsync(file.uri, { mimeType: mime, dialogTitle: name });
  return "shared";
}
