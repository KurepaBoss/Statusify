// The shareable monthly Wrapped card, drawn on a canvas (port of
// render_wrapped in statusify_ui_stats.py). 1080 x 1350, the accent colour
// drives soft blurred colour fields, darkened so white type always reads.
import { ellipsize, fmtHm, hexToRgb, hlsToRgb, plural, rgbToHls, wrapText } from "./stats_util";

export type Summary = {
  year: number;
  month: number;
  label: string;
  days_in_month: number;
  plays: number;
  listened_ms: number;
  artists: number;
  top_song: { title: string; artist: string; album_art: string; plays: number } | null;
  top_artist: [string, number] | null;
  busiest_hour: [number, number] | null;
  longest_streak: number;
  active_days: number;
};

export const WRAPPED_W = 1080;
export const WRAPPED_H = 1350;
const FONT = '"Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif';

type Weight = "regular" | "semibold" | "bold";
const WEIGHTS: Record<Weight, number> = { regular: 400, semibold: 600, bold: 700 };

function canvas(w: number, h: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  return [c, c.getContext("2d")!];
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

/** The Wrapped image for one month; `art` is the top song's cover or null. */
export function renderWrapped(s: Summary, accent: string, art: CanvasImageSource | null): HTMLCanvasElement {
  const W = WRAPPED_W, H = WRAPPED_H;
  const [out, ctx] = canvas(W, H);
  const [ar, ag, ab] = hexToRgb(accent);
  const [hh, , sat] = rgbToHls(ar, ag, ab);
  const ss = Math.max(sat, 0.45);
  const hls = (dh: number, l: number, sv = ss) => {
    const [r, g, b] = hlsToRgb(((hh + dh) % 1 + 1) % 1, l, sv);
    return `rgb(${r},${g},${b})`;
  };

  // Colour fields, blurred (padded so the blur has colour to pull from at the edges).
  const PAD = 320;
  const [fields, fx] = canvas(W + 2 * PAD, H + 2 * PAD);
  fx.fillStyle = hls(0, 0.07, ss * 0.7);
  fx.fillRect(0, 0, fields.width, fields.height);
  const blobs: [number, number, number, string][] = [
    [0.15, 0.1, 0.55, hls(0.0, 0.33)],
    [0.95, 0.3, 0.5, hls(0.09, 0.26)],
    [0.2, 0.75, 0.6, hls(-0.08, 0.2)],
    [0.85, 0.95, 0.45, hls(0.04, 0.28)],
  ];
  for (const [cx, cy, r, col] of blobs) {
    fx.fillStyle = col;
    fx.beginPath();
    fx.arc(PAD + cx * W, PAD + cy * H * 1.0, r * W, 0, Math.PI * 2);
    fx.fill();
  }
  const [blurred, bx] = canvas(fields.width, fields.height);
  bx.filter = `blur(${Math.round(W * 0.16)}px)`;
  bx.drawImage(fields, 0, 0);
  ctx.drawImage(blurred, -PAD, -PAD);
  // Darken toward the bottom, where the numbers sit.
  const shade = ctx.createLinearGradient(0, 0, 0, H);
  shade.addColorStop(0, "rgba(8,8,12,0)");
  shade.addColorStop(1, "rgba(8,8,12,0.55)");
  ctx.fillStyle = shade;
  ctx.fillRect(0, 0, W, H);

  const white = "rgba(255,255,255,1)";
  const soft = "rgba(255,255,255,0.7)";
  const faint = "rgba(255,255,255,0.47)";
  ctx.textBaseline = "top";
  const font = (w: Weight, px: number) => `${WEIGHTS[w]} ${px}px ${FONT}`;
  const measure = (w: Weight, px: number) => (t: string) => {
    ctx.font = font(w, px);
    return ctx.measureText(t).width;
  };
  const lineH = (px: number) => Math.round(px * 1.25);
  const text = (t: string, x: number, y: number, w: Weight, px: number, fill: string, align: CanvasTextAlign = "left") => {
    ctx.font = font(w, px);
    ctx.fillStyle = fill;
    ctx.textAlign = align;
    ctx.fillText(t, x, y);
    ctx.textAlign = "left";
  };

  const M = 84;
  let y = 84;
  text("STATUSIFY  ·  WRAPPED", M, y, "semibold", 28, soft);
  y += 50;
  text(s.label || `${s.month}/${s.year}`, M, y, "bold", 88, white);
  y += lineH(88) + 36;

  // Top song, with its cover.
  const artPx = 300;
  if (art) {
    ctx.save();
    ctx.shadowColor = "rgba(0,0,0,0.6)";
    ctx.shadowBlur = 36;
    ctx.shadowOffsetX = 6;
    ctx.shadowOffsetY = 14;
    ctx.fillStyle = "#000";
    roundRect(ctx, M, y, artPx, artPx, 28);
    ctx.fill();
    ctx.restore();
    ctx.save();
    roundRect(ctx, M, y, artPx, artPx, 28);
    ctx.clip();
    ctx.drawImage(art, M, y, artPx, artPx);
    ctx.restore();
  } else {
    ctx.fillStyle = hls(0.0, 0.5);
    roundRect(ctx, M, y, artPx, artPx, 28);
    ctx.fill();
    ctx.font = font("bold", 140);
    ctx.fillStyle = "rgba(255,255,255,0.86)";
    ctx.textAlign = "center";
    ctx.fillText("♫", M + artPx / 2, y + (artPx - lineH(140)) / 2 + 12);
    ctx.textAlign = "left";
  }
  const tx = M + artPx + 48;
  const tw = W - M - tx;
  let ty = y + 18;
  text("TOP SONG", tx, ty, "semibold", 26, faint);
  ty += 48;
  const song = s.top_song;
  const title = song ? song.title : "—";
  const all = wrapText(title, tw, measure("bold", 54));
  const lines = all.slice(0, 3);
  if (all.length > 3) lines[2] = ellipsize(lines[2] + "…", tw, measure("bold", 54));
  for (const ln of lines) {
    text(ln, tx, ty, "bold", 54, white);
    ty += lineH(54) - 4;
  }
  if (song) {
    ty += 8;
    text(ellipsize(song.artist, tw, measure("regular", 34)), tx, ty, "regular", 34, soft);
    ty += 48;
    text(plural(song.plays, "play"), tx, ty, "semibold", 28, faint);
  }
  y += artPx + 64;

  // 2 x 3 grid of numbers.
  const hour = s.busiest_hour;
  const cells: [string, string, string][] = [
    ["TOP ARTIST", s.top_artist ? s.top_artist[0] : "—", s.top_artist ? `${s.top_artist[1]} plays` : ""],
    ["LISTENING TIME", fmtHm(s.listened_ms), ""],
    ["PLAYS", s.plays.toLocaleString("en-US"), plural(s.artists, "artist")],
    ["BUSIEST HOUR", hour ? `${String(hour[0]).padStart(2, "0")}:00` : "—", hour ? `${hour[1]} plays` : ""],
    ["LONGEST STREAK", plural(s.longest_streak, "day"), "in a row"],
    ["ACTIVE DAYS", String(s.active_days), `of ${s.days_in_month}`],
  ];
  const colW = Math.floor((W - 2 * M) / 2);
  const rowH = 192;
  cells.forEach(([label, value, sub], i) => {
    const cx = M + (i % 2) * colW;
    const cy = y + Math.floor(i / 2) * rowH;
    text(label, cx, cy, "semibold", 26, faint);
    text(ellipsize(value, colW - 30, measure("bold", 62)), cx, cy + 38, "bold", 62, white);
    if (sub) text(sub, cx, cy + 118, "regular", 28, soft);
  });
  text("Made with Statusify", W - M, H - 84, "semibold", 26, faint, "right");
  ctx.fillStyle = hls(0.0, 0.6);
  roundRect(ctx, M, H - 76, 96, 8, 4);
  ctx.fill();
  return out;
}

/** The canvas as PNG bytes, base64 (no data: prefix). */
export async function canvasToPngBase64(c: HTMLCanvasElement): Promise<string> {
  const blob = await new Promise<Blob | null>((res) => c.toBlob(res, "image/png"));
  if (!blob) throw new Error("render failed");
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export async function canvasToBlob(c: HTMLCanvasElement): Promise<Blob> {
  const blob = await new Promise<Blob | null>((res) => c.toBlob(res, "image/png"));
  if (!blob) throw new Error("render failed");
  return blob;
}
