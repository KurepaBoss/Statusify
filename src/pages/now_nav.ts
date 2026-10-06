// The page switcher's motion (statusify_ui_backdrop: _slide_to / _slide_step /
// _drag_*). main.ts (the shell's) only toggles which page is `.on`; this
// watches body[data-page] and turns that into Python's behaviour:
//   * the pages slide sideways according to tab order (420 ms ease-out quart),
//     the leaving page too, and a click mid-slide carries on from where the
//     pages are rather than jumping;
//   * the pill under the selected tab slides with them;
//   * a drag or flick across the page pulls the neighbouring page in.
// Everything that moves is a transform on a composited layer.

import * as L from "./now_logic";

const NO_DRAG = "button, a, input, textarea, select, [role=slider], .np-seek, .np-panel, .np-menu, [data-nodrag]";

let started = false;

export function startNav() {
  if (started) return;
  started = true;
  const body = document.body;
  const tabs = document.getElementById("tabs")!;
  const pages = document.getElementById("pages")!;

  let ids: string[] = [];
  let pageEls = new Map<string, HTMLElement>();
  let tabEls: HTMLElement[] = [];
  let pill: HTMLElement | null = null;
  let cur = "";
  let pillPos = 0; // tab index the pill sits at (fractional while moving)
  let pos: Record<string, number> = {}; // page -> x in page widths while pages move
  let slide: { plan: Record<string, [number, number]>; t0: number; to: string; p0: number; p1: number } | null = null;
  let raf = 0;

  const animated = () => document.documentElement.dataset.anim !== "off";

  function build() {
    tabEls = [...tabs.querySelectorAll<HTMLElement>(".tab")];
    ids = tabEls.map((t) => t.dataset.page!);
    pageEls = new Map(ids.map((id) => [id, document.getElementById(`page-${id}`)!]));
    tabs.style.setProperty("--tabs", String(ids.length));
    pill = document.createElement("div");
    pill.className = "tab-pill";
    tabs.prepend(pill);
  }

  function setPill(p: number) {
    pillPos = p;
    if (pill) pill.style.transform = `translate3d(${(p * 100).toFixed(2)}%,0,0)`;
  }

  /** Put every moving page where `pos` says. */
  function apply() {
    for (const [id, el] of pageEls) {
      const x = pos[id];
      if (x === undefined) {
        el.classList.remove("sl");
        el.style.transform = "";
      } else {
        el.classList.add("sl");
        el.style.transform = `translate3d(${(x * 100).toFixed(3)}%,0,0)`;
      }
    }
  }

  function settle() {
    slide = null;
    pos = {};
    cancelAnimationFrame(raf);
    raf = 0;
    pages.classList.remove("sliding");
    apply();
    setPill(Math.max(0, ids.indexOf(cur)));
  }

  function step(now: number) {
    raf = 0;
    const s = slide;
    if (!s) return;
    const t = (now - s.t0) / L.SLIDE_MS;
    const e = L.easeOut(L.clamp(t));
    for (const [p, [x0, x1]] of Object.entries(s.plan)) pos[p] = x0 + (x1 - x0) * e;
    apply();
    setPill(s.p0 + (s.p1 - s.p0) * e);
    if (t < 1) raf = requestAnimationFrame(step);
    else settle();
  }

  /** Slide `to` into view from wherever the pages are now. */
  function slideTo(to: string, leaving: string = to) {
    const from = Object.keys(pos).length ? { ...pos } : { [leaving]: 0 };
    if (!animated()) {
      settle();
      return;
    }
    pages.classList.add("sliding");
    const plan = L.slidePlan(from, to, ids);
    pos = Object.fromEntries(Object.entries(plan).map(([p, [x0]]) => [p, x0]));
    slide = { plan, t0: performance.now(), to, p0: pillPos, p1: ids.indexOf(to) };
    cancelAnimationFrame(raf);
    apply();
    raf = requestAnimationFrame(step);
  }

  // The shell changed page.
  function onPage() {
    const next = body.dataset.page ?? "";
    if (!next) return;
    if (!ids.length) build();
    const first = !cur;
    const prev = cur;
    cur = next;
    if (first) return settle();
    if (prev !== next || slide) slideTo(next, prev);
  }
  new MutationObserver(onPage).observe(body, { attributes: true, attributeFilter: ["data-page"] });
  if (body.dataset.page) onPage();

  // ── Dragging ──
  type Drag = { x0: number; y0: number; on: boolean; n: string | null; samples: [number, number][]; id: number };
  let drag: Drag | null = null;
  let swallow = false;

  pages.addEventListener("pointerdown", (e) => {
    drag = null;
    if (e.button !== 0 || !animated() || !cur || document.documentElement.hasAttribute("data-npfs")) return;
    if ((e.target as HTMLElement).closest(NO_DRAG)) return;
    // Pressing during a slide takes the pages where they are.
    if (slide) settle();
    drag = { x0: e.clientX, y0: e.clientY, on: false, n: null, samples: [[performance.now(), e.clientX]], id: e.pointerId };
  });

  pages.addEventListener("pointermove", (e) => {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
    if (!d.on) {
      if (Math.abs(dx) < L.DRAG_START_PX || Math.abs(dx) <= Math.abs(dy)) {
        if (Math.abs(dy) > L.DRAG_START_PX) drag = null; // a vertical gesture: leave it alone
        return;
      }
      d.on = true;
      try { pages.setPointerCapture(e.pointerId); } catch { /* the pointer is gone */ }
      pages.classList.add("dragging", "sliding");
    }
    d.samples = [...d.samples, [performance.now(), e.clientX] as [number, number]].slice(-6);
    const W = Math.max(1, pages.clientWidth);
    const n = L.dragNeighbour(ids, cur, dx);
    // Nothing that way: resist.
    const off = (n === null ? dx * 0.3 : dx) / W;
    d.n = n;
    pos = { [cur]: off };
    if (n !== null) pos[n] = off + (dx < 0 ? 1 : -1);
    apply();
    if (n !== null) setPill(ids.indexOf(cur) + (ids.indexOf(n) - ids.indexOf(cur)) * Math.min(1, Math.abs(off)));
  });

  const end = (e: PointerEvent, cancelled: boolean) => {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    drag = null;
    if (!d.on) return;
    pages.classList.remove("dragging");
    swallow = true; // the click that follows a drag is not a click
    window.setTimeout(() => (swallow = false), 0);
    const dx = e.clientX - d.x0;
    const s = d.samples;
    let v = 0;
    if (s.length >= 2 && s[s.length - 1][0] > s[0][0]) v = ((s[s.length - 1][1] - s[0][1]) / (s[s.length - 1][0] - s[0][0])) * 1000;
    if (!cancelled && d.n !== null && L.dragSwitches(dx, v, true)) {
      // Through the shell, so its show()/hide() hooks run; the observer then slides on from here.
      tabEls[ids.indexOf(d.n)]?.click();
    } else {
      slideTo(cur);
    }
  };
  pages.addEventListener("pointerup", (e) => end(e, false));
  pages.addEventListener("pointercancel", (e) => end(e, true));
  pages.addEventListener("click", (e) => {
    if (swallow) { e.stopPropagation(); e.preventDefault(); }
  }, true);
}
