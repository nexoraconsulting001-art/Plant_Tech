// The unit a product is sold in (the server's backend/units.py has the same list). Pieces and packs are
// counted in whole numbers; weight, volume and length can be 1.5. Plants are always pieces.
// Lines and products saved before units existed have none and count as pieces.
import { t } from "i18next";

export type Unit = "pcs" | "bag" | "packet" | "tray" | "kg" | "g" | "litre" | "ml" | "metre";
export const UNITS: Unit[] = ["pcs", "kg", "g", "litre", "ml", "metre", "bag", "packet", "tray"];
const DECIMAL = new Set<string>(["kg", "g", "litre", "ml", "metre"]);

export const MAX_QTY = 100000; // the server refuses more on one order line

export const wholeUnit = (u?: string | null) => !DECIMAL.has(u ?? "pcs");

// 1.4999 pieces -> 1; 0.12345 kg -> 0.123
export const roundQty = (n: number, whole: boolean) => (whole ? Math.round(n) : Math.round(n * 1000) / 1000);

// "40 pcs", "1.5 kg": the short form next to a number
export function unitShort(u?: string | null): string {
  switch (u) {
    case "kg": return t("kg");
    case "g": return t("g");
    case "litre": return t("L");
    case "ml": return t("ml");
    case "metre": return t("m");
    case "bag": return t("bag");
    case "packet": return t("pkt");
    case "tray": return t("tray");
    default: return t("pcs");
  }
}

// "Kilogram": the name in the unit chooser
export function unitName(u?: string | null): string {
  switch (u) {
    case "kg": return t("Kilogram");
    case "g": return t("Gram");
    case "litre": return t("Litre");
    case "ml": return t("Millilitre");
    case "metre": return t("Metre");
    case "bag": return t("Bag");
    case "packet": return t("Packet");
    case "tray": return t("Tray");
    default: return t("Pieces");
  }
}

const PLANT = /plant|succulent|cact|tree|shrub|herb|sapling|bonsai|पौध/;

// "Indoor Plants", "Succulents", "Cacti", "पौधे"… (not Seeds, Pots, Soil). Products in these are pieces.
export const isPlantCategory = (name?: string | null) => PLANT.test((name ?? "").toLowerCase());
