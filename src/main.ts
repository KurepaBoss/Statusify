// Main window shell: tab bar + one page per tab. Each page lives in
// src/pages/<id>.ts and exports mount(root) (called once) and optional
// show()/hide() hooks when its tab is selected / left.
import "./api";
import * as now from "./pages/now";
import * as history from "./pages/history";
import * as stats from "./pages/stats";
import * as settings from "./pages/settings";

type Page = { mount(root: HTMLElement): void; show?(): void; hide?(): void };
const PAGES: { id: string; label: string; page: Page }[] = [
  { id: "now", label: "Lyrics", page: now },
  { id: "history", label: "History", page: history },
  { id: "stats", label: "Stats", page: stats },
  { id: "settings", label: "Settings", page: settings },
];

const tabs = document.getElementById("tabs")!;
const pages = document.getElementById("pages")!;
let current = "";

for (const p of PAGES) {
  const b = document.createElement("button");
  b.className = "tab";
  b.textContent = p.label;
  b.dataset.page = p.id;
  b.addEventListener("click", () => select(p.id));
  tabs.append(b);
  const sec = document.createElement("section");
  sec.className = "page";
  sec.id = `page-${p.id}`;
  pages.append(sec);
  p.page.mount(sec);
}

export function select(id: string) {
  if (id === current) return;
  const prev = PAGES.find((p) => p.id === current);
  prev?.page.hide?.();
  current = id;
  document.body.dataset.page = id;
  for (const b of tabs.querySelectorAll<HTMLElement>(".tab")) b.classList.toggle("on", b.dataset.page === id);
  for (const s of pages.querySelectorAll<HTMLElement>(".page")) s.classList.toggle("on", s.id === `page-${id}`);
  PAGES.find((p) => p.id === id)?.page.show?.();
}

select("now");
