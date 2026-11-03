// App admin: the shared product pictures every shop sees. A shop's first clear photo fills an empty
// one and shows as "To check" until the admin looks at it here.
import { useMemo, useState } from "react";
import { FlatList, Pressable, ScrollView, View } from "react-native";
import { Image } from "expo-image";
import { Redirect } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiDelete, apiGet, apiPatch, apiPost, fileUrl } from "@/src/api";
import { useAuth } from "@/src/auth";
import { Sheet } from "@/src/components/sheet";
import { useToast } from "@/src/components/toast";
import { Badge, Button, Chip, EmptyState, ErrorView, Field, Header, Icon, LoadingView, Screen, SearchBox, T, thumbUrl } from "@/src/components/ui";
import { pickFromGallery, takePhoto } from "@/src/hooks/useImageCapture";
import { LONG_EDGE } from "@/src/image-tools";
import { colors, radius, spacing } from "@/src/theme";
import type { LibraryEntry } from "@/src/types";

type Filter = "all" | "new" | "none" | "live";

function StatusBadge({ e }: { e: LibraryEntry }) {
  const { t } = useTranslation();
  return e.image_status === "new" ? <Badge text={t("To check")} tone="amber" />
    : e.image_status === "live" ? <Badge text={t("Checked")} tone="green" /> : <Badge text={t("No picture")} tone="grey" />;
}

// In the grid (half the screen) the small copy of the photo; in the sheet the photo itself, to check it properly.
function Picture({ path, size }: { path: string | null; size: number | "100%" }) {
  const uri = size === "100%" ? thumbUrl(path) : fileUrl(path);
  return (
    <View style={{ width: size, aspectRatio: 1, borderRadius: radius.md, overflow: "hidden", backgroundColor: colors.greenTint,
      alignItems: "center", justifyContent: "center" }}>
      {uri ? <Image source={{ uri }} recyclingKey={uri} cachePolicy="memory-disk" style={{ width: "100%", height: "100%" }} contentFit="cover" />
        : <Icon name="sprout" size={48} color={colors.greenMid} />}
    </View>
  );
}

export default function AdminLibrary() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const q = useQuery<LibraryEntry[]>({ queryKey: ["admin-library"], queryFn: () => apiGet("/admin/library?status=all"), enabled: !!user?.is_admin });

  const all = useMemo(() => q.data ?? [], [q.data]);
  const count = (f: Filter) => (f === "all" ? all.length : all.filter((e) => (e.image_status ?? "none") === f).length);
  const shown = useMemo(() => {
    const s = search.trim().toLowerCase();
    return all.filter((e) => (filter === "all" || (e.image_status ?? "none") === filter)
      && (!s || e.name.toLowerCase().includes(s) || (e.name_hi ?? "").includes(search.trim())));
  }, [all, filter, search]);
  const open = all.find((e) => e.id === openId) ?? null;

  if (!user?.is_admin) return <Redirect href="/(tabs)/more" />;

  const FILTERS: { f: Filter; label: string }[] = [
    { f: "all", label: t("All") }, { f: "new", label: t("To check") }, { f: "none", label: t("No picture") }, { f: "live", label: t("Checked") },
  ];

  return (
    <Screen bg={colors.surface}>
      <Header title={t("Shared pictures")} right={<View style={{ marginRight: 12 }}><Badge text={t("Admin")} tone="amber" /></View>} />
      <View style={{ paddingHorizontal: spacing.lg, gap: spacing.md, paddingBottom: spacing.md }}>
        <T v="small">{t("Every shop sees these pictures. A product without one gets the first clear photo a shop takes; check those under \"To check\".")}</T>
        <SearchBox value={search} onChangeText={setSearch} placeholder={t("Search plants")} testID="lib-search" />
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: spacing.sm }}>
          {FILTERS.map(({ f, label }) => (
            <Chip key={f} label={`${label} · ${count(f)}`} active={filter === f} tone={f === "new" ? "amber" : "green"}
              onPress={() => setFilter(f)} testID={`lib-filter-${f}`} />
          ))}
        </ScrollView>
      </View>
      {q.isLoading ? <LoadingView /> : q.isError ? <ErrorView message={(q.error as Error).message} onRetry={q.refetch} /> : (
        <FlatList
          data={shown}
          keyExtractor={(e) => e.id}
          numColumns={2}
          columnWrapperStyle={{ gap: spacing.md }}
          contentContainerStyle={{ paddingHorizontal: spacing.lg, paddingBottom: spacing.xl, gap: spacing.md }}
          ListEmptyComponent={<EmptyState icon="image-multiple" title={t("Nothing here")} />}
          renderItem={({ item: e }) => (
            <Pressable onPress={() => setOpenId(e.id)} testID={`lib-${e.name}`}
              style={({ pressed }) => ({ flex: 1, maxWidth: "50%", gap: 6, padding: spacing.sm, borderRadius: radius.lg, borderWidth: 1,
                borderColor: e.image_status === "new" ? colors.amber : colors.line, backgroundColor: pressed ? colors.page : colors.surface })}>
              <Picture path={e.image_path} size="100%" />
              <View style={{ gap: 2, paddingHorizontal: 2 }}>
                <T v="bold">{e.name}</T>
                {!!e.name_hi && <T v="small">{e.name_hi}</T>}
                <StatusBadge e={e} />
              </View>
            </Pressable>
          )}
        />
      )}
      {open && <EntrySheet entry={open} onClose={() => setOpenId(null)} />}
    </Screen>
  );
}

function EntrySheet({ entry: e, onClose }: { entry: LibraryEntry; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [sure, setSure] = useState<"remove" | "delete" | null>(null);
  const [name, setName] = useState(e.name);
  const [nameHi, setNameHi] = useState(e.name_hi ?? "");
  const [category, setCategory] = useState(e.default_category ?? "");

  async function act(kind: string, fn: () => Promise<unknown>, done?: string, close = false) {
    setBusy(kind);
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["admin-library"] });
      qc.invalidateQueries({ queryKey: ["products"] });
      if (done) toast.show(done, "success");
      setSure(null);
      if (close) onClose();
    } catch (err: any) {
      toast.show(err.message, "error");
    } finally {
      setBusy(null);
    }
  }

  async function photo(source: "camera" | "gallery") {
    const out = source === "camera" ? await takePhoto({ aspect: 1, longEdge: LONG_EDGE.plant }) : await pickFromGallery({ aspect: 1, longEdge: LONG_EDGE.plant });
    if (out.status !== "ok") return;
    act(source, () => apiPost(`/admin/library/${e.id}/photo`, { image_base64: out.result.base64 }), t("Picture updated"));
  }

  const changed = name.trim() !== e.name || nameHi.trim() !== (e.name_hi ?? "") || category.trim() !== (e.default_category ?? "");

  return (
    <Sheet visible onClose={onClose} title={e.name} testID="lib-sheet">
      <View style={{ alignItems: "center", gap: spacing.sm }}>
        <Picture path={e.image_path} size={220} />
        <StatusBadge e={e} />
      </View>
      <View style={{ flexDirection: "row", gap: spacing.md }}>
        <Button title={t("Camera")} icon="camera" variant="outline" size="md" style={{ flex: 1 }} onPress={() => photo("camera")} loading={busy === "camera"} />
        <Button title={t("Gallery")} icon="image-outline" variant="outline" size="md" style={{ flex: 1 }} onPress={() => photo("gallery")} loading={busy === "gallery"} />
      </View>
      {e.image_status === "new" && (
        <Button title={t("Looks good")} icon="check-decagram" onPress={() => act("approve", () => apiPost(`/admin/library/${e.id}/approve`), t("Marked as checked"))}
          loading={busy === "approve"} testID="lib-approve" />
      )}
      {!!e.image_path && (sure === "remove" ? (
        <View style={{ gap: spacing.sm, padding: spacing.md, borderRadius: radius.md, backgroundColor: "#FDECEC" }}>
          <T v="bold" color={colors.red}>{t("Remove this picture for every shop?")}</T>
          <T v="small">{t("Shops that use their own photo keep it. The next suitable shop photo may replace it.")}</T>
          <View style={{ flexDirection: "row", gap: spacing.md }}>
            <Button title={t("Cancel")} variant="outline" size="md" style={{ flex: 1 }} onPress={() => setSure(null)} />
            <Button title={t("Remove")} icon="image-remove" variant="danger" size="md" style={{ flex: 1 }} testID="lib-remove-yes"
              onPress={() => act("remove", () => apiDelete(`/admin/library/${e.id}/photo`), t("Picture removed"))} loading={busy === "remove"} />
          </View>
        </View>
      ) : (
        <Button title={t("Remove picture")} icon="image-remove" variant="danger" size="md" onPress={() => setSure("remove")} testID="lib-remove" />
      ))}

      <View style={{ gap: spacing.md, marginTop: 6, paddingTop: spacing.lg, borderTopWidth: 1, borderTopColor: colors.line }}>
        <T v="label" style={{ fontSize: 18 }}>{t("Details")}</T>
        <Field label={t("Name")} value={name} onChangeText={setName} autoCapitalize="words" />
        <Field label={t("Hindi name")} value={nameHi} onChangeText={setNameHi} placeholder="मनी प्लांट" />
        <Field label={t("Usual category")} value={category} onChangeText={setCategory} autoCapitalize="words" placeholder={t("e.g. Indoor Plants")} />
        <Button title={t("Save details")} icon="content-save" variant="secondary" size="md" disabled={!changed || !name.trim()} loading={busy === "save"}
          onPress={() => act("save", () => apiPatch(`/admin/library/${e.id}`, { name: name.trim(), name_hi: nameHi.trim(), default_category: category.trim() }), t("Saved"))} />
      </View>

      <View style={{ marginTop: 6, paddingTop: spacing.lg, borderTopWidth: 1, borderTopColor: colors.line, gap: spacing.sm }}>
        {sure === "delete" ? (
          <View style={{ gap: spacing.sm, padding: spacing.md, borderRadius: radius.md, backgroundColor: "#FDECEC" }}>
            <T v="bold" color={colors.red}>{t("Delete \"{{name}}\" from the library?", { name: e.name })}</T>
            <T v="small">{t("It will no longer appear in suggestions. Shops keep their own products.")}</T>
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <Button title={t("Cancel")} variant="outline" size="md" style={{ flex: 1 }} onPress={() => setSure(null)} />
              <Button title={t("Delete")} icon="trash-can-outline" variant="danger" size="md" style={{ flex: 1 }} testID="lib-delete-yes"
                onPress={() => act("delete", () => apiDelete(`/admin/library/${e.id}`), t("Deleted"), true)} loading={busy === "delete"} />
            </View>
          </View>
        ) : (
          <Button title={t("Delete from library")} icon="trash-can-outline" variant="danger" size="md" onPress={() => setSure("delete")} testID="lib-delete" />
        )}
      </View>
    </Sheet>
  );
}
