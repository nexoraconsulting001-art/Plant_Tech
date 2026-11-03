// Photos are prepared on the phone: turned upright once (so the crop screen and the file agree on
// every pixel), cut to the frame the user chose, and shrunk before upload: the longest side at most 1024 px for a
// plant or product photo, 1568 px for a bill page (small print needs the pixels; a plant doesn't).
import { ImageManipulator, SaveFormat } from "expo-image-manipulator";

export const LONG_EDGE = { plant: 1024, bill: 1568 } as const;
export const MAX_EDGE = LONG_EDGE.bill; // the largest side Claude uses; bigger photos only cost upload time
const WORK_EDGE = 2000; // what the crop screen works on

export type Picked = { uri: string; width: number; height: number };
export type Rect = { x: number; y: number; width: number; height: number }; // in image pixels

export async function normalize(uri: string): Promise<Picked> {
  const first = await ImageManipulator.manipulate(uri).renderAsync();
  const long = Math.max(first.width, first.height);
  const ref = long > WORK_EDGE
    ? await ImageManipulator.manipulate(uri)
      .resize(first.width >= first.height ? { width: WORK_EDGE } : { height: WORK_EDGE })
      .renderAsync()
    : first;
  const saved = await ref.saveAsync({ format: SaveFormat.JPEG, compress: 0.92 });
  return { uri: saved.uri, width: saved.width, height: saved.height };
}

// longEdge: LONG_EDGE.plant or LONG_EDGE.bill (the default, as before, for callers that don't say).
export async function cropAndShrink(image: Picked, rect: Rect, longEdge: number = MAX_EDGE): Promise<{ uri: string; base64: string }> {
  const edge = Math.max(1, Math.min(Math.round(longEdge), MAX_EDGE));
  const x = Math.max(0, Math.min(Math.round(rect.x), image.width - 1));
  const y = Math.max(0, Math.min(Math.round(rect.y), image.height - 1));
  const width = Math.max(1, Math.min(Math.round(rect.width), image.width - x));
  const height = Math.max(1, Math.min(Math.round(rect.height), image.height - y));
  let ctx = ImageManipulator.manipulate(image.uri).crop({ originX: x, originY: y, width, height });
  if (Math.max(width, height) > edge) ctx = ctx.resize(width >= height ? { width: edge } : { height: edge });
  const out = await (await ctx.renderAsync()).saveAsync({ format: SaveFormat.JPEG, compress: 0.7, base64: true });
  return { uri: out.uri, base64: out.base64 ?? "" };
}
