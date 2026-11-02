// The order: change how many, the price for this customer, remove, discount, then take payment.
// A changed price shows the shop's price struck through; once billed nothing can be changed.
import { useState } from "react";
import { Pressable, TextInput, View } from "react-native";
import { useRouter } from "expo-router";
import { KeyboardAwareScrollView } from "react-native-keyboard-controller";
import { useTranslation } from "react-i18next";

import { useAuth } from "@/src/auth";
import { CartItem, isLoose, useCart } from "@/src/cart-context";
import { DiscardOrder, OrderCustomerBar } from "@/src/components/order-customer";
import { BottomBar, Button, EmptyState, Field, Header, Icon, MAX_FONT_SCALE, Screen, Stepper, T, Thumb, Toggle } from "@/src/components/ui";
import { formatCurrency, plural } from "@/src/format";
import { colors, fonts, radius, spacing } from "@/src/theme";
import { unitShort, wholeUnit } from "@/src/units";

export default function OrderCart() {
  const { t } = useTranslation();
  const router = useRouter();
  const cart = useCart();
  const { isOwner } = useAuth();
  const back = () => (router.canGoBack() ? router.back() : router.replace("/order/scan"));

  if (!cart.items.length) {
    return (
      <Screen bg={colors.surface}>
        <Header title={t("Order")} onBack={back} right={<DiscardOrder />} />
        <EmptyState icon="cart-outline" title={t("Nothing in this order yet")}
          action={<Button title={t("Add items")} icon="camera" onPress={() => router.replace("/order/scan")} />} />
      </Screen>
    );
  }

  return (
    <Screen bg={colors.surface}>
      <Header title={`${t("Order")} · ${plural(cart.count, "1 item", "{{n}} items")}`} onBack={back} right={<DiscardOrder />} />
      <KeyboardAwareScrollView contentContainerStyle={{ padding: spacing.lg, gap: spacing.md }} bottomOffset={24}>
        <OrderCustomerBar />
        {cart.items.map((it) => <Line key={it.product_id} it={it} />)}

        <Button title={t("Add more items")} icon="plus" variant="dashed" size="md" onPress={back} testID="cart-add-more" />

        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md, marginTop: spacing.sm }}>
          <T v="bodyLarge" style={{ flex: 1 }}>{t("Discount ₹")}</T>
          <Field style={{ width: 140 }} value={cart.discount ? String(cart.discount) : ""} placeholder="0" keyboardType="decimal-pad"
            accessibilityLabel={t("Discount")} testID="cart-discount" inputStyle={{ textAlign: "right" }}
            onChangeText={(v) => cart.setDiscount(Number(v.replace(/[^0-9.]/g, "")) || 0)} />
        </View>
        {isOwner && cart.gstAvailable > 0.004 && (
          <View style={{ paddingHorizontal: spacing.lg, paddingVertical: spacing.xs, borderRadius: radius.lg, backgroundColor: colors.page }}>
            <Toggle title={t("Bill without GST")} subtitle={t("The total stays the same. No GST is shown on this bill.")}
              value={cart.gstRemoved} onChange={cart.setGstRemoved} testID="cart-no-gst" />
          </View>
        )}
      </KeyboardAwareScrollView>

      <BottomBar>
        <View style={{ flexDirection: "row", alignItems: "flex-end", justifyContent: "space-between" }}>
          <View>
            <T v="label">{t("Total")}</T>
            {cart.gstIncluded > 0.004 && <T v="small">{t("Includes GST {{gst}}", { gst: formatCurrency(cart.gstIncluded) })}</T>}
            {cart.gstRemoved && <T v="small">{t("No GST on this bill")}</T>}
          </View>
          <T v="amount" testID="cart-total">{formatCurrency(cart.total)}</T>
        </View>
        <Button title={t("Take payment")} icon="cash-register" onPress={() => router.push("/order/pay")} testID="cart-pay" />
      </BottomBar>
    </Screen>
  );
}

function Line({ it }: { it: CartItem }) {
  const { t } = useTranslation();
  const cart = useCart();
  const loose = isLoose(it.product_id);
  const shop = it.list_price ?? it.price;
  const changed = !loose && Math.abs(it.price - shop) > 0.004;
  const whole = wholeUnit(it.unit);
  return (
    <View testID={`cart-line-${it.name}`} style={{ gap: spacing.md, padding: spacing.md, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
        <Thumb path={it.image_path} size={56} icon={loose ? "tag-outline" : "sprout"} />
        <View style={{ flex: 1, minWidth: 0 }}>
          <T v="bold">{it.name}</T>
          {loose && <T v="small">{t("not from stock")}</T>}
        </View>
        <T v="number">{formatCurrency(it.price * it.quantity)}</T>
      </View>
      <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
        <View style={{ flex: 1 }}>
          <T v="label">{whole ? t("Price each") : t("Price per {{unit}}", { unit: unitShort(it.unit) })}</T>
          {changed && (
            <T v="small" testID={`cart-shop-price-${it.name}`}>
              {t("Shop price")} <T v="small" style={{ textDecorationLine: "line-through" }}>{formatCurrency(shop)}</T>
            </T>
          )}
        </View>
        <PriceBox price={it.price} onChange={(v) => cart.setPrice(it.product_id, v)} name={it.name} changed={changed} />
      </View>
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
        <Pressable accessibilityLabel={t("Remove")} onPress={() => cart.removeItem(it.product_id)} testID={`cart-remove-${it.name}`}
          style={{ flexDirection: "row", alignItems: "center", gap: 6, minHeight: 48, paddingRight: spacing.md }}>
          <Icon name="trash-can-outline" size={24} color={colors.red} />
          <T v="bold" color={colors.red}>{t("Remove")}</T>
        </Pressable>
        <Stepper value={it.quantity} min={0} max={!loose && it.stock != null ? it.stock : 9999} decimals={!whole} unit={unitShort(it.unit)}
          onChange={(q) => cart.setQuantity(it.product_id, q)} testID={`cart-qty-${it.name}`} />
      </View>
    </View>
  );
}

// "₹ [600]": the price for this order only; the product keeps its own price.
function PriceBox({ price, onChange, name, changed }: { price: number; onChange: (v: number) => void; name: string; changed: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState(String(price));
  return (
    <View style={{ flexDirection: "row", alignItems: "center", width: 140, minHeight: 52, borderWidth: 2, borderRadius: radius.md,
      borderColor: changed ? colors.amber : colors.lineStrong, backgroundColor: colors.surface, paddingLeft: spacing.md }}>
      <T v="bold" style={{ fontSize: 20 }}>₹</T>
      <TextInput value={text} keyboardType="decimal-pad" selectTextOnFocus maxFontSizeMultiplier={MAX_FONT_SCALE}
        accessibilityLabel={t("Price each")} testID={`cart-price-${name}`}
        onChangeText={(v) => {
          const clean = v.replace(/[^0-9.]/g, "");
          setText(clean);
          if (clean !== "" && Number.isFinite(Number(clean))) onChange(Number(clean));
        }}
        onBlur={() => setText(String(price))}
        style={{ flex: 1, minWidth: 0, width: 0, minHeight: 48, paddingHorizontal: spacing.sm, paddingRight: spacing.md, textAlign: "right", fontFamily: fonts.semibold, fontSize: 20, color: colors.ink }} />
    </View>
  );
}
