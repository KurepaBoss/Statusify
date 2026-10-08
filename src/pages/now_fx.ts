// The window's moving backdrop (statusify_backdrop + statusify_fluid): the
// playing song's cover, enlarged, heavily blurred and slowly drifting, with
// the water (five soft blobs of the cover's colours on slow Lissajous paths)
// flowing faintly over it. Every page shows it; the Lyrics page brighter.
//
// Brightness follows Python's cover mode (album tint on, dark theme, a cover
// loaded): only then are the picture darkened (vignette, #shade). Without it the
// Lyrics page shows the water as it is, melting into the window colour at its
// edges, and the other pages are flat. All of it is CSS keyed off #backdrop.cover
// and body[data-page] (styles.css).
//
// Everything that moves is a CSS animation of transform on a composited
// layer, and the cover is a 96 px canvas texture the GPU scales up, so a
// frame costs the compositor nothing but the blend (the Python version
// rebuilt a PIL image every frame). Each blob is a 128 px canvas texture too
// (a radial gradient is linear in the radius, so the GPU's bilinear upscale
// reproduces it; a CSS gradient the size of the screen was a 2000 px texture
// per blob, rasterised again on every frame of its 1.6 s colour fade at a
// track change). The fade is drawn here, with CSS's ease-in-out, into that
// small texture. Brightness per page is one opacity on #shade (index.html),
// set from body[data-page] in styles.css.

export type BdColors = { base: string; blobs: string[] };

const NEUTRAL_DARK: BdColors = { base: "#0b0d10", blobs: ["#1d1d25", "#18181f", "#1b1f1d", "#1d1d25", "#15151b"] };
const NEUTRAL_LIGHT: BdColors = { base: "#f4f6f8", blobs: ["#dfe2e9", "#e4e7ed", "#dde5e0", "#dfe2e9", "#e7e9ef"] };

/** Quiet water of the theme's own surfaces with a trace of the accent. */
export function neutralColors(dark: boolean, accent?: string): BdColors {
  const c = dark ? NEUTRAL_DARK : NEUTRAL_LIGHT;
  if (!accent) return c;
  const blobs = [...c.blobs];
  blobs[2] = mixHex(c.blobs[2], accent, 0.14);
  return { base: c.base, blobs };
}

type Rgb = [number, number, number];

/** #rgb or #rrggbb to channels; anything else is black. */
export function hexRgb(h: string): Rgb {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(h.trim());
  if (!m) return [0, 0, 0];
  const x = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  return [0, 2, 4].map((i) => parseInt(x.slice(i, i + 2), 16)) as Rgb;
}

function mixHex(a: string, b: string, t: number): string {
  const [x, y] = [hexRgb(a), hexRgb(b)];
  return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, "0")).join("");
}

/** How long a blob takes to change colour, and its curve: CSS `1.6s ease-in-out`. */
export const BLOB_FADE_MS = 1600;
const EASE_IN_OUT = cubicBezier(0.42, 0, 0.58, 1);

/** y(x) of a CSS cubic-bezier timing function. */
export function cubicBezier(x1: number, y1: number, x2: number, y2: number): (x: number) => number {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const sx = (t: number) => ((ax * t + bx) * t + cx) * t;
  const sy = (t: number) => ((ay * t + by) * t + cy) * t;
  const dx = (t: number) => (3 * ax * t + 2 * bx) * t + cx;
  return (x: number) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i++) {
      const d = dx(t);
      if (Math.abs(d) < 1e-6) break;
      const e = sx(t) - x;
      if (Math.abs(e) < 1e-6) return sy(t);
      t -= e / d;
    }
    let lo = 0, hi = 1;
    t = x;
    while (hi - lo > 1e-6) {
      if (sx(t) < x) lo = t; else hi = t;
      t = (lo + hi) / 2;
    }
    return sy(t);
  };
}

/** The texture of one blob: a disc fading linearly from `rgb` at the centre
 *  to nothing at the edge, as CSS `radial-gradient(closest-side, c, transparent)`
 *  paints it (the alpha fades, the colour stays: premultiplied interpolation). */
export const DISC_PX = 128;
export function paintDisc(c: HTMLCanvasElement, rgb: Rgb) {
  const ctx = c.getContext("2d")!;
  const r = DISC_PX / 2;
  const g = ctx.createRadialGradient(r, r, 0, r, r, r);
  g.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1)`);
  g.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
  ctx.clearRect(0, 0, DISC_PX, DISC_PX);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, DISC_PX, DISC_PX);
}

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class Backdrop {
  private layers: HTMLElement[] = [];
  private canvases: HTMLCanvasElement[] = [];
  private cur = -1;
  private url: string | null = null;
  private gen = 0;
  private water: HTMLElement;
  private blobs: HTMLElement[] = [];
  private discs: HTMLCanvasElement[] = [];
  /** Each blob's colour: what is shown now, and the fade it is in (from, to, started at). */
  private tint: { now: Rgb; from: Rgb; to: Rgb; at: number }[] = [];
  private fadeRaf = 0;
  private beat: HTMLElement;
  private lastBeat = 0;

  constructor(private root: HTMLElement) {
    root.replaceChildren();
    root.classList.add("bd");
    const mk = (cls: string, parent: HTMLElement = root) => {
      const e = document.createElement("div");
      e.className = cls;
      parent.append(e);
      return e;
    };
    mk("bd-base");
    for (let i = 0; i < 2; i++) {
      const l = mk("bd-cov");
      const c = document.createElement("canvas");
      c.width = c.height = 96;
      c.className = "bd-img";
      l.append(c);
      this.layers.push(l);
      this.canvases.push(c);
    }
    this.water = mk("bd-water");
    this.buildBlobs();
    mk("bd-vig");
    mk("bd-edge");
    mk("bd-flat");
    this.beat = mk("bd-beat");
  }

  private buildBlobs() {
    const r = rng(7);
    const u = (lo: number, hi: number) => lo + (hi - lo) * r();
    for (let n = 0; n < 5; n++) {
      const fx = u(0.07, 0.16), fy = u(0.06, 0.14), gx = u(0.17, 0.29), gy = u(0.15, 0.27);
      const px = u(0, 6.3), py = u(0, 6.3), qx = u(0, 6.3), qy = u(0, 6.3);
      const rad = u(0.34, 0.5), fr = u(0.1, 0.22), pr = u(0, 6.3);
      // sin(w t + phi) as an alternating ease-in-out of half the period, starting at -amp.
      const anim = (el: HTMLElement, name: string, w: number, phi: number) => {
        const T = (2 * Math.PI) / w;
        el.style.animation = `${name} ${(T / 2).toFixed(2)}s ease-in-out infinite alternate`;
        el.style.animationDelay = `${(-(((phi + Math.PI / 2) / w) % T)).toFixed(2)}s`;
      };
      let parent = this.water;
      const wrap = (cls: string, name: string, w: number, phi: number, amp?: string) => {
        const e = document.createElement("div");
        e.className = cls;
        if (amp) e.style.setProperty("--amp", amp);
        anim(e, name, w, phi);
        parent.append(e);
        parent = e;
        return e;
      };
      wrap("bl", "bd-x", fx, px, "34vw");
      wrap("bl", "bd-x", gx, qx, "14vw");
      wrap("bl", "bd-y", fy, py, "34vh");
      wrap("bl", "bd-y", gy, qy, "14vh");
      const sz = `${(rad * 200).toFixed(1)}vmax`;
      const disc = wrap("bd-blob", "bd-s", fr, pr);
      disc.style.width = disc.style.height = sz;
      disc.style.margin = `calc(${sz} / -2) 0 0 calc(${sz} / -2)`;
      const c = document.createElement("canvas");
      c.width = c.height = DISC_PX;
      c.className = "bd-disc";
      disc.append(c);
      const start = hexRgb(NEUTRAL_DARK.blobs[n]);
      paintDisc(c, start);
      this.blobs.push(disc);
      this.discs.push(c);
      this.tint.push({ now: start, from: start, to: start, at: 0 });
    }
  }

  /** One frame of the blobs' colour fades; stops itself when they are done. */
  private fadeFrame = (ts: number) => {
    this.fadeRaf = 0;
    let busy = false;
    this.tint.forEach((t, i) => {
      if (t.now === t.to) return;
      const k = EASE_IN_OUT(Math.min(1, (ts - t.at) / BLOB_FADE_MS));
      const rgb = (k >= 1 ? t.to : t.from.map((v, j) => v + (t.to[j] - v) * k)) as Rgb;
      t.now = k >= 1 ? t.to : rgb;
      paintDisc(this.discs[i], rgb);
      if (k < 1) busy = true;
    });
    if (busy) this.fadeRaf = requestAnimationFrame(this.fadeFrame);
  };

  /** Crossfade to a new cover; null shows the water alone. */
  setCover(url: string | null) {
    if (url === this.url) return;
    this.url = url;
    const gen = ++this.gen;
    if (!url) {
      this.layers.forEach((l) => l.classList.remove("on"));
      this.cur = -1;
      return;
    }
    const img = new Image();
    img.onload = () => {
      if (gen !== this.gen) return; // skipped on since
      const next = this.cur === 0 ? 1 : 0;
      this.paint(this.canvases[next], img);
      this.layers[next].classList.add("on");
      if (this.cur >= 0) this.layers[this.cur].classList.remove("on");
      this.cur = next;
    };
    img.onerror = () => {
      if (gen === this.gen) {
        this.layers.forEach((l) => l.classList.remove("on"));
        this.cur = -1;
      }
    };
    img.src = url;
  }

  /** The cover as a smooth texture: shrunk to 24 px (that shrink is the heavy
   *  blur), a little more saturated, scaled back up to 96 px. */
  private paint(c: HTMLCanvasElement, img: HTMLImageElement) {
    const tiny = document.createElement("canvas");
    tiny.width = tiny.height = 24;
    const t = tiny.getContext("2d")!;
    t.imageSmoothingQuality = "medium";
    t.drawImage(img, 0, 0, 24, 24);
    const ctx = c.getContext("2d")!;
    ctx.clearRect(0, 0, 96, 96);
    ctx.imageSmoothingQuality = "high";
    ctx.filter = "saturate(1.3) blur(6px)";
    ctx.drawImage(tiny, -10, -10, 116, 116); // a little oversize: the blur has no transparent rim
    ctx.filter = "none";
  }

  setColors(c: BdColors) {
    this.root.style.setProperty("--bd-base", c.base);
    const now = performance.now();
    let changed = false;
    this.tint.forEach((t, i) => {
      const to = hexRgb(c.blobs[i % c.blobs.length]);
      if (to.join() === t.to.join()) return;
      // Like a CSS transition: from wherever the colour is now.
      t.from = t.now;
      t.to = to;
      t.at = now;
      changed = true;
    });
    if (changed && !this.fadeRaf) this.fadeRaf = requestAnimationFrame(this.fadeFrame);
  }

  /** Cover mode shows the water at 30 % over the cover; otherwise alone. */
  setCoverMode(on: boolean) {
    this.root.classList.toggle("cover", on);
  }

  /** animations off, or the adaptive tier asking for a still background. */
  setMotion(animations: boolean, still: boolean) {
    this.root.classList.toggle("still", !animations || still);
  }

  /** A beat's swell: a flat lift of the whole frame, in levels of 255. */
  setBeat(lift: number) {
    if (lift === this.lastBeat) return;
    this.lastBeat = lift;
    this.beat.style.opacity = lift ? String(lift / 255) : "0";
  }
}

let shared: Backdrop | null = null;

/** The one backdrop of the window (created on first use). */
export function backdrop(): Backdrop | null {
  if (!shared) {
    const el = document.getElementById("backdrop");
    if (el) shared = new Backdrop(el);
  }
  return shared;
}

/** The surface/text tokens `extras.palette.tokens` carries (statusify_colors.tinted_palette). */
export type Surfaces = Partial<Record<"BG" | "BG2" | "BG3" | "BG4" | "TEXT" | "TEXT2" | "MUTED" | "BORDER", string>>;

const SURFACE_VARS: [keyof Surfaces, string][] = [
  ["BG", "--bg"], ["BG2", "--bg2"], ["BG3", "--bg3"], ["BG4", "--bg4"],
  ["TEXT", "--text"], ["TEXT2", "--text2"], ["MUTED", "--muted"], ["BORDER", "--border"],
];
let surfaceKey = "";

/** Tint the whole window from the cover; null goes back to the theme's own colours. */
export function applySurfaces(t: Surfaces | null | undefined, root: HTMLElement = document.documentElement) {
  const ok = !!t && SURFACE_VARS.every(([k]) => /^#[0-9a-f]{6}$/i.test(t[k] ?? ""));
  const key = ok ? SURFACE_VARS.map(([k]) => t![k]).join() : "";
  if (key === surfaceKey) return;
  surfaceKey = key;
  for (const [k, v] of SURFACE_VARS) {
    if (ok) root.style.setProperty(v, t![k]!);
    else root.style.removeProperty(v);
  }
}
