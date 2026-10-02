import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type Line = { startMs: number; words: string };
type Snapshot = {
  track: { uri: string; artist: string; title: string; album: string; album_art: string } | null;
  position_ms: number; position_at_ms: number; duration_ms: number; is_playing: boolean;
  lyrics: { mode: string; synced: Line[]; plain: string[]; source: string };
  bridge_connected: boolean; discord_user: string | null; note: string;
};
type Play = { artist: string; title: string; played_at: string };

const $ = (id: string) => document.getElementById(id)!;
let snap: Snapshot | null = null;
let sheetKey = "";
let lineEls: HTMLElement[] = [];
let cur = -2;

function position(s: Snapshot): number {
  const p = s.position_ms + (s.is_playing ? Date.now() - s.position_at_ms : 0);
  return s.duration_ms > 0 ? Math.min(p, s.duration_ms) : p;
}

function renderSheet(s: Snapshot) {
  const n = s.lyrics.synced.length || s.lyrics.plain.length;
  const key = `${s.track?.uri}|${s.lyrics.source}|${s.lyrics.mode}|${n}`;
  if (key === sheetKey) return;
  sheetKey = key;
  const sheet = $("sheet");
  sheet.replaceChildren();
  sheet.style.transform = "translateY(0)";
  sheet.className = s.lyrics.mode === "plain" ? "plain" : "";
  cur = -2;
  const lines = s.lyrics.mode === "synced" ? s.lyrics.synced.map((l) => l.words)
    : s.lyrics.mode === "plain" ? s.lyrics.plain : [];
  if (!lines.length) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = s.track ? "Looking for lyrics…" : "";
    sheet.append(e);
  }
  lineEls = lines.map((w) => {
    const e = document.createElement("div");
    e.className = "line" + (w.trim() ? "" : " blank");
    e.textContent = w;
    sheet.append(e);
    return e;
  });
}

function render(s: Snapshot) {
  snap = s;
  const t = s.track;
  $("title").textContent = t ? t.title : "Waiting for Spotify…";
  $("artist").textContent = t ? t.artist : "";
  $("source").textContent = s.lyrics.mode === "none" ? "" : `Lyrics · ${s.lyrics.source}`;
  const cover = $("cover") as HTMLImageElement;
  const art = t?.album_art ?? "";
  if (cover.getAttribute("src") !== art) {
    cover.src = art;
    const bd = $("backdrop");
    bd.style.backgroundImage = art ? `url("${art}")` : "";
    bd.classList.toggle("on", !!art);
  }
  $("st-bridge").classList.toggle("ok", s.bridge_connected);
  $("st-discord").classList.toggle("ok", !!s.discord_user);
  $("st-discord").title = s.discord_user ? `Connected as ${s.discord_user}` : "Not connected";
  $("note").textContent = s.note;
  renderSheet(s);
}

// Every frame: move the highlight and the progress bar from the extrapolated
// position. Only transforms and colours change, so it stays on the compositor.
function tick() {
  requestAnimationFrame(tick);
  const s = snap;
  if (!s) return;
  const pos = position(s);
  $("bar").style.width = s.duration_ms ? `${(pos / s.duration_ms) * 100}%` : "0";
  if (s.lyrics.mode !== "synced" || !lineEls.length) return;
  const L = s.lyrics.synced;
  let lo = 0, hi = L.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (L[m].startMs <= pos) lo = m + 1; else hi = m; }
  const i = lo - 1;
  if (i === cur) return;
  cur = i;
  lineEls.forEach((e, k) => {
    e.classList.toggle("now", k === i);
    e.classList.toggle("past", k < i);
  });
  const box = $("lyrics");
  const target = lineEls[Math.max(i, 0)];
  const y = target.offsetTop - box.clientHeight * 0.38 + target.offsetHeight / 2;
  $("sheet").style.transform = `translateY(${-Math.max(0, y)}px)`;
}

async function loadPlays() {
  const plays = await invoke<Play[]>("recent_plays", { limit: 30 });
  $("plays").replaceChildren(...plays.map((p) => {
    const li = document.createElement("li");
    li.textContent = `${p.artist} — ${p.title}`;
    const tm = document.createElement("time");
    tm.textContent = p.played_at.replace("T", " ").slice(5, 16);
    li.append(tm);
    return li;
  }));
}

document.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) =>
  b.addEventListener("click", () => invoke("player", { action: b.dataset.act })));
$("progress").addEventListener("click", (ev) => {
  if (!snap?.duration_ms) return;
  const r = $("progress").getBoundingClientRect();
  invoke("seek", { positionMs: Math.round(((ev.clientX - r.left) / r.width) * snap.duration_ms) });
});
$("history").addEventListener("toggle", () => {
  if (($("history") as HTMLDetailsElement).open) loadPlays();
});

listen<Snapshot>("snapshot", (e) => render(e.payload));
invoke<Snapshot>("snapshot").then(render);
requestAnimationFrame(tick);
