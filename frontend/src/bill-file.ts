// The bill as a PDF: share it (WhatsApp, Drive…) or download it into a folder on the phone (src/export-file.ts:
// Android asks once which folder to save in, it opens on Downloads, and remembers it).
// In a computer's browser both open the bill page, which has its own Download PDF button.
import { Platform } from "react-native";
import * as Print from "expo-print";
import * as Sharing from "expo-sharing";
import { File, Paths } from "expo-file-system";

import { apiGet, fetchText } from "@/src/api";
import { FileResult, saveToPhone } from "@/src/export-file";

const PDF = "application/pdf";

export type BillFileResult = FileResult;

const fileName = (invoiceNumber: string) => `Bill ${invoiceNumber.replace(/[^\w-]/g, "")}.pdf`;

async function openBillPage(invoiceId: string): Promise<BillFileResult> {
  const { url } = await apiGet(`/invoices/${invoiceId}/share`);
  window.open(url, "_blank", "noopener");
  return "opened";
}

// A PDF of the bill in the app's cache, named "Bill INV-0001.pdf".
async function makePdf(invoiceId: string, invoiceNumber: string): Promise<File> {
  const html = await fetchText(`/invoices/${invoiceId}/html`);
  const { uri } = await Print.printToFileAsync({ html, width: 420, height: 595 }); // A5
  const made = new File(uri);
  const named = new File(Paths.cache, fileName(invoiceNumber));
  try {
    if (named.exists) named.delete();
    await made.copy(named);
    return named;
  } catch {
    return made;
  }
}

export async function shareBillPdf(invoiceId: string, invoiceNumber: string): Promise<BillFileResult> {
  if (Platform.OS === "web") return openBillPage(invoiceId);
  const pdf = await makePdf(invoiceId, invoiceNumber);
  await Sharing.shareAsync(pdf.uri, { mimeType: PDF, UTI: "com.adobe.pdf", dialogTitle: invoiceNumber });
  return "shared";
}

export async function downloadBillPdf(invoiceId: string, invoiceNumber: string): Promise<BillFileResult> {
  if (Platform.OS === "web") return openBillPage(invoiceId);
  const pdf = await makePdf(invoiceId, invoiceNumber);
  return saveToPhone(pdf, fileName(invoiceNumber), PDF, "com.adobe.pdf");
}
