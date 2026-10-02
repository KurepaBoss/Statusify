// Glass panels over the lyric sheet (Up next, lyric search), the context menu
// and the toast. Ported from statusify_np_extras.py. The Python version drew
// all of this into the frame with PIL; here they are plain elements with
// backdrop-filter (the frosted card) that slide and fade on the compositor.

import { fmtTime } from "./now_logic";
import { icon } from "./now_icons";
import type { call as Call } from "../api";

export type PanelTrack = { uri: string; artist: string; title: string; album_art: string };
export type QueueItem = { uri: string; uid: string; title: string; artist: string; album_art: string; duration_ms: number };
type SearchResult = { track: string; artist: string; album: string; duration: number; synced: boolean };
export type MenuItem = { id: string; label: string; enabled: boolean; checked?: boolean } | null;

const QUEUE_ROWS = 8;

export type PanelHost = {
  root: HTMLElement;
  call: typeof Call;
  track: () => PanelTrack | null;
  isPinned: () => boolean;
  toast: (text: string, frac?: number | null) => void;
  error: (msg: string) => void;
  onMenu: (id: string) => void;
};

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

export class Panels {
  private panel: HTMLElement;
  private menuEl: HTMLElement;
  private kind: "queue" | "search" | null = null;
  private queue: QueueItem[] = [];
  private search = { q: "", status: "", results: [] as SearchResult[], gen: 0 };
  private pinned = false;
  private closing = 0;
  private menuItems: MenuItem[] = [];

  constructor(private host: PanelHost) {
    this.panel = document.createElement("aside");
    this.panel.className = "np-panel glass";
    this.panel.addEventListener("click", (e) => this.onPanelClick(e));
    this.panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        this.close();
      }
    });
    this.menuEl = document.createElement("div");
    this.menuEl.className = "np-menu glass";
    this.menuEl.addEventListener("click", (e) => {
      const it = (e.target as HTMLElement).closest<HTMLElement>(".mi");
      if (!it || it.classList.contains("dis")) return;
      const id = it.dataset.id!;
      this.closeMenu();
      host.onMenu(id);
    });
    host.root.append(this.panel, this.menuEl);
    // A click anywhere else closes the menu (and a panel, when the sheet is clicked).
    document.addEventListener("pointerdown", (e) => {
      const t = e.target as Node;
      if (this.menuOpen && !this.menuEl.contains(t)) {
        // A click outside a menu only closes it (the click that follows is swallowed).
        this.closeMenu();
        if (e.button === 0) {
          const swallow = (ev: Event) => { ev.stopPropagation(); ev.preventDefault(); };
          window.addEventListener("click", swallow, { capture: true, once: true });
          window.setTimeout(() => window.removeEventListener("click", swallow, true), 400);
        }
      }
    }, true);
    // A click on the sheet while a panel is open only closes the panel (capture: before the row's seek).
    host.root.querySelector(".sheet")?.addEventListener("click", (e) => {
      if (!this.kind) return;
      e.stopPropagation();
      this.close();
    }, true);
  }

  // ── Panels ──────────────────────────────────────────────────

  get open(): "queue" | "search" | null {
    return this.kind;
  }

  toggleQueue() {
    if (this.kind === "queue") this.close();
    else this.show("queue");
  }

  openSearch() {
    this.show("search");
  }

  private show(kind: "queue" | "search") {
    if (this.kind === kind) return;
    this.closeMenu();
    window.clearTimeout(this.closing);
    this.kind = kind;
    if (kind === "search") {
      const t = this.host.track();
      const q = t ? `${t.artist} ${t.title}`.trim() : "";
      this.search = { q, status: "", results: [], gen: this.search.gen };
      this.render();
      this.panel.classList.add("open");
      const inp = this.panel.querySelector<HTMLInputElement>("input");
      inp?.focus();
      inp?.setSelectionRange(inp.value.length, inp.value.length);
      void this.runSearch(q);
    } else {
      this.render();
      this.panel.classList.add("open");
    }
    this.host.root.classList.add("panel-open");
    this.host.root.dispatchEvent(new CustomEvent("np-panel"));
  }

  close(): boolean {
    if (!this.kind) return false;
    this.kind = null;
    this.search.gen++;
    this.panel.classList.remove("open");
    this.host.root.classList.remove("panel-open");
    (document.activeElement as HTMLElement | null)?.blur?.();
    // Keep the contents while it slides away.
    this.closing = window.setTimeout(() => { if (!this.kind) this.panel.replaceChildren(); }, 300);
    this.host.root.dispatchEvent(new CustomEvent("np-panel"));
    return true;
  }

  setQueue(q: QueueItem[]) {
    this.queue = q;
    if (this.kind === "queue") this.render();
  }

  setPinned(p: boolean) {
    if (p === this.pinned) return;
    this.pinned = p;
    if (this.kind === "search") this.renderResults();
  }

  private head(title: string): string {
    return `<div class="ph"><div class="pt">${esc(title)}</div><button class="ib sm" data-act="close" title="Close">${icon("close")}</button></div>`;
  }

  private render() {
    if (this.kind === "queue") {
      const q = this.queue.slice(0, QUEUE_ROWS);
      const rows = q.length
        ? q.map((t, i) => `<button class="qrow" data-i="${i}">
            ${t.album_art ? `<img class="qc" src="${esc(t.album_art)}" alt="" loading="lazy" decoding="async">` : `<div class="qc ph0"></div>`}
            <div class="qt"><div class="q1">${esc(t.title || "Unknown")}</div><div class="q2">${esc(t.artist)}</div></div>
            ${t.duration_ms ? `<span class="qd">${fmtTime(t.duration_ms)}</span>` : ""}</button>`).join("")
        : `<div class="pempty"><div class="pe1">Nothing queued</div><div class="pe2">Songs you queue in Spotify show up here</div></div>`;
      this.panel.innerHTML = this.head("Up next") + `<div class="plist">${rows}</div>`;
    } else if (this.kind === "search") {
      this.panel.innerHTML = this.head("Find lyrics") +
        `<label class="sfield"><span class="si">${icon("search")}</span><input type="text" spellcheck="false" autocomplete="off" value="${esc(this.search.q)}" placeholder="Artist and song title"></label>
         <div class="sstat"></div><div class="plist res"></div><div class="pfoot"></div>`;
      const inp = this.panel.querySelector("input")!;
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          void this.runSearch(inp.value);
        }
      });
      this.renderResults();
    }
  }

  private renderResults() {
    const s = this.search;
    const stat = this.panel.querySelector<HTMLElement>(".sstat");
    const list = this.panel.querySelector<HTMLElement>(".res");
    const foot = this.panel.querySelector<HTMLElement>(".pfoot");
    if (!stat || !list || !foot) return;
    stat.textContent = s.status;
    list.innerHTML = s.results.map((r, i) => {
      const sub = [r.artist, r.album].filter(Boolean).join(" · ");
      return `<button class="qrow sres" data-i="${i}"><div class="qt"><div class="q1">${esc(r.track || "Untitled")}</div><div class="q2">${esc(sub)}</div></div>
        <div class="sr"><span class="badge ${r.synced ? "syn" : ""}">${r.synced ? "Synced" : "Plain"}</span>${r.duration ? `<span class="qd">${fmtTime(r.duration * 1000)}</span>` : ""}</div></button>`;
    }).join("");
    foot.innerHTML = this.pinned ? `<button class="chip" data-act="unpin">Use Spotify's lyrics again</button>` : "";
  }

  private async runSearch(text: string) {
    const s = this.search;
    const gen = ++s.gen;
    s.q = text.trim();
    if (!s.q) {
      s.status = "Type an artist and a song title";
      s.results = [];
      this.renderResults();
      return;
    }
    s.status = "Searching LRCLIB…";
    s.results = [];
    this.renderResults();
    try {
      const r = await this.host.call<{ results?: SearchResult[]; status?: string; stale?: boolean }>("nowplaying", "lyric_search", { query: s.q });
      if (gen !== s.gen || r.stale) return;
      s.results = r.results ?? [];
      s.status = r.status ?? "";
    } catch {
      if (gen !== s.gen) return;
      s.status = "Search failed. Check your connection and try again";
      s.results = [];
    }
    this.renderResults();
  }

  private async onPanelClick(e: MouseEvent) {
    const t = e.target as HTMLElement;
    const act = t.closest<HTMLElement>("[data-act]")?.dataset.act;
    if (act === "close") return void this.close();
    if (act === "unpin") return void this.host.onMenu("unpin");
    const row = t.closest<HTMLElement>(".qrow");
    if (!row) return;
    const i = Number(row.dataset.i);
    if (this.kind === "queue") {
      const q = this.queue[i];
      if (!q) return;
      const ok = await this.host.call<boolean>("nowplaying", "queue_pick", { uri: q.uri, uid: q.uid }).catch(() => false);
      if (!ok) return this.host.error("Spotify isn't connected, so it can't be controlled from here");
      this.host.toast(`Playing ${q.title || "track"}`);
      this.close();
    } else if (this.kind === "search") {
      const r = await this.host.call<{ ok: boolean; error?: string }>("nowplaying", "pin_result", { index: i }).catch(() => null);
      if (r?.ok) {
        this.host.toast("Lyrics saved for this song");
        this.close();
      } else {
        this.host.toast(r?.error ?? "That result has no usable lyrics");
      }
    }
  }

  // ── Context menu ────────────────────────────────────────────

  get menuOpen(): boolean {
    return this.menuEl.classList.contains("open");
  }

  /** at: where; "pt" below-right of the pointer, "below" under a button
   *  (right-aligned to x), "above" over it. */
  menu(items: MenuItem[], at: { x: number; y: number; where: "pt" | "below" | "above" }) {
    this.menuItems = items;
    const checks = items.some((i) => i?.checked);
    this.menuEl.classList.toggle("checks", checks);
    this.menuEl.innerHTML = items.map((it) => it
      ? `<div class="mi${it.enabled ? "" : " dis"}${it.checked ? " chk" : ""}" data-id="${esc(it.id)}">${esc(it.label)}</div>`
      : `<div class="msep"></div>`).join("");
    const rr = this.host.root.getBoundingClientRect();
    this.menuEl.style.visibility = "hidden";
    this.menuEl.classList.add("open");
    const w = this.menuEl.offsetWidth, h = this.menuEl.offsetHeight;
    let x = at.x - rr.left, y = at.y - rr.top;
    if (at.where === "pt") {
      x += 2; y += 2;
      if (y + h > rr.height - 6) y = at.y - rr.top - h - 2;
    } else if (at.where === "below") x -= w;
    else { x -= w; y -= h; }
    x = Math.max(6, Math.min(rr.width - 6 - w, x));
    y = Math.max(6, Math.min(rr.height - 6 - h, y));
    this.menuEl.style.left = `${x}px`;
    this.menuEl.style.top = `${y}px`;
    this.menuEl.style.visibility = "";
  }

  closeMenu(): boolean {
    if (!this.menuOpen) return false;
    this.menuEl.classList.remove("open");
    return true;
  }

  /** Esc: a menu first, then a panel. */
  closeOverlays(): boolean {
    return this.closeMenu() || this.close();
  }

  items(): MenuItem[] {
    return this.menuItems;
  }
}

/** The "Volume 65%" pill. */
export class Toast {
  private el: HTMLElement;
  private t = 0;
  constructor(root: HTMLElement) {
    this.el = document.createElement("div");
    this.el.className = "np-toast";
    root.append(this.el);
  }

  show(text: string, frac?: number | null) {
    const bar = frac !== null && frac !== undefined;
    this.el.innerHTML = `<span>${esc(text)}</span>${bar ? `<i class="tb"><b style="width:${Math.round(Math.max(0, Math.min(1, frac!)) * 100)}%"></b></i>` : ""}`;
    this.el.classList.toggle("bar", bar);
    this.el.classList.add("in");
    window.clearTimeout(this.t);
    this.t = window.setTimeout(() => this.el.classList.remove("in"), 1150);
  }
}
