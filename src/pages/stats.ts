// Stats page (owner: history agent): listening overview, activity heatmap, a
// monthly Wrapped card (exportable as an image), top songs and recently
// played. Port of statusify_ui_stats.py; numbers come from the Rust "history"
// feature. It queries only while on screen (at most every 15 s, sooner after
// a new play), never on a timer in the background.
/// <reference types="vite/client" />
import { listen } from "@tauri-apps/api/event";
import { call, onSnapshot, type Snapshot } from "../api";
import { copyName, el, isNow, openSearch, thumb, toast } from "./history";
import { buildHeat, fmtHm, fmtSession, heatTip, heatWeeks, parseDay, plural, type Heat } from "./stats_util";
import { canvasToBlob, canvasToPngBase64, renderWrapped, type Summary } from "./stats_wrapped";
import "./history.css";
import "./stats.css";

const RECENT_PAGE = 30; // "Recently played" rows per page ("Show more" adds this many)
const VISIBLE_REFRESH_MS = 15_000; // while the page is open, re-query at most this often

type Artists = [string, number][];
type Period = { plays: number; listened_ms: number; top_artists: Artists };
type Top = { title: string; artist: string; album_art: string; plays: number; listened_ms: number };
type Recent = {
  id: number;
  track_uri: string;
  artist: string;
  title: string;
  album_art: string;
  played_at: string;
  when: string;
  dur: string;
};
type Data = {
  off: boolean;
  today: string;
  first: string | null;
  week: Period;
  all: Period;
  recent: Recent[];
  recent_n: number;
  heat: Record<string, number>;
  month: Summary;
  top: { all: Top[]; "30": Top[] };
  latest: { id: number; track_uri: string; played_at: string } | null;
  current_id: number;
};

let root: HTMLElement;
let snap: Snapshot | null = null;
let data: Data | null = null;
let lastJson = "";
let visible = false;
let busy = false;
let dirty = true;
let fetchedAt = 0;
let drawnAt = 0;
let recentN = RECENT_PAGE;
let topMode: "all" | "30" = "all";
let ym: [number, number] = (() => {
  const t = new Date();
  return [t.getFullYear(), t.getMonth() + 1];
})();
let session = { songs: 0, listened_ms: 0 };
let error = "";

// Live bits of the current render.
let sessSongs: HTMLElement | null = null;
let sessTime: HTMLElement | null = null;
let heatHost: HTMLElement | null = null;
let heatCounts: Record<string, number> = {};
let heatToday = "";
let flashEl: HTMLElement | null = null;
let flashTimer = 0;
let nowEls: { play: Recent; el: HTMLElement }[] = [];
let wrappedBusy = false;

const monthKey = (y: number, m: number) => y * 12 + (m - 1);

// ── Fetching ──
async function refresh(force = false) {
  if (!visible && !force) return;
  if (busy) return;
  if (!force && !dirty && Date.now() - fetchedAt < VISIBLE_REFRESH_MS) return;
  busy = true;
  try {
    const r = await call<Data>("history", "stats", { year: ym[0], month: ym[1], recent_n: recentN });
    error = "";
    const json = JSON.stringify(r);
    const same = json === lastJson;
    data = r;
    lastJson = json;
    dirty = false;
    fetchedAt = Date.now();
    // The periodic refresh usually finds nothing new; redraw then only once a
    // minute, so "3 min ago" keeps up without needless redraws.
    if (!same || Date.now() - drawnAt > 60_000) render();
  } catch (e) {
    error = String(e);
    render();
  } finally {
    busy = false;
  }
}

async function pollSession() {
  try {
    session = await call("history", "session");
    paintSession();
  } catch {
    /* the next tick tries again */
  }
}

function paintSession() {
  if (sessSongs) sessSongs.textContent = String(session.songs);
  if (sessTime) sessTime.textContent = fmtSession(session.listened_ms / 1000);
}

// ── Building blocks ──
function section(title: string, sub?: string): [HTMLElement, HTMLElement] {
  const s = el("section", "st-sec");
  const h = el("div", "st-sec-head");
  h.append(el("h2", "", title));
  if (sub) h.append(el("p", "", sub));
  const card = el("div", "st-card");
  s.append(h, card);
  return [s, card];
}

function btn(label: string, kind: string, on: () => void, enabled = true): HTMLButtonElement {
  const b = el("button", `h-btn ${kind}`, label);
  b.type = "button";
  b.disabled = !enabled;
  b.addEventListener("click", on);
  return b;
}

function tile(value: string, caption: string): [HTMLElement, HTMLElement] {
  const t = el("div", "st-tile");
  const v = el("div", "st-big", value);
  t.append(v, el("div", "st-cap", caption));
  return [t, v];
}

function artistsList(title: string, tops: Artists): HTMLElement {
  const col = el("div", "st-artists");
  col.append(el("div", "st-sub", title));
  if (!tops.length) {
    col.append(el("div", "st-none", "Nothing yet"));
    return col;
  }
  const most = Math.max(...tops.map(([, n]) => n)) || 1;
  tops.forEach(([name, n], i) => {
    const row = el("div", "st-art");
    const line = el("div", "st-art-line");
    line.append(el("span", i === 0 ? "st-art-name first" : "st-art-name", `${i + 1}. ${name}`), el("span", "st-art-n", String(n)));
    const bar = el("div", "st-bar");
    const fill = el("i");
    fill.style.width = `${Math.max(2, (100 * n) / most)}%`;
    bar.append(fill);
    row.append(line, bar);
    col.append(row);
  });
  return col;
}

// ── Overview ──
function overviewCard(card: HTMLElement) {
  const g = el("div", "st-tiles");
  const [t1, v1] = tile(String(session.songs), "songs this session");
  const [t2, v2] = tile(fmtSession(session.listened_ms / 1000), "listened this session");
  sessSongs = v1;
  sessTime = v2;
  g.append(t1, t2);
  card.append(g);
  const d = data;
  if (d?.off) {
    card.append(el("hr", "st-hr"), el("p", "st-off-note", 'History is off. Turn on "Remember history" for long-term stats.'));
    return;
  }
  if (!d) return;
  card.append(el("hr", "st-hr"));
  const g2 = el("div", "st-tiles");
  g2.append(
    tile(d.week.plays.toLocaleString("en-US"), `plays in the last 7 days · ${fmtHm(d.week.listened_ms)}`)[0],
    tile(d.all.plays.toLocaleString("en-US"), `plays all time · ${fmtHm(d.all.listened_ms)}`)[0],
  );
  card.append(g2);
  if (d.week.top_artists.length || d.all.top_artists.length) {
    card.append(el("hr", "st-hr"));
    const g3 = el("div", "st-tiles");
    g3.append(artistsList("Top artists · 7 days", d.week.top_artists), artistsList("Top artists · all time", d.all.top_artists));
    card.append(g3);
  }
}

// ── Heatmap ──
const CELL = 12;
const GAP = 3;
const STEP = CELL + GAP;
const LABEL_W = 34;
let measureCtx: CanvasRenderingContext2D | null = null;
const measureLabel = (s: string) => {
  measureCtx ??= document.createElement("canvas").getContext("2d")!;
  measureCtx.font = '11px "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif';
  return measureCtx.measureText(s).width;
};

function drawHeat() {
  const host = heatHost;
  if (!host || !host.isConnected) return;
  const width = host.clientWidth;
  if (width < 120) return;
  const weeks = heatWeeks(width, LABEL_W, STEP, GAP);
  const heat: Heat = buildHeat(heatToday, weeks, heatCounts, STEP, width - LABEL_W, measureLabel);

  const head = el("div", "st-heat-head");
  const lead = el("div", "st-heat-lead");
  lead.append(el("b", "", `${heat.total.toLocaleString("en-US")} play${heat.total !== 1 ? "s" : ""} in the last ${weeks} weeks`), el("span", "", `· ${plural(heat.active, "active day")}`));
  const legend = el("div", "st-legend");
  legend.append(el("span", "", "Less"));
  for (let l = 0; l <= 4; l++) legend.append(el("i", `st-hc l${l}`));
  legend.append(el("span", "", "More"));
  head.append(lead, legend);

  const wrap = el("div", "st-heat-wrap");
  wrap.style.paddingLeft = `${LABEL_W}px`;
  const months = el("div", "st-heat-months");
  for (const m of heat.months) {
    const s = el("span", "", m.label);
    s.style.left = `${m.col * STEP}px`;
    months.append(s);
  }
  const days = el("div", "st-heat-days");
  for (const [r, name] of [[0, "Mon"], [2, "Wed"], [4, "Fri"]] as const) {
    const s = el("span", "", name);
    s.style.top = `${r * STEP}px`;
    days.append(s);
  }
  const grid = el("div", "st-heat-grid");
  grid.style.setProperty("--weeks", String(weeks));
  const tip = el("div", "st-tip");
  for (const c of heat.cells) {
    const cell = el("i", `st-hc l${c.level}`);
    cell.style.gridColumn = String(c.col + 1);
    cell.style.gridRow = String(c.row + 1);
    cell.dataset.tip = heatTip(parseDay(c.date), c.n);
    grid.append(cell);
  }
  grid.addEventListener("pointerover", (e) => {
    const c = (e.target as HTMLElement).closest<HTMLElement>(".st-hc");
    if (!c?.dataset.tip) return;
    tip.textContent = c.dataset.tip;
    const hr = c.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
    tip.classList.add("on");
    const tw = tip.offsetWidth;
    const x = Math.max(0, Math.min(wr.width - tw, hr.left - wr.left + hr.width / 2 - tw / 2));
    tip.style.left = `${x}px`;
    tip.style.top = `${hr.top - wr.top - tip.offsetHeight - 6}px`;
  });
  grid.addEventListener("pointerleave", () => tip.classList.remove("on"));
  wrap.append(months, days, grid, tip);
  host.replaceChildren(head, wrap);
}

function heatCard(card: HTMLElement) {
  heatHost = el("div", "st-heat");
  card.append(heatHost);
  heatCounts = data!.heat;
  heatToday = data!.today;
}

// ── Wrapped ──
function flash(text: string) {
  if (!flashEl) return;
  flashEl.textContent = text;
  clearTimeout(flashTimer);
  const f = flashEl;
  flashTimer = window.setTimeout(() => {
    if (f.isConnected) f.textContent = "";
  }, 2600);
}

function monthBounds(): [number, number] {
  const d = data!;
  const t = parseDay(d.today);
  const f = d.first ? parseDay(d.first) : t;
  return [monthKey(f.getFullYear(), f.getMonth() + 1), monthKey(t.getFullYear(), t.getMonth() + 1)];
}

async function stepMonth(delta: number) {
  if (!data) return;
  let y = ym[0], m = ym[1] + delta;
  y += Math.floor((m - 1) / 12);
  m = ((m - 1 + 12) % 12) + 1;
  const [lo, hi] = monthBounds();
  if (monthKey(y, m) < lo || monthKey(y, m) > hi) return;
  ym = [y, m];
  try {
    const res = await call<Summary>("history", "month", { year: y, month: m });
    if (!data || ym[0] !== y || ym[1] !== m) return;
    data.month = res;
    lastJson = JSON.stringify(data);
    render();
  } catch (e) {
    toast(String(e));
  }
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const i = new Image();
    i.onload = () => res(i);
    i.onerror = () => rej(new Error("image failed"));
    i.src = src;
  });
}

/** Render the Wrapped card (fetching the top song's cover through the backend). */
async function makeWrapped(): Promise<HTMLCanvasElement | null> {
  const s = data?.month;
  if (!s || !s.plays) return null;
  let art: HTMLImageElement | null = null;
  const url = s.top_song?.album_art;
  if (url) {
    try {
      art = await loadImage(await call<string>("history", "fetch_art", { url }));
    } catch {
      /* the card draws a placeholder cover */
    }
  }
  try {
    await document.fonts?.ready;
  } catch {
    /* fonts are best effort */
  }
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1ed760";
  return renderWrapped(s, accent, art);
}

/** Save: ask where first (as Python did), then render and write, so nothing
 *  is rendered for a cancelled dialog and the path is the user's choice. */
async function saveWrapped() {
  const s = data?.month;
  if (wrappedBusy || !s?.plays) return;
  wrappedBusy = true;
  try {
    const pick = await call<{ path?: string; cancelled?: boolean }>("history", "pick_wrapped_path", {
      year: s.year,
      month: s.month,
    });
    if (pick.cancelled) return;
    flash("Rendering…");
    const c = await makeWrapped();
    if (!c) return;
    const r = await call<{ path?: string; cancelled?: boolean }>("history", "save_wrapped", {
      png: await canvasToPngBase64(c),
      year: s.year,
      month: s.month,
    });
    if (r.cancelled) return;
    flash("Saved");
    if (r.path) toast(`Saved to ${r.path}`);
  } catch (e) {
    console.error("Could not save Wrapped image", e);
    flash("Couldn't save the image");
  } finally {
    wrappedBusy = false;
  }
}

/** Copy: the ClipboardItem is built synchronously inside the click with a
 *  Promise<Blob>, so a slow cover fetch can't outlive the user gesture. */
async function copyWrapped() {
  if (wrappedBusy || !data?.month?.plays) return;
  wrappedBusy = true;
  try {
    const blob = makeWrapped().then((c) => {
      if (!c) throw new Error("nothing to render");
      return canvasToBlob(c);
    });
    flash("Rendering…");
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    flash("Copied");
  } catch (e) {
    console.error("Could not copy Wrapped image", e);
    flash("Couldn't copy the image");
  } finally {
    wrappedBusy = false;
  }
}

function wrappedCard(card: HTMLElement) {
  const d = data!;
  const s = d.month;
  const [lo, hi] = monthBounds();
  const k = monthKey(ym[0], ym[1]);
  const narrow = root.clientWidth < 470;

  const head = el("div", "st-wr-head");
  const nav = el("div", "st-wr-nav");
  nav.append(btn("‹", "ghost", () => void stepMonth(-1), k > lo), el("span", "st-wr-month", s.label), btn("›", "ghost", () => void stepMonth(1), k < hi));
  head.append(nav);
  if (s.plays) {
    const acts = el("div", "st-wr-acts");
    flashEl = el("span", "st-flash");
    acts.append(flashEl);
    if (!narrow) acts.append(btn("Copy image", "secondary", () => void copyWrapped()));
    acts.append(btn("Save as image", "primary", () => void saveWrapped()));
    head.append(acts);
  }
  card.append(head);
  if (!s.plays) {
    card.append(el("div", "st-none pad", `Nothing played in ${s.label}.`));
    return;
  }
  const h = s.busiest_hour;
  const song = s.top_song;
  const cells: [string, string, string][] = [
    ["Top song", song ? song.title : "—", song ? `${song.artist} · ${song.plays} plays` : ""],
    ["Top artist", s.top_artist ? s.top_artist[0] : "—", s.top_artist ? `${s.top_artist[1]} plays` : ""],
    ["Listening time", fmtHm(s.listened_ms), `across ${s.active_days} days`],
    ["Plays", s.plays.toLocaleString("en-US"), `${s.artists} different artists`],
    ["Busiest hour", h ? `${String(h[0]).padStart(2, "0")}:00–${String((h[0] + 1) % 24).padStart(2, "0")}:00` : "—", h ? `${h[1]} plays in that hour` : ""],
    ["Longest streak", plural(s.longest_streak, "day"), "in a row with music"],
  ];
  const grid = el("div", "st-wr-grid");
  cells.forEach(([label, value, cap], i) => {
    const c = el("div", "st-wr-cell");
    const text = el("div", "st-wr-text");
    text.append(el("div", "st-wr-label", label), el("div", "st-wr-value", value), el("div", "st-wr-cap", cap));
    text.title = value;
    if (i === 0 && song) {
      const withArt = el("div", "st-wr-with-art");
      withArt.append(thumb(song.album_art, 44), text);
      c.append(withArt);
    } else c.append(text);
    grid.append(c);
  });
  card.append(grid);
}

// ── Top songs ──
function topCard(card: HTMLElement) {
  const d = data!;
  const head = el("div", "st-top-head");
  head.append(el("span", "st-sub", "Most played"));
  const seg = el("div", "st-seg");
  for (const [label, key] of [["All time", "all"], ["30 days", "30"]] as const) {
    const b = el("button", key === topMode ? "on" : "", label);
    b.type = "button";
    b.addEventListener("click", () => {
      if (topMode !== key) {
        topMode = key;
        render();
      }
    });
    seg.append(b);
  }
  head.append(seg);
  card.append(head);
  const tops = d.top[topMode] ?? [];
  if (!tops.length) {
    card.append(el("div", "st-none pad", "No plays in the last 30 days."));
    return;
  }
  const most = Math.max(...tops.map((t) => t.plays)) || 1;
  const list = el("ol", "st-top");
  tops.forEach((t, i) => {
    const li = el("li", "st-top-row");
    li.append(el("span", i === 0 ? "st-rank first" : "st-rank", String(i + 1)), thumb(t.album_art, 34));
    const main = el("div", "st-top-main");
    main.append(el("div", "st-row-title", t.title), el("div", "st-row-artist", t.artist));
    const side = el("div", "st-top-side");
    const bar = el("div", "st-bar");
    const fill = el("i");
    fill.style.width = `${Math.max(2, (100 * t.plays) / most)}%`;
    bar.append(fill);
    side.append(el("div", "st-count", plural(t.plays, "play")), bar);
    li.append(main, side);
    list.append(li);
  });
  card.append(list);
}

// ── Recently played ──
function goTab(id: string) {
  document.querySelector<HTMLElement>(`.tab[data-page="${id}"]`)?.click();
}

function updateNow() {
  const current = data?.current_id ?? 0;
  for (const { play, el: e } of nowEls) {
    const now = isNow(play, current);
    e.textContent = now ? "Now playing" : play.dur;
    e.classList.toggle("now", now);
  }
}

async function showMore() {
  recentN += RECENT_PAGE;
  try {
    const r = await call<{ recent: Recent[]; recent_n: number }>("history", "recent", { limit: recentN });
    if (!data) return;
    data.recent = r.recent;
    data.recent_n = r.recent_n;
    lastJson = JSON.stringify(data);
    render();
  } catch (e) {
    toast(String(e));
  }
}

function recentCard(card: HTMLElement) {
  const d = data!;
  nowEls = [];
  const list = el("div", "st-recent");
  for (const p of d.recent) {
    const row = el("button", "st-rrow");
    row.type = "button";
    const main = el("span", "st-rmain");
    main.append(el("span", "st-row-title", p.title || "Unknown"), el("span", "st-row-artist", p.artist));
    const side = el("span", "st-rside");
    const dur = el("span", "st-rdur", p.dur);
    side.append(el("span", "st-rwhen", p.when), dur);
    row.append(thumb(p.album_art, 40), main, side);
    row.addEventListener("click", () => {
      goTab("history");
      openSearch(p.title || p.artist || "");
    });
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      void copyName(p);
    });
    nowEls.push({ play: p, el: dur });
    list.append(row);
  }
  card.append(list);
  if (d.recent.length >= d.recent_n) {
    const more = el("div", "st-more");
    more.append(btn("Show more", "secondary", () => void showMore()));
    card.append(more);
  }
  updateNow();
}

// ── Page ──
function emptyCard(card: HTMLElement, title: string, body: string, action?: [string, () => void, string?]) {
  const e = el("div", "st-empty");
  e.append(el("div", "st-empty-note", "♫"), el("div", "st-empty-title", title), el("div", "st-empty-body", body));
  if (action) e.append(btn(action[0], action[2] ?? "secondary", action[1]));
  card.append(e);
}

function render() {
  drawnAt = Date.now();
  sessSongs = sessTime = heatHost = flashEl = null;
  nowEls = [];
  const frag = document.createDocumentFragment();
  const wrap = el("div", "st-wrap");
  const title = el("div", "st-title");
  title.append(el("h1", "", "Stats"), el("p", "", "Your listening, at a glance."));
  wrap.append(title);

  const [o, oc] = section("Overview");
  overviewCard(oc);
  wrap.append(o);

  const d = data;
  if (error && !d) {
    const [s, c] = section("Your history");
    emptyCard(c, "Stats are unavailable", error);
    wrap.append(s);
  } else if (d?.off) {
    const [s, c] = section("Your history");
    emptyCard(c, "History is off", 'Turn on "Remember history" in Settings and your listening stats will build up here.', [
      "Open Settings",
      () => goTab("settings"),
      "primary",
    ]);
    wrap.append(s);
  } else if (d && !d.all.plays) {
    const [s, c] = section("Your history");
    emptyCard(c, "Nothing here yet", "Play something and your stats will appear here: an activity map, your top songs and a monthly Wrapped.");
    wrap.append(s);
  } else if (d) {
    const [a, ac] = section("Activity", "Plays per day. Hover a day for details.");
    heatCard(ac);
    const [w, wc] = section("Wrapped", "Your month in numbers. Save it as an image to share.");
    wrappedCard(wc);
    const [t, tc] = section("Top songs");
    topCard(tc);
    const [r, rc] = section("Recently played", "Click a song to find it in History. Right-click to copy.");
    recentCard(rc);
    wrap.append(a, w, t, r);
  }
  frag.append(wrap);
  root.replaceChildren(frag);
  drawHeat();
}

export function mount(rootEl: HTMLElement) {
  root = rootEl;
  root.classList.add("st-root");
  render();
  onSnapshot((s) => {
    const changed = s.track?.uri !== snap?.track?.uri;
    snap = s;
    // The old play stops being "Now playing" as soon as the track changes.
    if (visible && changed) {
      dirty = true;
      window.setTimeout(() => void refresh(true), 400);
    }
  });
  // A play was committed (or history cleared): refresh now if the page is
  // open, otherwise remember that the next visit needs fresh data.
  let t = 0;
  void listen("history_changed", () => {
    dirty = true;
    if (visible) {
      clearTimeout(t);
      t = window.setTimeout(() => void refresh(true), 800);
    }
  });
  let rt = 0;
  new ResizeObserver(() => {
    clearTimeout(rt);
    rt = window.setTimeout(() => {
      if (visible) drawHeat();
    }, 80);
  }).observe(root);
  window.setInterval(() => {
    if (!visible) return;
    void pollSession();
    void refresh();
  }, 5000);
}

export function show() {
  visible = true;
  void pollSession();
  void refresh();
  requestAnimationFrame(drawHeat);
}

export function hide() {
  visible = false;
}
