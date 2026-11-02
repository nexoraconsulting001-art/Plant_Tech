// Selling more than the stock count. The owner may (the stock goes below zero, after a warning); employees
// may not and are asked to get the stock updated. These sentences are shared by the order screens.
import { t } from "i18next";

import { formatQty, formatQtyUnit } from "@/src/format";
import { wholeUnit } from "@/src/units";

// "3" for pieces (the name says what), "2.5 kg" for weight, volume and length
export const stockQty = (n: number, unit?: string | null) => (wholeUnit(unit) ? formatQty(n) : formatQtyUnit(n, unit));

// For the owner: "Only 3 Rose in stock. Stock will go to -2."
export function shortWarning(name: string, stock: number, want: number, unit?: string | null): string {
  const after = stockQty(stock - want, unit);
  return stock > 0 ? t("Only {{n}} {{name}} in stock. Stock will go to {{after}}.", { n: stockQty(stock, unit), name, after })
    : t("{{name}} is out of stock. Stock will go to {{after}}.", { name, after });
}

// For an employee: "Only 3 Rose in stock. Ask the owner to update the stock."
export function askOwner(name: string, stock: number, unit?: string | null): string {
  return stock > 0 ? t("Only {{n}} {{name}} in stock. Ask the owner to update the stock.", { n: stockQty(stock, unit), name })
    : t("{{name}} is out of stock. Ask the owner to update the stock.", { name });
}
