// History page (owner: history agent): every play grouped by day, searchable,
// with each play's lyrics on a sheet that slides in over the list. Port of
// statusify_ui_history.py. Data comes from the Rust "history" feature.
/// <reference types="vite/client" />
import { listen } from "@tauri-apps/api/event";
import { call, onSnapshot, position, type Snapshot } from "../api";
import "./history.css";

const PAGE = 60; // plays per page ("Show more" adds this many)

// ── Types (shapes produced by features/history.rs) ──
export type Row = {
  id: number;
  track_uri: string;
  artist: string;
  title: string;
  album_art: string;
  played_at: string;
  hm: string;
  time: string;
  listened_ms: number;
  badge: "Synced" | "Plain" | null;
  lines: number;
};
type Group = { day: string; label: string; summary: string; entries: Row[] };
type ListResp = { query: string; groups: Group[]; more: boolean; count: number; caption: string; latest_id: number };
type Detail = Row & {
  mode: string;
  synced: { startMs: number; words: string }[];
  plain: string[];
  meta: string;
};

// ── Small DOM helpers (shared with the Stats page) ──
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** A rounded cover; a note glyph until (or unless) the art loads. */
export function thumb(url: string, size: number): HTMLElement {
  const box = el("span", "h-thumb");
  box.style.setProperty("--size", `${size}px`);
  box.textContent = "♫";
  if (url) {
    const img = new Image();
    img.alt = "";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("load", () => box.replaceChildren(img));
    img.src = url;
  }
  return box;
}

let toastTimer = 0;
/** A brief confirmation near the bottom of the window. */
export function toast(text: string) {
  let t = document.getElementById("app-toast");
  if (!t) {
    t = el("div", "app-toast");
    t.id = "app-toast";
    t.setAttribute("role", "status");
    document.body.append(t);
  }
  t.textContent = text;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => t!.classList.remove("on"), 2200);
}

export async function copyText(text: string, done = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch {
      /* nothing else to try */
    }
    ta.remove();
  }
  toast(done);
}

export const copyName = (r: { artist: string; title: string }) =>
  copyText(`${r.artist} — ${r.title}`.replace(/^ — | — $/g, ""), "Copied");

/** The play that is playing right now: the newest one, for the playing track. */
export function isNow(r: { id: number; track_uri: string; played_at: string }, latestId: number | null, s: Snapshot | null) {
  if (!s?.track || latestId == null || r.id !== latestId || s.track.uri !== r.track_uri) return false;
  const t = Date.parse(r.played_at);
  return Number.isNaN(t) || Date.now() - t < (s.duration_ms || 0) + 600_000;
}

const fmtTs = (ms: number) => {
  const s = Math.floor((ms || 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// ── State ──
let root: HTMLElement;
let q: HTMLInputElement, caption: HTMLElement, listEl: HTMLElement, body: HTMLElement, sheet: HTMLElement;
let snap: Snapshot | null = null;
let data: ListResp | null = null;
let limit = PAGE;
let confirmClear = false;
let loadSeq = 0;
let visible = false;
let openId: number | null = null;
let detail: Detail | null = null;
let nowEls: { row: Row; when: HTMLElement }[] = [];

const query = () => q.value.trim();

// ── List ──
async function load(opts: { toTop?: boolean } = {}) {
  const seq = ++loadSeq;
  try {
    const r = await call<ListResp>("history", "list", { query: query(), limit });
    if (seq !== loadSeq) return;
    data = r;
    caption.textContent = r.caption || " ";
    render(opts.toTop);
  } catch (e) {
    if (seq !== loadSeq) return;
    data = null;
    body.replaceChildren(el("div", "h-empty", `History is unavailable: ${e}`));
  }
}

function updateWhen() {
  for (const { row, when } of nowEls) {
    const now = isNow(row, data?.latest_id ?? null, snap);
    when.textContent = now ? "Now playing" : row.hm;
    when.classList.toggle("now", now);
  }
}

function rowEl(r: Row): HTMLElement {
  const b = el("button", "h-row");
  b.type = "button";
  b.dataset.id = String(r.id);
  const main = el("span", "h-main");
  main.append(el("span", "h-title", r.title || "Unknown"), el("span", "h-artist", r.artist));
  const side = el("span", "h-side");
  const when = el("span", "h-when");
  side.append(when);
  if (r.badge) side.append(el("span", `h-badge ${r.badge.toLowerCase()}`, r.badge));
  b.append(thumb(r.album_art, 40), main, side);
  b.title = `${r.artist} — ${r.title}\n${r.time}`;
  b.addEventListener("click", () => void openSheet(r.id));
  b.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    void copyName(r);
  });
  nowEls.push({ row: r, when });
  return b;
}

function btn(label: string, kind: string, on: () => void): HTMLButtonElement {
  const b = el("button", `h-btn ${kind}`, label);
  b.type = "button";
  b.addEventListener("click", on);
  return b;
}

function render(toTop = false) {
  const d = data;
  if (!d) return;
  const keep = listEl.scrollTop;
  nowEls = [];
  const frag = document.createDocumentFragment();
  if (!d.groups.length) {
    frag.append(el("div", "h-empty", query() ? "No plays match that search." : "Nothing played yet."));
  }
  for (const g of d.groups) {
    const sec = el("section", "h-group");
    const head = el("div", "h-group-head");
    head.append(el("h2", "", g.label), el("span", "h-sum", g.summary));
    const card = el("div", "h-card");
    for (const r of g.entries) card.append(rowEl(r));
    sec.append(head, card);
    frag.append(sec);
  }
  const foot = el("div", "h-foot");
  if (confirmClear) {
    foot.classList.add("confirm");
    foot.append(
      el("div", "h-confirm-title", `Delete all ${d.count.toLocaleString("en-US")} play${d.count !== 1 ? "s" : ""}?`),
      el("div", "h-confirm-body", "Your listening history and stats go with them. This can't be undone."),
    );
    const bar = el("div", "h-btns");
    bar.append(btn("Delete history", "danger", () => void clearHistory()), btn("Cancel", "secondary", () => ask(false)));
    foot.append(bar);
  } else if (d.groups.length || d.more) {
    if (d.more) {
      foot.append(
        btn("Show more", "secondary", () => {
          limit += PAGE;
          void load();
        }),
      );
    }
    if (d.groups.length && !query()) {
      const c = btn("Clear history…", "ghost-danger", () => ask(true));
      c.classList.add("right");
      foot.append(c);
    }
  }
  if (foot.childElementCount) frag.append(foot);
  body.replaceChildren(frag);
  updateWhen();
  listEl.scrollTop = toTop ? 0 : keep;
}

function ask(on: boolean) {
  confirmClear = on;
  render();
  if (on) listEl.scrollTo({ top: listEl.scrollHeight, behavior: "smooth" });
}

async function clearHistory() {
  confirmClear = false;
  closeSheet(false);
  try {
    await call("history", "clear");
  } catch (e) {
    toast(`Could not delete history: ${e}`);
  }
  limit = PAGE;
  await load({ toTop: true });
}

// ── Lyrics sheet ──
let lyEls: HTMLElement[] = [];
let lyActive = -1;
let userScrollAt = 0;
let sheetBody: HTMLElement, sheetHead: HTMLElement, lyScroller: HTMLElement;
let lyQuery: HTMLInputElement | null = null;
let metaEl: HTMLElement | null = null;
let flashTimer = 0;

async function openSheet(id: number) {
  try {
    detail = await call<Detail>("history", "entry", { id });
  } catch (e) {
    toast(`Could not open that play: ${e}`);
    return;
  }
  openId = id;
  buildSheet();
  sheet.classList.add("open");
  sheet.inert = false;
  sheet.setAttribute("aria-hidden", "false");
  sheetHead.querySelector<HTMLElement>(".h-back")?.focus({ preventScroll: true });
  highlightActive();
}

function closeSheet(animate = true): boolean {
  if (openId === null) return false;
  openId = null;
  detail = null;
  lyEls = [];
  lyActive = -1;
  if (!animate) sheet.classList.add("instant");
  sheet.classList.remove("open");
  sheet.inert = true;
  sheet.setAttribute("aria-hidden", "true");
  if (!animate) requestAnimationFrame(() => sheet.classList.remove("instant"));
  return true;
}

function flash(text: string) {
  if (!metaEl || !detail) return;
  const m = metaEl;
  const id = openId;
  m.textContent = text;
  m.classList.add("flash");
  clearTimeout(flashTimer);
  flashTimer = window.setTimeout(() => {
    if (openId === id && detail) {
      m.textContent = detail.meta;
      m.classList.remove("flash");
    }
  }, 2500);
}

function lyricsBody(d: Detail): string {
  return d.synced.length ? d.synced.map((l) => l.words).join("\n") : d.plain.join("\n");
}

function buildSheet() {
  const d = detail!;
  sheetHead.replaceChildren();
  const back = btn("‹ History", "ghost", () => closeSheet());
  back.classList.add("h-back");
  const top = el("div", "h-sheet-top");
  top.append(back);

  const info = el("div", "h-info");
  const art = thumb(d.album_art, 64);
  art.classList.add("big");
  const txt = el("div", "h-info-text");
  txt.append(el("div", "h-sheet-title", d.title || "Unknown"), el("div", "h-sheet-artist", d.artist));
  metaEl = el("div", "h-meta", d.meta);
  txt.append(metaEl);
  info.append(art, txt);

  // Only the actions that can do something for this play.
  const acts = el("div", "h-btns");
  const hasLy = d.synced.length > 0 || d.plain.length > 0;
  if (d.synced.length) acts.append(btn("Export .lrc", "primary", () => void doExport("lrc")));
  if (hasLy) {
    acts.append(btn("Export .txt", d.synced.length ? "secondary" : "primary", () => void doExport("txt")));
    acts.append(
      btn("Copy", "ghost", () => {
        const t = lyricsBody(d);
        if (t) void copyText(t, "Copied lyrics").then(() => flash("Copied lyrics"));
      }),
    );
  }
  if (d.track_uri.startsWith("spotify:")) {
    acts.append(
      btn("Open in Spotify", "ghost", () => {
        call("history", "open_spotify", { uri: d.track_uri }).catch((e) => toast(String(e)));
      }),
    );
  }
  sheetHead.append(top, info);
  if (acts.childElementCount) sheetHead.append(acts);

  lyQuery = null;
  if (hasLy) {
    const f = el("label", "h-field");
    const input = el("input");
    input.type = "search";
    input.placeholder = "Search these lyrics";
    input.spellcheck = false;
    input.autocomplete = "off";
    input.addEventListener("input", markHits);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && input.value) {
        input.value = "";
        markHits();
        e.stopPropagation();
        e.preventDefault();
      }
    });
    f.append(input);
    lyQuery = input;
    sheetHead.append(f);
  }

  sheetBody.replaceChildren();
  const card = el("div", "h-lycard");
  lyEls = [];
  const lines = d.synced.length
    ? d.synced.map((l) => [fmtTs(l.startMs), l.words] as const)
    : d.plain.map((w) => ["", w] as const);
  if (!lines.length) card.append(el("div", "h-nolyrics", "No lyrics for this track."));
  for (const [ts, words] of lines) {
    const row = el("div", "h-lline");
    if (ts) row.append(el("span", "h-ts", ts));
    row.append(el("span", "h-words", words || " "));
    row.dataset.t = (words || "").toLowerCase();
    card.append(row);
    lyEls.push(row);
  }
  sheetBody.append(card);
  lyActive = -1;
  lyScroller.scrollTop = 0;
}

function markHits() {
  const needle = (lyQuery?.value ?? "").trim().toLowerCase();
  let first: HTMLElement | null = null;
  for (const e of lyEls) {
    const hit = !!needle && (e.dataset.t ?? "").includes(needle);
    e.classList.toggle("hit", hit);
    if (hit && !first) first = e;
  }
  if (first) lyScroller.scrollTo({ top: Math.max(0, first.offsetTop - lyScroller.clientHeight / 3), behavior: "smooth" });
}

async function doExport(fmt: "lrc" | "txt") {
  if (openId === null) return;
  try {
    const r = await call<{ message: string }>("history", "export", { id: openId, fmt });
    flash(r.message);
  } catch (e) {
    toast(String(e));
  }
}

/** Mark the line being sung (only when the sheet shows the playing track) and
 *  keep it in view unless the reader scrolled lately. */
function highlightActive() {
  const d = detail;
  if (!d || !lyEls.length || !snap?.track || d.track_uri !== snap.track.uri || !d.synced.length) return;
  const off = Number(snap.extras?.core?.track_offset_ms ?? 0) || 0;
  const pos = position(snap) + off;
  let idx = 0;
  for (let i = 0; i < d.synced.length; i++) {
    if (d.synced[i].startMs <= pos) idx = i;
    else break;
  }
  if (idx >= lyEls.length || idx === lyActive) return;
  lyEls[lyActive]?.classList.remove("active");
  lyEls[idx].classList.add("active");
  lyActive = idx;
  if (Date.now() - userScrollAt > 4000) {
    const e = lyEls[idx];
    const top = lyScroller.scrollTop, view = lyScroller.clientHeight;
    if (!(top + view * 0.2 <= e.offsetTop && e.offsetTop + e.offsetHeight <= top + view * 0.8)) {
      lyScroller.scrollTo({ top: Math.max(0, e.offsetTop - view / 3), behavior: "smooth" });
    }
  }
}

// ── Page plumbing ──
const TEMPLATE = `
  <div class="h-page">
    <header class="h-head">
      <h1>History</h1>
      <div class="h-caption">&nbsp;</div>
      <label class="h-field main"><input type="search" placeholder="Search titles, artists and lyrics" spellcheck="false" autocomplete="off"><kbd>Ctrl F</kbd></label>
    </header>
    <div class="h-list"><div class="h-body"></div></div>
    <section class="h-sheet" aria-hidden="true" inert>
      <div class="h-sheet-head"></div>
      <div class="h-sheet-scroll"><div class="h-sheet-body"></div></div>
    </section>
  </div>`;

let reloadTimer = 0;
function scheduleReload(ms: number) {
  clearTimeout(reloadTimer);
  reloadTimer = window.setTimeout(() => {
    // While a search is showing, new plays wait for it to clear; only the
    // caption (the count) keeps up.
    if (query()) {
      call<ListResp>("history", "list", { query: "", limit: 1 })
        .then((r) => {
          caption.textContent = r.caption;
          if (data) data.latest_id = r.latest_id;
        })
        .catch(() => {});
    } else void load();
  }, ms);
}

/** Filter the History page by `text` (used by Stats' "Recently played"). */
export function openSearch(text: string) {
  closeSheet(false);
  q.value = text;
  limit = PAGE;
  void load({ toTop: true });
}

export function mount(rootEl: HTMLElement) {
  root = rootEl;
  root.innerHTML = TEMPLATE;
  const $ = <T extends HTMLElement>(s: string) => root.querySelector<T>(s)!;
  q = $<HTMLInputElement>(".h-field.main input");
  caption = $(".h-caption");
  listEl = $(".h-list");
  body = $(".h-body");
  sheet = $(".h-sheet");
  sheetHead = $(".h-sheet-head");
  lyScroller = $(".h-sheet-scroll");
  sheetBody = $(".h-sheet-body");

  let t = 0;
  q.addEventListener("input", () => {
    clearTimeout(t);
    t = window.setTimeout(() => {
      limit = PAGE;
      void load({ toTop: true });
    }, 200);
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && q.value) {
      q.value = "";
      limit = PAGE;
      void load({ toTop: true });
      e.stopPropagation();
      e.preventDefault();
    }
  });
  for (const ev of ["wheel", "touchmove", "pointerdown"]) {
    lyScroller.addEventListener(ev, () => (userScrollAt = Date.now()), { passive: true });
  }

  document.addEventListener("keydown", (e) => {
    if (!visible) return;
    if (e.key === "Escape" && closeSheet()) {
      e.preventDefault();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
      e.preventDefault();
      const f = openId !== null ? lyQuery : q;
      f?.focus();
      f?.select();
    }
  });

  onSnapshot((s) => {
    snap = s;
    if (!visible) return;
    updateWhen();
    highlightActive();
  });
  window.setInterval(() => {
    if (visible && openId !== null) highlightActive();
  }, 500);

  // A play was committed (or history cleared). Off screen, show() catches up.
  void listen("history_changed", () => {
    if (visible) scheduleReload(150);
  });
  void load();
}

export function show() {
  visible = true;
  scheduleReload(0);
}

export function hide() {
  visible = false;
  if (document.activeElement instanceof HTMLElement && root.contains(document.activeElement)) {
    document.activeElement.blur();
  }
}
