// The Lyrics page (owner: nowplaying agent): a lyric sheet over the song's
// cover. Ported from statusify_ui_now_playing.py / statusify_np_extras.py /
// statusify_np_fx.py. The Python page was one PIL canvas redrawn every frame;
// this one is DOM: the sheet glides on the compositor (now_sheet.ts), the
// backdrop is CSS-animated layers (now_fx.ts), and the per-frame JavaScript
// only sets the seek bar, the karaoke fill and the dots.

import "./now.css";
import { call, onSnapshot, position, seek, type Snapshot } from "../api";
import { applySurfaces, backdrop, neutralColors } from "./now_fx";
import { icon } from "./now_icons";
import * as L from "./now_logic";
import { startNav } from "./now_nav";
import { Panels, Toast, type MenuItem, type QueueItem } from "./now_panels";
import { Sheet, type RowSpec } from "./now_sheet";
import { blobToBase64, copyImage, renderShareImage } from "./now_share";

const DEFAULT_FONT = `"Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif`;
const NOT_CONNECTED = "Spotify isn't connected, so it can't be controlled from here";

const volRing = `<svg class="np-vol-ring" viewBox="0 0 36 36"><circle class="bg" cx="18" cy="18" r="16" stroke-dasharray="75.4 200" transform="rotate(135 18 18)"/><circle class="fg" cx="18" cy="18" r="16" stroke-dasharray="0 200" transform="rotate(135 18 18)"/></svg>`;

const TEMPLATE = `
<div class="np">
  <header class="np-head">
    <div class="np-cover"><div class="ph">${icon("note")}</div><img alt=""><img alt=""></div>
    <div class="np-meta">
      <div class="np-trow"><span class="np-title">Waiting for Spotify…</span><button class="np-like" data-k="like" title="Like (Ctrl+L)" hidden>${icon("heart_o")}</button></div>
      <div class="np-artist"></div>
      <div class="np-info"></div>
    </div>
    <div class="np-head-r">
      <button class="ib" data-k="more" title="Up next, lyric search, share">${icon("more")}</button>
      <button class="ib" data-k="fs" title="Full screen (F11)">${icon("full")}</button>
      <button class="np-rpc" data-k="rpc" title="Show lyrics on your Discord profile"><i></i><span>Not sharing</span></button>
    </div>
  </header>
  <div class="np-sheet"></div>
  <footer class="np-foot">
    <div class="np-seek"><div class="tr"><div class="fill"></div></div><i class="knob"></i><div class="tip">0:00</div></div>
    <div class="np-times"><span class="el">0:00</span><span class="tot">--:--</span></div>
    <div class="np-transport">
      <button class="ib side q" data-k="queue" title="Up next">${icon("queue")}</button>
      <button class="ib sh mark" data-k="shuffle" title="Shuffle (Ctrl+S)">${icon("shuffle")}</button>
      <button class="ib np-skip" data-k="prev" title="Previous (Ctrl+Left)">${icon("prev")}</button>
      <button class="ib np-play" data-k="play" title="Play / pause">${icon("play")}</button>
      <button class="ib np-skip" data-k="next" title="Next (Ctrl+Right)">${icon("next")}</button>
      <button class="ib rp mark" data-k="repeat" title="Repeat (Ctrl+R)">${icon("repeat")}<span class="bd1">1</span></button>
      <button class="ib side v" data-k="vol" title="Volume: scroll to change, click to mute">${volRing}<span class="vg">${icon("vol2")}</span></button>
    </div>
    <div class="np-ctl">
      <div class="np-delay"><span class="lab" data-k="delay_lbl">Delay</span>
        <div class="np-chip"><button data-k="dec" title="Lyrics 0.1 s later (Shift: every song)">${icon("minus")}</button><span class="val" data-k="delay" title="Double-click: reset">0.0s</span><button data-k="inc" title="Lyrics 0.1 s earlier (Shift: every song)">${icon("plus")}</button></div></div>
      <div class="np-acts">
        <button class="act" data-k="copy" title="Copy the current line (Ctrl+C)">Copy</button>
        <button class="act" data-k="top" title="Keep above other windows (Ctrl+T)">On top</button>
        <button class="act" data-k="overlay" title="Lyrics floating on your desktop">Overlay</button>
        <button class="act" data-k="mini" title="Mini player (Ctrl+M)">Mini</button>
        <button class="act more" data-k="ov_more" title="More" hidden>${icon("more")}</button>
      </div>
    </div>
    <div class="np-err"></div>
    <div class="np-status"><span class="d sp">Spotify</span><span class="d dc">Discord</span><span class="sl"></span><span class="warn"></span></div>
  </footer>
</div>`;

// Feature data pushed with Engine::set_extra has no static type.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

let snap: Snapshot | null = null;
let np: HTMLElement;
let sheet: Sheet;
let panels: Panels;
let toast: Toast;
let shown = false;
let raf = 0;

// ── DOM handles ─────────────────────────────────────────────
const q = <T extends HTMLElement = HTMLElement>(sel: string) => np.querySelector<T>(sel)!;
let elTitle: HTMLElement, elArtist: HTMLElement, elInfo: HTMLElement, elLike: HTMLElement, elRpc: HTMLElement;
let elSeek: HTMLElement, elFill: HTMLElement, elKnob: HTMLElement, elTip: HTMLElement, elEl: HTMLElement, elTot: HTMLElement;
let elPlay: HTMLElement, elVol: HTMLElement, elRing: SVGElement, elErr: HTMLElement, elSleep: HTMLElement, elWarn: HTMLElement;
let elDelayLab: HTMLElement, elDelayVal: HTMLElement, elFoot: HTMLElement, elSheet: HTMLElement;
let elRpcLabel: HTMLElement, elShuffle: HTMLElement, elRepeat: HTMLElement, elQueueBtn: HTMLElement, elVolGlyph: HTMLElement, elSp: HTMLElement, elDc: HTMLElement;
const actBtns: Record<string, HTMLElement> = {};
let covers: HTMLImageElement[] = [];
let coverPh: HTMLElement;

const ex = (k: string): Any => snap?.extras?.[k];
const setText = (e: HTMLElement, t: string) => { if (e.textContent !== t) e.textContent = t; };

// ── Local state ─────────────────────────────────────────────
let trackUri = "";
let trackAt = 0; // Date.now() when the track changed
let coverUrl = "";
let coverCur = 0;
let drag: number | null = null; // seek-bar drag fraction
let playOv: { playing: boolean; until: number } | null = null;
let posOv: { base: number; at: number; until: number } | null = null;
let frozen: { pos: number; at: number; until: number } | null = null;
let localErr = "";
let errTimer = 0;
let rlTimer = 0;
let prevVol = 0.5;
let volOv: { v: number; until: number } | null = null; // volume asked for, not yet confirmed
let fs = false;
let chromeT = 0;
let ema = 16.7;
let frames = 0;
let autoTier = 0;
let lastTs = 0;
let lastFill = "";
let lastEl = "";
let lastLiveTier = -1;
let lastBeatKey = "";
let lastRingDash = "";
let seekRect: DOMRect | null = null;
let seekW = 0;
let overflow: string[] = [];

type Cur = { baseKey: string; extra: string; subSig: string; kind: "synced" | "plain" | "none"; specs: RowSpec[]; plan: L.PlanRow[]; t0s: number[]; n: number; geo: string };
const cur: Cur = { baseKey: "", extra: "", subSig: "", kind: "none", specs: [], plan: [], t0s: [], n: 0, geo: "" };

const live = () => !!snap?.bridge_connected;
const nowS = () => performance.now() / 1000;

// ── Preferences, theme, backdrop ────────────────────────────
function prefs(): Any {
  return ex("np") ?? {};
}
const dark = () => prefs().dark_mode !== false;

function tier(): number {
  const rq = prefs().render_quality;
  if (rq === "high") return 0;
  if (rq === "low") return 2;
  return autoTier;
}

let themeKey = "";

function applyTheme() {
  const p = prefs();
  const animations = p.animations !== false;
  const isDark = dark();
  // Everything below is a pure function of these; a snapshot that moved only
  // the clock (or an unrelated extra) must not restyle the page root, which
  // would recalculate the style of every element under it.
  const palRef = ex("palette");
  const key = [animations, isDark, p.album_tint !== false, p.accent, p.render_quality, autoTier, snap?.track?.album_art ?? "", palRef?.uri ?? "", palRef?.accent ?? "", palRef?.base ?? ""].join("|");
  if (key === themeKey) return;
  themeKey = key;
  const html = document.documentElement;
  html.dataset.anim = animations ? "on" : "off";
  if (isDark) delete html.dataset.light; else html.dataset.light = "";
  np.classList.toggle("light", !isDark);
  const fg = L.fgRgb(isDark);
  np.style.setProperty("--fgc", fg.join(" "));

  const tint = p.album_tint !== false;
  const pal = ex("palette");
  const art = snap?.track?.album_art ?? "";
  const pv = tint && pal && pal.base ? pal : null;
  const accent: string = (pv ? (isDark ? pv.accent : pv.light?.accent) : null) ?? p.accent ?? "#1db954";
  const rgb = L.parseHex(accent) ?? [30, 215, 96];
  np.style.setProperty("--accc", L.mixAccent(rgb, fg).join(" "));
  html.style.setProperty("--accent", accent);

  // The window itself takes the cover's hue (statusify_colors.tinted_palette).
  const tokens = pv ? (isDark ? pv.tokens : pv.light?.tokens) ?? null : null;
  applySurfaces(tokens);
  const bg2 = tokens ? L.parseHex(tokens.BG2) : null;
  if (bg2) np.style.setProperty("--bgc", bg2.join(" ")); else np.style.removeProperty("--bgc");

  const bd = backdrop();
  if (!bd) return;
  // Cover mode (main._cover_mode): dark theme, album tint on, a cover loaded.
  // Only then is the picture darkened; without it the water shows as it is.
  const coverMode = tint && isDark && !!art && !!pv;
  bd.setCover(coverMode ? art : null);
  bd.setCoverMode(coverMode);
  if (pv) bd.setColors(isDark ? { base: pv.base, blobs: pv.blobs } : { base: pv.light.base, blobs: pv.light.blobs });
  else bd.setColors(neutralColors(isDark, accent));
  bd.setMotion(animations, tier() >= 2);
  np.style.setProperty("--ink", isDark ? "rgba(0,0,0,0.86)" : "rgba(255,255,255,0.94)");
}

/** The chosen lyric family, "" for the default ("Segoe UI"). */
const lyricFont = (): string => {
  const f: string = prefs().lyric_font || "";
  return f === "Segoe UI" ? "" : f;
};

function applyFont() {
  const f: string = lyricFont();
  const css = f ? `"${f.replace(/"/g, "")}", ${DEFAULT_FONT}` : DEFAULT_FONT;
  if (elSheet.style.getPropertyValue("--lyric-font") !== css) {
    elSheet.style.setProperty("--lyric-font", css);
    sheet.relayout();
  }
}

// ── Geometry ────────────────────────────────────────────────
function layoutActions() {
  const keys = ["copy", "top", "overlay", "mini"];
  const more = actBtns.ov_more;
  for (const k of keys) actBtns[k].hidden = false;
  more.hidden = true;
  const avail = q(".np-ctl").clientWidth - q(".np-delay").offsetWidth - 12;
  const widths: Record<string, number> = {};
  for (const k of keys) widths[k] = actBtns[k].offsetWidth + 4;
  const r = L.fitActions(keys, widths, avail, 38);
  overflow = r.overflow;
  for (const k of keys) actBtns[k].hidden = !r.shown.includes(k);
  more.hidden = !r.overflow.length;
}

/** What geometry depends on besides the window's size (which the ResizeObservers watch). */
let geoPrefs = "";

function updateGeometry() {
  if (!np) return;
  // Reading clientWidth/offsetHeight forces a layout if anything is dirty,
  // so this runs only when something geometry depends on changed, never on
  // every snapshot.
  const W = np.clientWidth, H = np.clientHeight;
  if (!W || !H) return;
  const boost = Number(prefs().lyric_font_boost ?? 0);
  const px = L.lyricPx(boost, fs ? L.fsScale(H) : 1);
  const footH = elFoot.offsetHeight;
  const headH = 86;
  np.style.setProperty("--foot-h", `${footH}px`);
  np.style.setProperty("--head-h", `${headH}px`);
  const band = fs ? H - 48 : H - (headH + 4) - (footH + 4);
  const pad = fs ? Math.max(28, Math.trunc(W * 0.08)) : 28;
  const geo = `${px}|${band}|${fs}|${pad}`;
  if (geo === cur.geo) return;
  cur.geo = geo;
  sheet.setGeometry(px, Math.max(0, band), fs ? 0.36 : 0.3, fs ? 96 : 56, pad);
}

// ── Header ──────────────────────────────────────────────────
function trackSwap(newUri: string) {
  const meta = q(".np-meta");
  if (trackUri && newUri !== trackUri) {
    // The old title block fades up and away while the new one rises in.
    const old = meta.cloneNode(true) as HTMLElement;
    old.classList.add("old");
    old.querySelectorAll("[data-k]").forEach((e) => e.removeAttribute("data-k"));
    meta.parentElement!.insertBefore(old, meta);
    window.setTimeout(() => old.remove(), 480);
    meta.style.animation = "none";
    void meta.offsetWidth;
    meta.style.animation = "";
  }
  trackUri = newUri;
  trackAt = Date.now();
}

/** The cover thumbnail crossfades (0.35 s, rising from 96 %). */
function setCover(url: string) {
  if (url === coverUrl) return;
  coverUrl = url;
  const old = covers[coverCur];
  if (!url) {
    old.classList.remove("on");
    coverPh.classList.remove("gone");
    return;
  }
  const next = covers[1 - coverCur];
  next.onload = () => {
    if (coverUrl !== url) return;
    next.classList.add("on");
    old.classList.remove("on");
    coverPh.classList.add("gone");
    coverCur = 1 - coverCur;
  };
  next.onerror = () => {
    if (coverUrl !== url) return;
    next.classList.remove("on");
    old.classList.remove("on");
    coverPh.classList.remove("gone");
  };
  next.src = url;
}

function infoText(s: Snapshot): string {
  const l = s.lyrics;
  if (!s.track) return "";
  if (l.mode === "synced" || l.mode === "plain") return l.source + (l.mode === "plain" ? " (plain)" : "");
  return Date.now() - trackAt < 8000 ? "Looking for lyrics…" : "No lyrics";
}

function renderHeader(s: Snapshot) {
  const t = s.track;
  const uri = t?.uri ?? "";
  if (uri !== trackUri) trackSwap(uri);
  setText(elTitle, t ? t.title : "Waiting for Spotify…");
  setText(elArtist, t ? t.artist : "");
  setText(elInfo, infoText(s));
  setCover(t?.album_art ?? "");
  elLike.hidden = !t;
  const liked = !!ex("player")?.liked;
  elLike.classList.toggle("on", liked);
  const g = liked ? "heart" : "heart_o";
  if (elLike.dataset.g !== g) { elLike.dataset.g = g; elLike.innerHTML = icon(g); }
  const pill = L.rpcPill(ex("core")?.rpc_enabled, s.discord_user, ex("shell")?.app_id_set, ex("core")?.discord_error);
  elRpc.classList.toggle("on", pill.state === "on");
  elRpc.classList.toggle("waiting", pill.state === "waiting");
  if (elRpc.title !== pill.title) elRpc.title = pill.title;
  setText(elRpcLabel, pill.label);
}

// ── Sheet model ─────────────────────────────────────────────
function validTiming(s: Snapshot): Any {
  const t = ex("np_timing");
  const syn = s.lyrics.synced;
  if (!t || !s.track || t.uri !== s.track.uri || !Array.isArray(t.lines) || t.n !== syn.length || !syn.length) return null;
  if (t.start0 !== syn[0].startMs || t.startN !== syn[syn.length - 1].startMs) return null;
  return t;
}

function buildSpecs(s: Snapshot, kind: Cur["kind"], timing: Any): { specs: RowSpec[]; plan: L.PlanRow[] } {
  if (kind === "synced") {
    const lines: L.SyncedLine[] = s.lyrics.synced.map((l, i) => ({ ...l, ...(timing?.lines?.[i] ?? {}) }));
    const plan = L.buildPlan(lines, L.calcInstrumentalGaps(lines, s.duration_ms), s.duration_ms);
    const specs: RowSpec[] = plan.map((r) => r.kind === "line"
      ? { kind: "line" as const, k: r.k, t0: r.t0, t1: r.t1, text: (lines[r.k].words || "").trim(), syl: lines[r.k].syl }
      : { kind: r.kind, k: r.k, t0: r.t0, t1: r.t1, text: "" });
    return { specs, plan };
  }
  if (kind === "plain") {
    return { specs: s.lyrics.plain.map((t, i) => ({ kind: "plain" as const, k: i, t0: 0, t1: null, text: t.trim() })), plan: [] };
  }
  return { specs: [{ kind: "static", k: -1, t0: 0, t1: null, text: "" }], plan: [] };
}

let subsFor: { tr: unknown; specs: unknown; out: { subs: (string | null)[] | null; sig: string } } | null = null;

function computeSubs(s: Snapshot): { subs: (string | null)[] | null; sig: string } {
  const tr = ex("translation");
  if (!tr || !s.track || tr.uri !== s.track.uri || tr.mode === "off" || !tr.data) return { subs: null, sig: "off" };
  // The same translation over the same rows gives the same sublines.
  if (subsFor && subsFor.tr === tr && subsFor.specs === cur.specs) return subsFor.out;
  const out = computeSubsNow(tr);
  subsFor = { tr, specs: cur.specs, out };
  return out;
}

function computeSubsNow(tr: Any): { subs: (string | null)[] | null; sig: string } {
  const subs = cur.specs.map((sp, i) => {
    if (cur.kind === "synced") return sp.kind === "line" ? L.subline(tr.mode, tr.data[String(sp.k)]) : null;
    if (cur.kind === "plain") return L.subline(tr.mode, tr.data[String(i)]);
    return null;
  });
  const any = subs.some(Boolean);
  return { subs: any ? subs : null, sig: any ? `${tr.status}|${tr.mode}|${tr.lang}|${Object.keys(tr.data).length}` : "none" };
}

function syncSheet(s: Snapshot) {
  const l = s.lyrics;
  const kind: Cur["kind"] = l.mode === "synced" && l.synced.length ? "synced" : l.mode === "plain" && l.plain.length ? "plain" : "none";
  const n = kind === "synced" ? l.synced.length : kind === "plain" ? l.plain.length : 0;
  const firstWords = kind === "synced" ? l.synced[0].words : kind === "plain" ? l.plain[0] : "";
  const lastWords = kind === "synced" ? l.synced[n - 1].words : kind === "plain" ? l.plain[n - 1] : "";
  const baseKey = `${s.track?.uri ?? ""}|${kind}|${l.source}|${n}|${firstWords}|${lastWords}`;
  const timing = kind === "synced" ? validTiming(s) : null;
  // What refines the same lyrics: syllable timing arriving, the duration becoming known.
  const extra = `${timing ? 1 : 0}|${kind === "synced" ? s.duration_ms : 0}`;
  let rebuilt = false;
  if (baseKey !== cur.baseKey || extra !== cur.extra) {
    const quiet = baseKey === cur.baseKey;
    const { specs, plan } = buildSpecs(s, kind, timing);
    Object.assign(cur, { baseKey, extra, kind, specs, plan, t0s: plan.map((r) => r.t0), n });
    sheet.setModel(baseKey, specs, kind === "synced", nowS(), quiet);
    rebuilt = true;
    cur.geo = "";
    updateGeometry();
  }
  const { subs, sig } = computeSubs(s);
  if (rebuilt || sig !== cur.subSig) {
    cur.subSig = sig;
    sheet.setSubs(subs);
  }
}

// ── Position and play state as shown ────────────────────────
/** Play state as shown: an optimistic flip after a click, until the bridge confirms (or 1.5 s pass). */
function playing(s: Snapshot): boolean {
  const ov = playOv;
  if (ov && Date.now() < ov.until && s.is_playing !== ov.playing) return ov.playing;
  return s.is_playing;
}

function curPos(s: Snapshot): number {
  const now = Date.now();
  if (posOv && now < posOv.until && s.position_at_ms < posOv.at) {
    const p = posOv.base + (s.is_playing ? now - posOv.at : 0);
    return s.duration_ms > 0 ? Math.min(p, s.duration_ms) : p;
  }
  if (frozen && now < frozen.until && s.position_at_ms < frozen.at) return frozen.pos;
  return position(s);
}

const offsetMs = () => Number(ex("core")?.track_offset_ms ?? prefs().lyric_delay_ms ?? 0);

// ── Footer ──────────────────────────────────────────────────
function renderFooter(s: Snapshot) {
  const p = ex("player") ?? {};
  const pl = playing(s);
  const lv = live();
  const glyph = pl ? "pause" : "play";
  if (elPlay.dataset.g !== glyph) { elPlay.dataset.g = glyph; elPlay.innerHTML = icon(glyph); }
  elShuffle.classList.toggle("on", !!p.shuffle);
  elRepeat.classList.toggle("on", Number(p.repeat) > 0);
  elRepeat.classList.toggle("one", Number(p.repeat) === 2);
  elQueueBtn.classList.toggle("on", panels?.open === "queue");
  const v = curVolume();
  const vg = v <= 0.001 ? "vol0" : v < 0.5 ? "vol1" : "vol2";
  if (elVolGlyph.dataset.g !== vg) { elVolGlyph.dataset.g = vg; elVolGlyph.innerHTML = icon(vg); }
  const dash = `${(75.4 * L.clamp(v)).toFixed(1)} 200`;
  if (lastRingDash !== dash) { lastRingDash = dash; elRing.setAttribute("stroke-dasharray", dash); }
  np.classList.toggle("dead", !lv);

  const off = offsetMs();
  setText(elDelayVal, L.fmtOffset(off));
  elDelayVal.classList.toggle("off", off !== 0);
  const song = !!prefs().song_offset;
  setText(elDelayLab, song ? "This song" : "Delay");
  elDelayLab.classList.toggle("song", song);

  actBtns.top.classList.toggle("on", !!prefs().always_on_top);
  actBtns.overlay.classList.toggle("on", !!ex("windows")?.overlay);

  elSp.classList.toggle("on", lv);
  elDc.classList.toggle("on", !!s.discord_user);
  const dcTitle = s.discord_user ? `Connected as ${s.discord_user}` : "Not connected";
  if (elDc.title !== dcTitle) elDc.title = dcTitle;
  const sleep: string = ex("core")?.sleep_label || "";
  if (elSleep.dataset.s !== sleep) {
    elSleep.dataset.s = sleep;
    elSleep.innerHTML = sleep ? `${icon("moon")}<span></span>` : "";
    if (sleep) elSleep.lastElementChild!.textContent = sleep;
  }
  renderRight();

  const { text: err, fix: fixable } = L.footerNotice(localErr, s.note || "", String(ex("core")?.bridge_warning || ""));
  setText(elErr, err);
  elErr.classList.toggle("click", fixable);
  const errTitle = fixable ? "Click to repair the Spotify connection" : "";
  if (elErr.title !== errTitle) elErr.title = errTitle;
}

/** Right of the status row: dropped lines, and "Rate limited · Ns" counting down
 *  (every 500 ms, like Python's _start_rl_countdown) while the planner holds a line back. */
function renderRight() {
  const core = ex("core");
  const rl = L.rateLimitLabel(core?.rate_limited_until_ms, Date.now());
  setText(elWarn, L.statusRight(Number(core?.dropped_lines ?? 0), rl));
  if (rl && !rlTimer) rlTimer = window.setInterval(renderRight, 500);
  else if (!rl && rlTimer) { window.clearInterval(rlTimer); rlTimer = 0; }
}

function showError(msg: string) {
  localErr = msg;
  window.clearTimeout(errTimer);
  errTimer = window.setTimeout(() => { localErr = ""; if (snap) renderFooter(snap); }, 8000);
  if (snap) renderFooter(snap);
}

// ── Commands ────────────────────────────────────────────────
function stateText(what: string): string {
  const p = ex("player") ?? {};
  // The optimistic flip lands with the next snapshot; say what it will be.
  if (what === "shuffle") return p.shuffle ? "Shuffle off" : "Shuffle on";
  if (what === "repeat") return ["Repeat all", "Repeat this song", "Repeat off"][Number(p.repeat) % 3];
  return p.liked ? "Removed from Liked Songs" : "Added to Liked Songs";
}

async function cmd(action: string): Promise<boolean> {
  const text = ["shuffle", "repeat", "like"].includes(action) ? stateText(action) : "";
  const ok = await call<boolean>("nowplaying", "player_cmd", { action }).catch(() => false);
  if (!ok) { showError(NOT_CONNECTED); return false; }
  if (action === "toggle" && snap) {
    const pl = playing(snap);
    // Freeze the estimate where it is, or it keeps running.
    if (pl) frozen = { pos: curPos(snap), at: Date.now(), until: Date.now() + 1500 };
    playOv = { playing: !pl, until: Date.now() + 1500 };
  }
  if (text) toast.show(text);
  if (snap) renderFooter(snap);
  return true;
}

async function seekTo(ms: number): Promise<boolean> {
  ms = Math.max(0, Math.trunc(ms));
  const ok = await seek(ms).catch(() => false);
  if (!ok) { showError(NOT_CONNECTED); return false; }
  // The page moves there at once; the bridge confirms a moment later.
  posOv = { base: ms, at: Date.now(), until: Date.now() + 1200 };
  frozen = null;
  return true;
}

/** The volume as the page shows it: what was just asked for until Spotify says so,
 *  so wheel ticks arriving before the answer add up instead of all starting from
 *  the same base (Python read state.volume, which set_volume updated at once). */
function curVolume(): number {
  if (volOv && Date.now() < volOv.until) return volOv.v;
  return Number(ex("player")?.volume ?? 1);
}

async function volume(v: number) {
  v = L.clamp(Math.round(v * 20) / 20);
  const prev = volOv;
  volOv = { v, until: Date.now() + 1500 };
  if (snap) renderFooter(snap);
  if (v > 0) prevVol = v;
  toast.show(v > 0 ? `Volume ${Math.round(v * 100)}%` : "Muted", v);
  const ok = await call<boolean>("nowplaying", "set_volume", { value: v }).catch(() => false);
  if (!ok) {
    if (volOv?.v === v) volOv = prev && Date.now() < prev.until ? prev : null;
    if (snap) renderFooter(snap);
    toast.show("Spotify isn't connected");
  }
}
const volumeStep = (n: number) => volume(L.stepVolume(curVolume(), n));
function toggleMute() {
  const v = curVolume();
  if (v > 0.001) { prevVol = v; void volume(0); } else void volume(prevVol > 0.001 ? prevVol : 0.5);
}

/** The footer stepper adjusts this song's own offset; Shift (or no track) the global delay. */
async function nudge(delta: number, global: boolean) {
  if (snap?.track && !global) await call("core", "nudge_track_offset", { delta_ms: delta }).catch(() => {});
  else await call("nowplaying", "nudge_global_delay", { delta_ms: delta }).catch(() => {});
}

async function resetDelay() {
  if (prefs().song_offset) await call("core", "set_track_offset", { ms: null }).catch(() => {});
  else await call("nowplaying", "set_pref", { key: "lyric_delay_ms", value: 0 }).catch(() => {});
}

// ── Lines, sharing, menus ───────────────────────────────────
function currentRow(): number {
  let i = sheet.focus;
  while (i >= 0 && ["intro", "gap", "static"].includes(cur.specs[i]?.kind)) i--;
  return i;
}

function lineTexts(i: number): { cur: string; nxt: string } {
  const sp = cur.specs[i];
  if (!sp || !sp.text) return { cur: "", nxt: "" };
  let nxt = "";
  for (let j = i + 1; j < Math.min(cur.specs.length, i + 4); j++) {
    if (cur.specs[j].text) { nxt = cur.specs[j].text; break; }
  }
  return { cur: sp.text, nxt };
}

async function copyText(text: string, msg: string) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); toast.show(msg); } catch { toast.show("Couldn't copy"); }
}

function copyCurrent() {
  void copyText(lineTexts(currentRow()).cur, "Line copied");
}

async function shareLine(i: number, save: boolean) {
  const { cur: line, nxt } = lineTexts(i);
  const t = snap?.track;
  if (!line || !t) return;
  let path = "";
  if (save) {
    // Always ask where, like Python's Save dialog ("Artist - Title.png" to start with).
    const r = await call<{ path?: string; cancelled?: boolean }>("nowplaying", "pick_save_path", { name: L.safeName(t.artist, t.title) }).catch(() => null);
    if (r?.cancelled) return;
    if (r?.path) path = r.path;
    else toast.show("No Save dialog here, saving to Pictures\Statusify");
  }
  try {
    const art = t.album_art ? await call<string | null>("nowplaying", "art", { url: t.album_art, size: 640 }).catch(() => null) : null;
    const pal = ex("palette");
    const f = lyricFont();
    const blob = await renderShareImage({
      line, next: nxt, title: t.title, artist: t.artist, cover: art,
      palette: pal?.blobs ?? [], base: pal?.base ?? null,
      accent: pal?.accent ?? prefs().accent ?? "#1db954",
      family: f ? `"${f}", ${DEFAULT_FONT}` : DEFAULT_FONT,
    });
    if (!save) {
      toast.show((await copyImage(blob)) ? "Image copied" : "Couldn't copy the image");
      return;
    }
    const saved = await call<string>("nowplaying", "save_image", { path, name: L.safeName(t.artist, t.title), data: await blobToBase64(blob) });
    toast.show(`Image saved to ${L.shortPath(String(saved))}`);
  } catch (e) {
    console.warn("lyric image failed", e);
    toast.show(save ? "Couldn't save the image" : "Couldn't make the image");
  }
}

function lyricsMenu(liveTrack: boolean): MenuItem[] {
  return [
    { id: "queue", label: "Up next", enabled: true, checked: panels.open === "queue" },
    { id: "search", label: "Wrong lyrics? Search…", enabled: liveTrack },
    { id: "unpin", label: "Use Spotify's lyrics again", enabled: liveTrack && !!prefs().pinned },
  ];
}

function sheetMenu(x: number, y: number, row?: number) {
  const i = row ?? currentRow();
  const has = i >= 0 && !!lineTexts(i).cur;
  panels.menu([
    { id: `copy_line:${i}`, label: "Copy line", enabled: has },
    { id: `share:${i}`, label: "Share as image…", enabled: has },
    { id: `copy_img:${i}`, label: "Copy image", enabled: has },
    null,
    ...lyricsMenu(!!snap?.track),
  ], { x, y, where: "pt" });
}

async function unpin() {
  const r = await call<{ had: boolean; restored: boolean }>("nowplaying", "unpin").catch(() => null);
  if (r?.restored) toast.show("Back to Spotify's lyrics");
  else if (r?.had) toast.show("Spotify's lyrics come back next time this song plays");
  panels.close();
}

function exitFullscreen() {
  if (fs) void toggleFullscreen();
}

function footerAction(k: string) {
  if (k === "copy") copyCurrent();
  else if (k === "top") void call("nowplaying", "toggle_topmost").catch(() => {});
  else if (k === "overlay") void call("windows", "toggle_overlay").catch(() => showError("The overlay isn't available"));
  else if (k === "mini") {
    exitFullscreen();
    void call("windows", "toggle_mini").catch(() => showError("The mini player isn't available"));
  }
}

function onMenu(id: string) {
  if (id.startsWith("copy_line:") || id.startsWith("share:") || id.startsWith("copy_img:")) {
    const [what, n] = id.split(":");
    const i = Number(n);
    if (what === "copy_line") void copyText(lineTexts(i).cur, "Line copied");
    else void shareLine(i, what === "share");
  } else if (id === "queue") panels.toggleQueue();
  else if (id === "search") panels.openSearch();
  else if (id === "unpin") void unpin();
  else if (id.startsWith("ov:")) footerAction(id.slice(3));
}

// ── Fullscreen ──────────────────────────────────────────────
/** Full screen lyrics: the window itself goes fullscreen through the backend;
 *  the browser's own fullscreen is the fallback outside the app. */
async function toggleFullscreen() {
  const on = !fs;
  if (on && prefsMini()) return;
  const r = await call<boolean>("nowplaying", "set_fullscreen", { on }).catch(() => null);
  if (r !== null && r !== undefined) return setFs(on);
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    toast.show("Full screen isn't available here");
  }
}

/** Mini mode owns the window. */
const prefsMini = () => !!ex("windows")?.mini;

function setFs(on: boolean) {
  if (on === fs) return;
  fs = on;
  const html = document.documentElement;
  if (fs) html.dataset.npfs = ""; else delete html.dataset.npfs;
  np.classList.toggle("fs", fs);
  np.classList.remove("idle");
  chromeT = nowS();
  // A full-screen frame is much bigger; the adaptive quality measures it afresh.
  autoTier = 0; frames = 0; ema = 16.7;
  q('[data-k="fs"]').innerHTML = icon(fs ? "unfull" : "full");
  cur.geo = "";
  requestAnimationFrame(() => { updateGeometry(); sheet.relayout(); layoutActions(); });
}

/** The window left full screen by other means (Alt+Enter, a shell shortcut). */
function onResize() {
  if (fs && !document.fullscreenElement && Math.abs(window.innerHeight - screen.height) > 6 && Math.abs(window.innerWidth - screen.width) > 6) setFs(false);
}

// ── Frame ───────────────────────────────────────────────────
function tick(ts: number) {
  if (!shown) { raf = 0; return; }
  raf = requestAnimationFrame(tick);
  const dt = lastTs ? ts - lastTs : 16.7;
  lastTs = ts;
  const s = snap;
  if (!s) return;
  const pl = playing(s);

  // Adaptive quality: a slow frame clock lowers the effects (tier 1 drops the
  // beat swell and the breathing; tier 2 keeps the background still).
  if (pl && dt < 250) {
    ema = ema * 0.92 + dt * 0.08;
    const rq = prefs().render_quality;
    if (++frames >= 40 && rq !== "high" && rq !== "low") {
      const want = L.tierFor(ema);
      if (want > autoTier) {
        autoTier = want;
        frames = 0;
        console.info(`Lyric sheet: frames take ${ema.toFixed(0)} ms here, lowering effects (tier ${want})`);
        applyTheme();
      }
    }
  }

  const pos = curPos(s);
  const dur = s.duration_ms;

  // Seek bar and times: a transform and two strings, nothing that lays out.
  // The fill is written when it has moved a quarter pixel (a 3-minute song
  // moves it 2 px a second), not on every frame.
  const frac = drag ?? (dur > 0 ? L.clamp(pos / dur) : 0);
  const key = (Math.round(frac * Math.max(1, seekW) * 4) / 4).toFixed(2);
  if (key !== lastFill) {
    lastFill = key;
    elFill.style.transform = `scaleX(${frac})`;
    elKnob.style.transform = `translate3d(${(frac * seekW - 7).toFixed(1)}px,0,0)`;
  }
  const el = L.fmtTime(drag !== null && dur ? drag * dur : pos);
  if (el !== lastEl) {
    lastEl = el;
    elEl.textContent = el;
  }
  setText(elTot, dur ? L.fmtTime(dur) : "--:--");

  // The sheet follows position + offset.
  const t = pos + offsetMs();
  const idx = cur.kind === "synced" ? L.planIndex(cur.t0s, t) : cur.kind === "plain" ? L.plainIndex(cur.n, t, dur) : 0;
  if (idx !== sheet.focus) sheet.applyFocus(idx, { now: nowS() });
  const tr = tier();
  if (tr !== lastLiveTier) { lastLiveTier = tr; sheet.setMotion(prefs().animations !== false, tr); }
  sheet.setPlaying(pl || cur.kind !== "synced");
  sheet.frame({ t, now: nowS(), playing: pl, tier: tr });

  // A beat swells the water for a moment (smooth tier, beat data only).
  const bd = backdrop();
  if (bd) {
    const beats = ex("beats");
    const on = !!beats?.beats?.length && beats.uri === s.track?.uri && tr === 0 && prefs().animations !== false && prefs().beat_react !== false && pl;
    const lift = on ? L.beatLevel(beats.beats, pos).lift : 0;
    const bk = String(lift);
    if (bk !== lastBeatKey) { lastBeatKey = bk; bd.setBeat(lift); }
  }

  // Fullscreen: the controls fade out while the pointer rests.
  if (fs) {
    const idle = nowS() - chromeT > L.CHROME_IDLE_S && drag === null && !panels.open && !panels.menuOpen;
    if (idle !== np.classList.contains("idle")) np.classList.toggle("idle", idle);
  }
}

// ── Snapshot ────────────────────────────────────────────────
let lastQueueSig = "";
let infoTimer = 0;

/** The queue as the panel shows it; a new snapshot object with the same queue is no change. */
const queueSig = (q: QueueItem[] | undefined) => (q ?? []).map((t) => `${t.uri}|${t.uid}|${t.title}|${t.album_art}`).join("\n");

function render(s: Snapshot) {
  snap = s;
  if (!np) return;
  if ((s.track?.uri ?? "") !== trackUri) { posOv = null; frozen = null; playOv = null; }
  if (playOv && s.is_playing === playOv.playing) playOv = null;
  if (volOv && Math.abs(Number(s.extras?.player?.volume) - volOv.v) < 0.005) volOv = null;
  applyTheme();
  applyFont();
  renderHeader(s);
  syncSheet(s);
  renderFooter(s);
  const queue = ex("queue") as QueueItem[] | undefined;
  const qs = queueSig(queue);
  if (qs !== lastQueueSig) { lastQueueSig = qs; panels.setQueue(queue ?? []); }
  panels.setPinned(!!prefs().pinned);
  if (s.lyrics.mode === "none" && s.track) {
    // "Looking for lyrics…" turns into "No lyrics" after 8 s.
    window.clearTimeout(infoTimer);
    infoTimer = window.setTimeout(() => snap && setText(elInfo, infoText(snap)), Math.max(50, 8100 - (Date.now() - trackAt)));
  }
  const gp = `${prefs().lyric_font_boost ?? 0}|${fs}`;
  if (gp !== geoPrefs) { geoPrefs = gp; updateGeometry(); }
}

// ── Interaction ─────────────────────────────────────────────
function isTyping(e: Event): boolean {
  const t = e.target as HTMLElement | null;
  return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}

function onKey(e: KeyboardEvent) {
  if (!shown) return;
  if (e.key === "Escape") {
    // A menu first, then a panel, then full screen.
    if (panels.closeOverlays()) e.preventDefault();
    else if (fs) { e.preventDefault(); exitFullscreen(); }
    return;
  }
  if (e.key === "F11") { e.preventDefault(); void toggleFullscreen(); return; }
  if (isTyping(e) || !e.ctrlKey || e.altKey || e.metaKey) return;
  const map: Record<string, () => void> = {
    arrowup: () => void volumeStep(1), arrowdown: () => void volumeStep(-1),
    arrowleft: () => void cmd("prev"), arrowright: () => void cmd("next"),
    s: () => void cmd("shuffle"), r: () => void cmd("repeat"), l: () => void cmd("like"),
    m: () => footerAction("mini"), t: () => footerAction("top"),
    c: () => { if (!window.getSelection()?.toString()) copyCurrent(); },
  };
  const f = map[e.key.toLowerCase()];
  if (f) { e.preventDefault(); f(); }
}

function onClick(e: MouseEvent) {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-k]");
  if (!b || !np.contains(b)) return;
  const k = b.dataset.k!;
  const box = b.getBoundingClientRect();
  switch (k) {
    case "rpc": void call("core", "toggle_rpc").catch(() => showError("Presence isn't available")); break;
    case "fs": void toggleFullscreen(); break;
    case "more": {
      const i = currentRow();
      const has = i >= 0 && !!lineTexts(i).cur;
      panels.menu([
        ...lyricsMenu(!!snap?.track), null,
        { id: `share:${i}`, label: "Share current line…", enabled: has },
        { id: `copy_img:${i}`, label: "Copy line as image", enabled: has },
      ], { x: box.right, y: box.bottom + 6, where: "below" });
      break;
    }
    case "queue": panels.toggleQueue(); break;
    case "shuffle": case "repeat": case "like": case "prev": case "next": void cmd(k); break;
    case "play": void cmd("toggle"); break;
    case "vol": toggleMute(); break;
    case "dec": void nudge(-100, e.shiftKey); break;
    case "inc": void nudge(100, e.shiftKey); break;
    case "copy": case "top": case "overlay": case "mini": footerAction(k); break;
    case "ov_more":
      panels.menu(overflow.map((o) => ({
        id: `ov:${o}`, enabled: true,
        label: ({ copy: "Copy", top: "On top", overlay: "Overlay", mini: "Mini" } as Record<string, string>)[o],
        checked: (o === "top" && !!prefs().always_on_top) || (o === "overlay" && !!ex("windows")?.overlay),
      })), { x: box.right, y: box.top - 6, where: "above" });
      break;
  }
}

function seekFracAt(x: number): number {
  const r = seekRect ?? elSeek.getBoundingClientRect();
  return L.seekFrac(x, r.left, r.width);
}

/** The time a click would land on, over the pointer. */
function moveTip(x: number) {
  const r = seekRect ?? elSeek.getBoundingClientRect();
  const dur = snap?.duration_ms ?? 0;
  if (!dur) return;
  const f = L.seekFrac(drag !== null ? r.left + drag * r.width : x, r.left, r.width);
  elTip.textContent = L.fmtTime(f * dur);
  const w = elTip.offsetWidth;
  const left = Math.max(0, Math.min(r.width - w, f * r.width - w / 2));
  elTip.style.transform = `translate3d(${left}px,0,0)`;
}

function wireSeek() {
  elSeek.addEventListener("pointerdown", (e) => {
    seekRect = elSeek.getBoundingClientRect();
    elSeek.setPointerCapture(e.pointerId);
    elSeek.classList.add("drag");
    drag = seekFracAt(e.clientX);
    moveTip(e.clientX);
  });
  elSeek.addEventListener("pointermove", (e) => {
    if (drag === null) seekRect = elSeek.getBoundingClientRect();
    else drag = seekFracAt(e.clientX);
    moveTip(e.clientX);
  });
  elSeek.addEventListener("pointerup", (e) => {
    if (drag === null) return;
    const f = seekFracAt(e.clientX);
    drag = null;
    elSeek.classList.remove("drag");
    if (snap?.duration_ms) void seekTo(f * snap.duration_ms);
  });
  elSeek.addEventListener("pointercancel", () => { drag = null; elSeek.classList.remove("drag"); });
}

function wireWheel() {
  // Over the play button or the speaker the wheel changes the volume.
  for (const b of [elPlay, elVol]) {
    b.addEventListener("wheel", (e) => {
      e.preventDefault();
      void volumeStep(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });
  }
  elVol.addEventListener("pointerenter", () => {
    const v = curVolume();
    toast.show((v > 0.001 ? `Volume ${Math.round(v * 100)}%` : "Muted") + "  ·  scroll to change, click to mute", v);
  });
}

function wireDelay() {
  elDelayVal.addEventListener("dblclick", () => void resetDelay());
  // Right-click on the stepper: this song follows the global delay again.
  const reset = (e: Event) => { e.preventDefault(); if (prefs().song_offset) void resetDelay(); };
  for (const e of [elDelayVal, elDelayLab, q('[data-k="dec"]'), q('[data-k="inc"]')]) e.addEventListener("contextmenu", reset);
  elErr.addEventListener("click", () => {
    if (elErr.classList.contains("click")) void call("core", "repair_bridge").catch(() => {});
  });
}

// ── Page contract (src/main.ts) ─────────────────────────────
export function mount(root: HTMLElement) {
  root.innerHTML = TEMPLATE;
  np = root.querySelector<HTMLElement>(".np")!;
  elFoot = q(".np-foot");
  elSheet = q(".np-sheet");
  elTitle = q(".np-title");
  elArtist = q(".np-artist");
  elInfo = q(".np-info");
  elLike = q(".np-like");
  elRpc = q(".np-rpc");
  elSeek = q(".np-seek");
  elFill = q(".fill");
  elKnob = q(".knob");
  elTip = q(".tip");
  elEl = q(".el");
  elTot = q(".tot");
  elPlay = q(".np-play");
  elVol = q(".v");
  elRing = q(".np-vol-ring .fg") as unknown as SVGElement;
  elErr = q(".np-err");
  elSleep = q(".sl");
  elWarn = q(".warn");
  elDelayLab = q(".np-delay .lab");
  elDelayVal = q(".np-chip .val");
  elRpcLabel = elRpc.querySelector("span")!;
  elShuffle = q(".sh");
  elRepeat = q(".rp");
  elQueueBtn = q(".q");
  elVolGlyph = q(".vg");
  elSp = q(".sp");
  elDc = q(".dc");
  covers = [...np.querySelectorAll<HTMLImageElement>(".np-cover img")];
  coverPh = q(".np-cover .ph");
  for (const k of ["copy", "top", "overlay", "mini", "ov_more"]) actBtns[k] = q(`[data-k="${k}"]`);

  sheet = new Sheet(elSheet);
  toast = new Toast(np);
  panels = new Panels({
    root: np, call, track: () => snap?.track ?? null, isPinned: () => !!prefs().pinned,
    toast: (t, f) => toast.show(t, f), error: showError, onMenu,
  });
  np.addEventListener("np-panel", () => snap && renderFooter(snap));
  sheet.onSeekRow = (i, row) => {
    // The sheet shows position + offset, so land on the line's start.
    const start = row.kind === "intro" ? 0 : row.t0;
    void i;
    sheet.followNow();
    void seekTo(Math.max(0, start - offsetMs() + 40));
  };
  np.addEventListener("click", onClick);
  np.addEventListener("contextmenu", (e) => {
    const t = e.target as HTMLElement;
    if (t.closest(".np-foot, .np-head, .np-panel, .np-menu") || isTyping(e)) return;
    e.preventDefault();
    const ln = t.closest<HTMLElement>(".ln");
    sheetMenu(e.clientX, e.clientY, ln && cur.kind === "synced" ? Number(ln.dataset.i) : undefined);
  });
  np.addEventListener("pointermove", () => {
    if (!fs) return;
    chromeT = nowS();
    if (np.classList.contains("idle")) np.classList.remove("idle");
  });
  startNav();
  wireSeek();
  wireWheel();
  wireDelay();
  window.addEventListener("keydown", onKey);
  document.addEventListener("fullscreenchange", () => setFs(!!document.fullscreenElement));
  window.addEventListener("resize", onResize);
  new ResizeObserver(() => { seekW = elSeek.clientWidth; lastFill = ""; updateGeometry(); layoutActions(); }).observe(np);
  new ResizeObserver(() => updateGeometry()).observe(elFoot);
  // The dev harness (tests-ts/harness.html) inspects the sheet through this.
  const dbg = window as unknown as { __npDebug?: boolean; __np?: unknown };
  if (dbg.__npDebug) dbg.__np = { sheet, panels, cur };
  onSnapshot(render);
}

export function show() {
  shown = true;
  lastTs = 0;
  if (!raf) raf = requestAnimationFrame(tick);
  requestAnimationFrame(() => { updateGeometry(); sheet.relayout(); layoutActions(); });
}

export function hide() {
  shown = false;
  panels.closeOverlays();
  backdrop()?.setBeat(0);
  lastBeatKey = "";
}
