// Now Playing / lyric sheet page (owner: nowplaying agent).
import { onSnapshot, player, position, seek, type Snapshot } from "../api";

const TEMPLATE = `
  <header class="np-head">
    <img id="cover" alt="" />
    <div class="meta">
      <div id="title">Waiting for Spotify…</div>
      <div id="artist"></div>
      <div id="source"></div>
    </div>
  </header>
  <section id="lyrics" aria-live="polite"><div id="sheet"></div></section>
  <div id="progress"><div id="bar"></div></div>
  <footer class="np-foot">
    <div class="transport">
      <button data-act="prev" title="Previous">⏮</button>
      <button data-act="toggle" title="Play / pause">⏯</button>
      <button data-act="next" title="Next">⏭</button>
    </div>
    <div class="status">
      <span id="st-bridge" class="pill">Spotify</span>
      <span id="st-discord" class="pill">Discord</span>
    </div>
  </footer>
  <div id="note"></div>`;

let root: HTMLElement;
const $ = (id: string) => root.querySelector<HTMLElement>(`#${id}`)!;
let snap: Snapshot | null = null;
let sheetKey = "";
let lineEls: HTMLElement[] = [];
let cur = -2;

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
    const bd = document.getElementById("backdrop")!;
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

export function mount(el: HTMLElement) {
  root = el;
  root.innerHTML = TEMPLATE;
  root.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) =>
    b.addEventListener("click", () => player(b.dataset.act!)));
  $("progress").addEventListener("click", (ev) => {
    if (!snap?.duration_ms) return;
    const r = $("progress").getBoundingClientRect();
    seek(Math.round(((ev.clientX - r.left) / r.width) * snap.duration_ms));
  });
  onSnapshot(render);
  requestAnimationFrame(tick);
}
