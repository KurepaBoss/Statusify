// The lyric sheet: rows of text that glide, blur and fade by their distance
// from the sung one, karaoke fill on the active line, breathing dots over
// instrumental breaks, mouse-wheel browsing.
//
// Performance. Rows live in normal flow so the browser wraps and measures
// them; each row's position is one translate3d, and the glide is a CSS
// transition of transform on the compositor, staggered per row with
// transition-delay (the "gentle wave"). Opacity and blur come from a
// data-d bucket (distance from the focus) in now.css, so they transition on
// the compositor too. Layout is measured once per build / resize / font
// change, never per frame; a frame only writes the handful of CSS variables
// of the active karaoke line and the active dots.

import {
  clamp, easeOut, KARA_DIM, LINE_MOVE_S, LINE_STAGGER_S, USER_SCROLL_HOLD_S,
  dotsActive, dotsState, karaBusy, karaEdge, karaFront, karaPieces, type Piece, type Syl,
} from "./now_logic";

export type RowKind = "intro" | "line" | "gap" | "plain" | "static";
export type RowSpec = { kind: RowKind; k: number; t0: number; t1: number | null; text: string; syl?: Syl[] };

type Row = RowSpec & {
  el: HTMLElement;
  body: HTMLElement | null;
  subEl: HTMLElement | null;
  dw: HTMLElement | null;
  top: number;
  h: number;
  bucket: number;
  dl: number;
  hidden: boolean;
  pieces: Piece[] | null;
  spans: HTMLElement[];
  widths: number[];
  spanState: number[];
};

export type FrameCtx = {
  /** sheet position in ms (playback position plus the lyric offset) */
  t: number;
  /** seconds, monotonic */
  now: number;
  playing: boolean;
  /** adaptive quality tier 0..2 */
  tier: number;
};

const DONE = 1e5; // a span fully sung
const WAIT = -1e5; // a span not started

export class Sheet {
  readonly el: HTMLElement;
  private pan: HTMLElement;
  private rowsEl: HTMLElement;
  rows: Row[] = [];
  synced = false;
  focus = -1;
  private S = 0; // scroll of the current focus
  private px = 27;
  private band = 400;
  private anchor = 120;
  private anim = true;
  private breathing = true;
  private focusAt = -10;
  private dotAct = -1;
  private karaAct = -1;
  private user = 0;
  private userTarget = 0;
  private userT = -10;
  private lastFrame = 0;
  private lastW = 0;
  private lastDim = -1;
  private modelKey = "";
  onSeekRow: (i: number, row: RowSpec) => void = () => {};

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("sheet");
    this.pan = document.createElement("div");
    this.pan.className = "pan";
    this.rowsEl = document.createElement("div");
    this.rowsEl.className = "rows";
    this.pan.append(this.rowsEl);
    el.append(this.pan);
    el.addEventListener("wheel", (ev) => this.onWheel(ev), { passive: false });
    this.rowsEl.addEventListener("click", (ev) => {
      if (!this.synced) return;
      const ln = (ev.target as HTMLElement).closest<HTMLElement>(".ln");
      if (!ln) return;
      const i = Number(ln.dataset.i);
      if (this.rows[i]) this.onSeekRow(i, this.rows[i]);
    });
    new ResizeObserver(() => {
      const w = this.rowsEl.clientWidth;
      if (w !== this.lastW) {
        this.lastW = w;
        this.relayout();
      }
    }).observe(this.rowsEl);
    document.fonts?.ready.then(() => this.relayout());
  }

  // ── Settings ────────────────────────────────────────────────

  /** Lyric size in css px, and the height of the band the sheet fills. */
  setGeometry(px: number, bandH: number, anchorFrac: number, fadePx: number, padPx: number) {
    const resized = px !== this.px;
    this.px = px;
    this.band = bandH;
    this.anchor = Math.round(bandH * anchorFrac);
    const s = this.el.style;
    s.setProperty("--lpx", `${px}px`);
    s.setProperty("--anchor", `${this.anchor}px`);
    s.setProperty("--fade", `${fadePx}px`);
    s.setProperty("--pad", `${padPx}px`);
    if (resized) this.relayout();
    else this.applyFocus(this.focus, { immediate: true });
  }

  setMotion(animations: boolean, tier: number) {
    this.anim = animations;
    this.breathing = animations && tier < 2;
    this.el.classList.toggle("noanim", !animations);
  }

  setPlaying(p: boolean) {
    this.el.classList.toggle("paused", !p);
  }

  // ── Model ───────────────────────────────────────────────────

  get key(): string {
    return this.modelKey;
  }

  /** Replace the rows. `quiet` keeps the sheet where it is (a refinement of
   *  the same lyrics: timing arrived, translation changed). */
  setModel(key: string, specs: RowSpec[], synced: boolean, now: number, quiet = false) {
    this.modelKey = key;
    this.synced = synced;
    this.el.classList.toggle("synced", synced);
    this.rowsEl.replaceChildren();
    const keepFocus = quiet ? this.focus : -1;
    this.rows = specs.map((s, i) => this.build(s, i));
    this.rowsEl.append(...this.rows.map((r) => r.el));
    this.dotAct = -1;
    this.karaAct = -1;
    this.focus = -1;
    if (!quiet) {
      this.user = this.userTarget = 0;
      this.pan.style.transform = "";
      this.el.classList.remove("browsing");
      // The sheet rises and fades in over 0.45 s.
      this.el.classList.remove("born");
      void this.el.offsetWidth;
      if (this.anim) {
        this.el.classList.add("born");
        window.setTimeout(() => this.el.classList.remove("born"), 520);
      }
    }
    this.measure();
    this.focusAt = now - 10;
    if (keepFocus >= 0) this.applyFocus(keepFocus, { immediate: true });
  }

  private build(s: RowSpec, i: number): Row {
    const el = document.createElement("div");
    el.className = "ln";
    el.dataset.i = String(i);
    const row: Row = { ...s, el, body: null, subEl: null, dw: null, top: 0, h: 0, bucket: 99, dl: -1, hidden: false, pieces: null, spans: [], widths: [], spanState: [] };
    if (s.kind === "intro" || s.kind === "gap" || s.kind === "static") {
      el.classList.add("dots");
      el.innerHTML = `<div class="dw"><i></i><i></i><i></i></div>`;
      row.dw = el.firstElementChild as HTMLElement;
      return row;
    }
    const body = document.createElement("div");
    body.className = "ln-t";
    row.body = body;
    el.append(body);
    if (!s.text.trim()) el.classList.add("blank");
    const timed = s.kind === "line" ? karaPieces(s.text, s.syl) : null;
    if (timed) {
      // Every piece of the text is a span, so the text between the timed pieces
      // (punctuation) is dim until the piece before it is sung, as one line.
      row.pieces = [];
      const add = (a: number, b: number, t0: number, t1: number) => {
        const sp = document.createElement("span");
        sp.className = "k";
        sp.textContent = s.text.slice(a, b);
        body.append(sp);
        row.spans.push(sp);
        row.pieces!.push({ a, b, t0, t1 });
      };
      let at = 0, prevEnd = timed[0].t0;
      for (const p of timed) {
        const gap = s.text.slice(at, p.a);
        if (gap.trim()) add(at, p.a, prevEnd, prevEnd + 1);
        else if (gap) body.append(document.createTextNode(gap));
        add(p.a, p.b, p.t0, p.t1);
        at = p.b;
        prevEnd = p.t1;
      }
      const tail = s.text.slice(at);
      if (tail.trim()) add(at, s.text.length, prevEnd, prevEnd + 1);
      else if (tail) body.append(document.createTextNode(tail));
    } else {
      body.textContent = s.text;
    }
    return row;
  }

  /** Romanisation / translation under each row (null: none). */
  setSubs(subs: (string | null)[] | null) {
    let changed = false;
    this.rows.forEach((r, i) => {
      const t = subs?.[i] ?? null;
      if (!r.body) return;
      if (t && !r.subEl) {
        r.subEl = document.createElement("div");
        r.subEl.className = "ln-s";
        r.el.append(r.subEl);
        changed = true;
      }
      if (r.subEl) {
        if (!t) {
          r.subEl.remove();
          r.subEl = null;
          changed = true;
        } else if (r.subEl.textContent !== t) {
          r.subEl.textContent = t;
          changed = true;
        }
      }
    });
    if (changed) this.relayout();
  }

  // ── Layout ──────────────────────────────────────────────────

  private measure() {
    for (const r of this.rows) {
      r.top = r.el.offsetTop;
      r.h = r.el.offsetHeight;
      r.widths = [];
      r.spanState = [];
    }
  }

  relayout() {
    if (!this.rows.length) return;
    this.measure();
    this.applyFocus(this.focus, { immediate: true });
  }

  // ── Focus (the sung row) ────────────────────────────────────

  /** Move to row `idx`: every row glides to the new scroll, a little later
   *  the further down it is; blur and opacity follow by distance. */
  applyFocus(idx: number, o: { immediate?: boolean; now?: number } = {}) {
    if (!this.rows.length) return;
    idx = clamp(idx, 0, this.rows.length - 1);
    const prev = this.focus;
    const prevS = this.S;
    const S = this.rows[idx].top;
    const jump = prev < 0 || Math.abs(idx - prev) > 6;
    const glide = this.anim && !o.immediate && !jump;
    if (!glide) this.rowsEl.classList.add("nt");
    this.focus = idx;
    this.S = S;
    if (prev !== idx) this.focusAt = o.now ?? performance.now() / 1000;
    const reach = this.band + this.px * 4;
    const tr = `translate3d(0,${-S}px,0)`;
    for (let i = 0; i < this.rows.length; i++) {
      const r = this.rows[i];
      const d = i - idx;
      const b = clamp(d, -4, 5);
      if (r.bucket !== b) {
        r.el.dataset.d = String(b);
        r.bucket = b;
      }
      const dl = glide ? Math.round(clamp(d, 0, 5) * LINE_STAGGER_S * 1000) : 0;
      if (r.dl !== dl) {
        r.el.style.setProperty("--dl", `${dl}ms`);
        r.dl = dl;
      }
      // Rows far from both the old and the new scroll are not painted at all.
      const near = (s: number) => r.top - s > -reach - r.h && r.top - s < reach;
      const hide = !(near(S) || near(prevS));
      if (hide !== r.hidden) {
        r.el.classList.toggle("off", hide);
        r.hidden = hide;
      }
      r.el.style.transform = tr;
    }
    if (prev >= 0 && prev !== idx) this.rows[prev]?.el.classList.remove("now");
    this.rows[idx].el.classList.add("now");
    if (this.karaAct !== idx) {
      this.karaAct = this.rows[idx].pieces ? idx : -1;
      this.lastDim = -1;
      const r = this.rows[idx];
      r.spanState = [];
      if (r.pieces) r.el.style.setProperty("--e", `${karaEdge(this.px)}px`);
    }
    if (!glide) {
      void this.rowsEl.offsetWidth; // commit the jump before transitions come back
      requestAnimationFrame(() => this.rowsEl.classList.remove("nt"));
    }
    // Rows that were hidden for being far away come back after the glide.
    if (glide) window.setTimeout(() => this.cull(), (LINE_MOVE_S + 0.3) * 1000);
  }

  private cull() {
    if (!this.rows.length) return;
    const reach = this.band + this.px * 4;
    for (const r of this.rows) {
      const hide = !(r.top - this.S > -reach - r.h && r.top - this.S < reach);
      if (hide !== r.hidden) {
        r.el.classList.toggle("off", hide);
        r.hidden = hide;
      }
    }
  }

  // ── Browsing ────────────────────────────────────────────────

  private onWheel(ev: WheelEvent) {
    if (!this.rows.length || ev.ctrlKey) return;
    ev.preventDefault();
    const dy = ev.deltaMode === 1 ? ev.deltaY * 32 : ev.deltaY;
    const cur = Math.max(0, this.focus);
    const lo = this.rows[0].top - this.rows[cur].top;
    const hi = this.rows[this.rows.length - 1].top - this.rows[cur].top;
    this.userTarget = clamp(this.userTarget + dy * 0.64, lo, hi);
    this.userT = performance.now() / 1000;
  }

  /** Back to the sung line at once (after a click on a row). */
  followNow() {
    this.user = this.userTarget = 0;
    this.pan.style.transform = "";
    this.el.classList.remove("browsing");
  }

  get browsing(): boolean {
    return Math.abs(this.userTarget) > 0.5;
  }

  // ── Frame ───────────────────────────────────────────────────

  /** Per-frame work: wheel easing, the active dots, the karaoke fill. */
  frame(c: FrameCtx) {
    const dt = this.lastFrame ? Math.min(0.1, c.now - this.lastFrame) : 1 / 60;
    this.lastFrame = c.now;
    // Manual browsing eases to the wheel's target; after a pause it drifts back to the sung line.
    if (this.userTarget && c.now - this.userT > USER_SCROLL_HOLD_S && !this.rowsEl.querySelector(".ln:hover")) this.userTarget = 0;
    const diff = this.userTarget - this.user;
    if (Math.abs(diff) > 0.5) {
      this.user += diff * (this.anim ? 1 - 0.78 ** (dt * 60) : 1);
      this.pan.style.transform = `translate3d(0,${-this.user.toFixed(1)}px,0)`;
    } else if (this.user !== this.userTarget) {
      this.user = this.userTarget;
      this.pan.style.transform = this.user ? `translate3d(0,${-this.user}px,0)` : "";
    }
    const brw = Math.abs(this.user) > 40;
    if (brw !== this.el.classList.contains("browsing")) this.el.classList.toggle("browsing", brw);

    const f = this.rows[this.focus];
    if (!f) return;
    // Breathing dots over a break.
    let want = -1;
    if (this.synced && f.dw) {
      if ((f.kind === "intro" || f.kind === "gap") && dotsActive({ kind: f.kind, k: f.k, t0: f.t0, t1: f.t1 }, this.focus, this.focus, c.t)) want = this.focus;
    }
    if (this.dotAct !== want) {
      const old = this.rows[this.dotAct];
      if (old?.dw) this.setDots(old, 0.62, [0.8, 0.8, 0.8], false);
      this.dotAct = want;
    }
    if (want >= 0) {
      const st = dotsState({ kind: f.kind, t0: f.t0, t1: f.t1 }, c.t, c.now, true, c.playing, this.breathing);
      this.setDots(f, st.scale, st.levels, true);
    } else if (f.dw && !this.synced) {
      this.setDots(f, 0.62, [0.8, 0.8, 0.8], false);
    }
    // Karaoke on the active line.
    if (this.karaAct === this.focus && f.pieces && this.synced) this.kara(f, c);
  }

  private q = new WeakMap<HTMLElement, string>();
  private setDots(r: Row, scale: number, lv: number[], live: boolean) {
    const sig = `${Math.round(scale * 200)}|${lv.map((v) => Math.round(v * 50)).join(",")}|${live}`;
    if (this.q.get(r.dw!) === sig) return;
    this.q.set(r.dw!, sig);
    const s = r.dw!.style;
    s.setProperty("--s", scale.toFixed(3));
    lv.forEach((v, j) => s.setProperty(`--l${j}`, v.toFixed(2)));
    r.dw!.classList.toggle("live", live);
  }

  private kara(r: Row, c: FrameCtx) {
    const pieces = r.pieces!;
    if (!r.widths.length) r.widths = r.spans.map((s) => s.getBoundingClientRect().width);
    const edge = karaEdge(this.px);
    // The dimming of the unsung words fades in as the sheet settles on this line.
    const near = this.anim ? easeOut(clamp((c.now - this.focusAt) / LINE_MOVE_S)) : 1;
    const dim = Math.round((1 - (1 - KARA_DIM) * near) * 48) / 48;
    if (dim !== this.lastDim) {
      r.el.style.setProperty("--dim", String(dim));
      this.lastDim = dim;
    }
    for (let i = 0; i < pieces.length; i++) {
      const p = pieces[i];
      const t = c.t;
      let v: number;
      if (t >= p.t1) v = DONE;
      else if (t <= p.t0) v = WAIT;
      else v = Math.round(karaFront(t, p.t0, p.t1, r.widths[i], edge) * 2) / 2;
      if (r.spanState[i] !== v) {
        r.spanState[i] = v;
        r.spans[i].style.setProperty("--f", v >= DONE ? "99999px" : v <= WAIT ? `${-edge}px` : `${v}px`);
      }
    }
  }

  /** True while the active line's syllables are being sung (for tests / throttling). */
  karaBusy(t: number): boolean {
    const r = this.rows[this.focus];
    return !!r && karaBusy(r.pieces, t);
  }

  rowText(i: number): string {
    return this.rows[i]?.kind === "intro" || this.rows[i]?.kind === "gap" || this.rows[i]?.kind === "static" ? "" : this.rows[i]?.text ?? "";
  }
}
