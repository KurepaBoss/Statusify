// "Share as image": a lyric line as a 1080 x 1350 picture. The song's colours
// blurred behind it, the line large, the next one quieter, the cover and
// title at the bottom and a small Statusify mark (render_share_image in
// statusify_np_extras.py), drawn on a canvas.

import { ellipsize, wrapText } from "./now_logic";

export type ShareOpts = {
  line: string;
  next: string;
  title: string;
  artist: string;
  /** the cover as a data: URI (a canvas fed a remote image would be tainted) */
  cover: string | null;
  /** blob colours of the cover (hex) and the base */
  palette: string[];
  base: string | null;
  accent: string;
  family: string;
};

export const SHARE_SIZE: [number, number] = [1080, 1350];

function loadImage(src: string): Promise<HTMLImageElement | null> {
  return new Promise((res) => {
    const i = new Image();
    i.onload = () => res(i);
    i.onerror = () => res(null);
    i.src = src;
  });
}

/** `cover` cropped to the aspect of w x h, drawn into (0,0,w,h). */
function coverFill(ctx: CanvasRenderingContext2D, img: HTMLImageElement, w: number, h: number) {
  const want = w / h;
  const cw = img.naturalWidth, ch = img.naturalHeight;
  let sx = 0, sy = 0, sw = cw, sh = ch;
  if (cw / ch > want) { sw = Math.trunc(ch * want); sx = (cw - sw) >> 1; }
  else { sh = Math.trunc(cw / want); sy = (ch - sh) >> 1; }
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, w, h);
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

export async function renderShareImage(o: ShareOpts): Promise<Blob> {
  const [W, H] = SHARE_SIZE;
  const cv = document.createElement("canvas");
  cv.width = W;
  cv.height = H;
  const ctx = cv.getContext("2d")!;
  const cover = o.cover ? await loadImage(o.cover) : null;

  // Background at 1/8 size, blurred, then scaled up: the same trick as the water.
  const sw = Math.max(8, W >> 3), sh = Math.max(8, H >> 3);
  const small = document.createElement("canvas");
  small.width = sw;
  small.height = sh;
  const sc = small.getContext("2d")!;
  sc.fillStyle = o.base || "#18181e";
  sc.fillRect(0, 0, sw, sh);
  const spots: [number, number, number][] = [[0.2, 0.25, 0.6], [0.8, 0.3, 0.55], [0.5, 0.75, 0.65], [0.15, 0.85, 0.5], [0.85, 0.8, 0.5]];
  o.palette.slice(0, 5).forEach((c, i) => {
    const [x, y, r] = spots[i % spots.length];
    const g = sc.createRadialGradient(x * sw, y * sh, 0, x * sw, y * sh, r * Math.max(sw, sh));
    g.addColorStop(0, c);
    g.addColorStop(1, c + "00");
    sc.fillStyle = g;
    sc.fillRect(0, 0, sw, sh);
  });
  if (cover) {
    sc.save();
    sc.filter = "blur(3px)";
    sc.globalAlpha = o.palette.length ? 0.45 : 1;
    coverFill(sc, cover, sw, sh);
    sc.restore();
  }
  ctx.imageSmoothingQuality = "high";
  ctx.filter = "blur(2px)";
  ctx.drawImage(small, -4, -4, W + 8, H + 8);
  ctx.filter = "none";
  // Darken for legibility, more towards the bottom where the title sits.
  const shade = ctx.createLinearGradient(0, 0, 0, H);
  for (let k = 0; k <= 20; k++) {
    const y = k / 20;
    const a = 0.4 + 0.35 * Math.max(0, (y - 0.45) / 0.55) ** 1.4;
    shade.addColorStop(y, `rgba(8,8,12,${a.toFixed(3)})`);
  }
  ctx.fillStyle = shade;
  ctx.fillRect(0, 0, W, H);

  const font = (w: number, px: number) => `${w} ${px}px ${o.family}`;
  const measure = (w: number, px: number) => (s: string) => { ctx.font = font(w, px); return ctx.measureText(s).width; };
  const margin = Math.trunc(W * 0.1);
  const maxw = W - 2 * margin;
  const line = o.line.trim() || "♪";
  let px = Math.trunc(W * 0.089);
  let rows: string[];
  for (;;) {
    rows = wrapText(line, maxw, measure(700, px));
    if (rows.length <= 5 || px <= Math.trunc(W * 0.044)) break;
    px -= 4;
  }
  rows = rows.slice(0, 6);
  const lh = Math.trunc(px * 1.22 * 1.04);
  const npx = Math.trunc(px * 0.52);
  const nrows = o.next.trim() ? wrapText(o.next.trim(), maxw, measure(600, npx)).slice(0, 3) : [];
  const nlh = Math.trunc(npx * 1.22 * 1.06);
  const block = rows.length * lh + (nrows.length ? Math.trunc(px * 0.6) + nrows.length * nlh : 0);
  const footH = Math.trunc(H * 0.2);
  const top = Math.max(Math.trunc(H * 0.1), (H - footH - block) >> 1);
  const bar = top - Math.trunc(px * 0.62);
  ctx.fillStyle = o.accent;
  roundRect(ctx, margin, bar, Math.trunc(W * 0.066), 8, 4);
  ctx.fill();
  ctx.textBaseline = "top";
  let y = top;
  ctx.fillStyle = "#fff";
  ctx.font = font(700, px);
  for (const r of rows) { ctx.fillText(r, margin, y); y += lh; }
  if (nrows.length) {
    y += Math.trunc(px * 0.6);
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.font = font(600, npx);
    for (const r of nrows) { ctx.fillText(r, margin, y); y += nlh; }
  }

  // Footer: cover, title, artist; the mark on the right.
  const A = Math.trunc(W * 0.111);
  const fy = H - margin - A;
  let tx = margin;
  if (cover) {
    ctx.save();
    roundRect(ctx, margin, fy, A, A, Math.trunc(A * 0.14));
    ctx.clip();
    ctx.drawImage(cover, margin, fy, A, A);
    ctx.restore();
    tx = margin + A + Math.trunc(W * 0.026);
  }
  const mpx = Math.trunc(W * 0.024);
  const mark = "Statusify";
  const mw = measure(600, mpx)(mark);
  const tpx = Math.trunc(W * 0.035), apx = Math.trunc(W * 0.03);
  const tmax = W - margin - tx - mw - Math.trunc(W * 0.04);
  const tS = ellipsize(o.title || "", tmax, measure(600, tpx));
  const aS = ellipsize(o.artist || "", tmax, measure(400, apx));
  const lt = Math.trunc(tpx * 1.22), la = Math.trunc(apx * 1.22);
  const ty = cover ? fy + ((A - lt - la) >> 1) : fy + A - lt - la;
  ctx.fillStyle = "rgba(255,255,255,0.96)";
  ctx.font = font(600, tpx);
  ctx.fillText(tS, tx, ty);
  ctx.fillStyle = "rgba(255,255,255,0.67)";
  ctx.font = font(400, apx);
  ctx.fillText(aS, tx, ty + lt);
  const ml = Math.trunc(mpx * 1.22);
  const mx = W - margin - mw;
  const my = ty + lt + la - ml;
  const dot = Math.trunc(mpx * 0.45);
  ctx.fillStyle = o.accent;
  ctx.globalAlpha = 0.78;
  ctx.beginPath();
  ctx.arc(mx - dot - Math.trunc(mpx * 0.45) + dot / 2, my + ml / 2, dot / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.fillStyle = "rgba(255,255,255,0.51)";
  ctx.font = font(600, mpx);
  ctx.fillText(mark, mx, my);

  return new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error("canvas export failed"))), "image/png"));
}

export function blobToBase64(b: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result));
    r.onerror = () => rej(r.error);
    r.readAsDataURL(b);
  });
}

export async function copyImage(b: Blob): Promise<boolean> {
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": b })]);
    return true;
  } catch {
    return false;
  }
}
