// Settings page (owner: shell agent). Port of the Python app's Settings page:
// grouped cards, one control per row, everything saves as you change it.
import { openUrl } from "@tauri-apps/plugin-opener";
import { call, onSnapshot, player, position, seek, type Snapshot } from "../api";
import { getPrefs, initTheme, onPrefs, refreshPrefs, setPrefs, type Prefs } from "./settings_theme";
import { availableText, changelogText, checkFailedMessage, checkMessage, installLabel, shouldAutoShow, updateDescription, type CheckResult, type UpdateInfo } from "./settings_update";
import "./settings.css";

initTheme();

// ── tiny DOM helpers ─────────────────────────────────────────────
type Child = Node | string | null | undefined | false;
function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", ...kids: Child[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) if (k) e.append(k);
  return e;
}

let snap: Snapshot | null = null;
let shell: Record<string, any> = {};
let languages: { code: string; name: string }[] = [];
let fonts: string[] = ["Segoe UI"];
const syncs: (() => void)[] = [];
const P = (): Prefs => getPrefs();

/** Run `fn` now and again on every state change. */
function reg<T extends HTMLElement>(el: T, fn: (el: T) => void): T {
  const run = () => fn(el);
  syncs.push(run);
  run();
  return el;
}
const syncAll = () => syncs.forEach((f) => f());

// ── toasts and dialogs ───────────────────────────────────────────
let toastEl: HTMLElement;
let toastTimer = 0;
function toast(msg: string, err = false) {
  toastEl.textContent = msg;
  toastEl.classList.toggle("err", err);
  toastEl.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toastEl.classList.remove("on"), err ? 6000 : 3500);
}
const fail = (e: unknown) => toast(String((e as any)?.message ?? e), true);

type DialogBtn = { text: string; kind?: "primary" | "ghost"; run?: () => boolean | void | Promise<boolean | void> };
function dialog(title: string, body: Node[], buttons: DialogBtn[]): HTMLElement {
  const back = h("div", "set-modal");
  const box = h("div", "set-dialog");
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-label", title);
  box.append(h("h3", "", title), ...body);
  const bar = h("div", "set-dialog-btns");
  const close = () => back.remove();
  for (const b of buttons) {
    const x = h("button", `set-btn ${b.kind ?? "secondary"}`, b.text);
    x.addEventListener("click", async () => {
      const keep = await b.run?.();
      if (keep !== false) close();
    });
    bar.append(x);
  }
  box.append(bar);
  back.append(box);
  back.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
    if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") {
      e.preventDefault();
      bar.querySelector<HTMLElement>(".primary")?.click();
    }
  });
  document.body.append(back);
  (box.querySelector("input") ?? bar.querySelector<HTMLElement>(".primary"))?.focus();
  return back;
}

// ── controls ─────────────────────────────────────────────────────
function btn(text: string, on: () => void, kind: "secondary" | "ghost" | "primary" = "secondary") {
  const b = h("button", `set-btn ${kind}`, text);
  b.addEventListener("click", on);
  return b;
}

/** Toggle switch. The knob moves first; the setting follows and may refuse. */
function sw(get: () => boolean, set: (v: boolean) => unknown): HTMLElement {
  const b = h("button", "set-switch", h("span", "knob"));
  b.setAttribute("role", "switch");
  b.addEventListener("click", async () => {
    const want = !get();
    b.setAttribute("aria-checked", String(want));
    b.classList.toggle("on", want);
    try {
      await set(want);
    } catch (e) {
      fail(e);
    }
    syncAll();
  });
  return reg(b, (el) => {
    const on = get();
    el.setAttribute("aria-checked", String(on));
    el.classList.toggle("on", on);
  });
}

/** A track with the chosen segment raised; the raised part slides on change. */
function seg(options: [string, string][], get: () => string, choose: (k: string) => unknown, full = false): HTMLElement {
  const wrap = h("div", `set-seg${full ? " full" : ""}`);
  wrap.style.setProperty("--n", String(options.length));
  wrap.append(h("span", "thumb"));
  const buttons = options.map(([label, key]) => {
    const b = h("button", "", label);
    b.addEventListener("click", async () => {
      wrap.style.setProperty("--i", String(options.findIndex((o) => o[1] === key)));
      try {
        await choose(key);
      } catch (e) {
        fail(e);
      }
      syncAll();
    });
    wrap.append(b);
    return b;
  });
  return reg(wrap, (el) => {
    const cur = get();
    const i = Math.max(0, options.findIndex((o) => o[1] === cur));
    el.style.setProperty("--i", String(i));
    buttons.forEach((b, k) => b.classList.toggle("on", k === i));
  });
}

function value(get: () => string, min = 0, accent?: () => boolean): HTMLElement {
  const v = h("span", "set-value");
  if (min) v.style.minWidth = `${min}px`;
  return reg(v, (el) => {
    el.textContent = get();
    el.classList.toggle("accent", !!accent?.());
  });
}

const group = (...kids: Node[]) => h("div", "set-group", ...kids);

/** Text field that saves on Enter and when it loses focus. */
function field(get: () => string, save: (v: string) => unknown, cls = "", placeholder = ""): HTMLInputElement {
  const i = h("input", `set-input ${cls}`);
  i.type = "text";
  i.spellcheck = false;
  i.placeholder = placeholder;
  let shown = "";
  i.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      i.blur();
    }
  });
  i.addEventListener("blur", async () => {
    if (i.value === shown) return;
    try {
      await save(i.value);
    } catch (e) {
      fail(e);
    }
    shown = "\u0000"; // force the next sync to redraw from the stored value
    syncAll();
  });
  return reg(i, (el) => {
    if (document.activeElement === el) return;
    shown = get();
    el.value = shown;
  });
}

type Row = { title?: string; desc?: string | (() => string); ctl?: Node; full?: Node; note?: () => string };
function card(rows: Row[]): HTMLElement {
  const c = h("div", "set-card");
  for (const r of rows) {
    if (r.full) {
      c.append(h("div", "set-full", r.full));
      continue;
    }
    const left = h("div", "set-left", h("div", "set-title", r.title));
    if (r.desc) {
      const d = h("div", "set-desc");
      const desc = r.desc;
      if (typeof desc === "string") d.textContent = desc;
      else reg(d, (el) => (el.textContent = desc()));
      left.append(d);
    }
    if (r.note) {
      const note = r.note;
      left.append(
        reg(h("div", "set-note"), (el) => {
          const t = note();
          el.textContent = t;
          el.hidden = !t;
        }),
      );
    }
    c.append(h("div", "set-row", left, r.ctl ? h("div", "set-ctl", r.ctl) : null));
  }
  return c;
}

const section = (title: string, sub?: string) => h("div", "set-section", h("h2", "", title), sub ? h("p", "", sub) : null);

// ── helpers over backend state ───────────────────────────────────
const core = () => (snap?.extras?.core ?? {}) as Record<string, any>;
const windowsState = () => (snap?.extras?.windows ?? {}) as Record<string, any>;

/** Write a setting. The page and the theme answer at once; a refusal rolls back. */
async function setPref(key: string, v: unknown) {
  setPrefs({ [key]: v }, true);
  try {
    setPrefs({ [key]: await call("settings", "set", { key, value: v }) }, true);
  } catch (e) {
    await refreshPrefs();
    throw e;
  }
}
const prefSwitch = (key: string) => sw(() => !!P()[key], (v) => setPref(key, v));

function sysLang(): string {
  const l = (navigator.language || "en").toLowerCase();
  if (l.startsWith("zh")) return "zh-CN";
  const base = l.split("-")[0];
  return languages.some((x) => x.code === base) ? base : "en";
}
const langName = (code: string) => languages.find((l) => l.code === code)?.name ?? code;

/** "45", "45m", "45 min", "45 minutes" -> 45; anything else (or out of 1..600) -> null. */
export function parseMinutes(text: string): number | null {
  let s = (text || "").trim().toLowerCase();
  for (const suffix of ["minutes", "min", "m"]) {
    if (s.endsWith(suffix)) {
      s = s.slice(0, -suffix.length).trim();
      break;
    }
  }
  if (!/^\d+$/.test(s)) return null;
  const m = parseInt(s, 10);
  return m >= 1 && m <= 600 ? m : null;
}

// Sleep timer: the page remembers what it set so "15m" stays lit; any other
// running timer reads as Custom.
let sleepChoice: string | null = null;
let sleepCustomOpen = false;
let lastSleepLabel = "";
/** Which segment is lit. core publishes sleep_value ("off" | "eos" | minutes), so a
 *  timer started from the tray or the Lyrics page lights the right one too. */
export function sleepSegment(value: unknown, label: string, custom: boolean, choice: string | null): string {
  if (custom) return "custom";
  if (typeof value === "string") {
    if (value === "off" || value === "") return "off";
    if (value === "eos") return "eos";
    return ["15", "30", "60"].includes(value) ? value : "custom";
  }
  // Older core without sleep_value: infer from the label.
  if (!label) return "off";
  if (!/^\d+:\d{2}$/.test(label.trim())) return "eos"; // "End of song" and friends, not a countdown
  return choice && ["15", "30", "60"].includes(choice) ? choice : "custom";
}
const sleepKey = () => sleepSegment(core().sleep_value, String(core().sleep_label || ""), sleepCustomOpen, sleepChoice);

// ── updates ──────────────────────────────────────────────────────
const update = (): UpdateInfo | null => (core().update_available as UpdateInfo | null) || null;
/** "You have v3.0.0." (nothing until the shell state, which carries the version, has arrived). */
const youHave = () => (shell.version ? `You have v${shell.version}.` : "");
let updateNote = "";
let checking = false;
let autoShownTag: string | null = null;
const DISMISSED = "statusify.update.dismissed";
const dismissedTag = (): string | null => {
  try {
    return localStorage.getItem(DISMISSED);
  } catch {
    return null;
  }
};
const dismiss = (tag: string) => {
  try {
    localStorage.setItem(DISMISSED, tag);
  } catch {
    /* no storage here: it is simply offered again next time */
  }
};

/** The "Check now" button: ask core (it answers from what it knows when asked twice within a minute). */
async function checkNow() {
  if (checking) return;
  checking = true;
  updateNote = "Checking…";
  syncAll();
  try {
    updateNote = checkMessage(await call<CheckResult>("core", "check_update"));
  } catch {
    updateNote = checkFailedMessage();
  }
  checking = false;
  syncAll();
}

async function installUpdate(u: UpdateInfo) {
  toast(u.can_install ? "Downloading the update…" : "Opening the download page…");
  try {
    const r = await call<{ installed: boolean; open_url?: string; error?: string }>("core", "install_update");
    if (r.installed) return; // the installer takes over and Statusify restarts
    if (r.error) toast(r.error, true);
    if (r.open_url) await openUrl(r.open_url);
  } catch (e) {
    fail(e);
  }
}

/** "Statusify v3.1.0 is available": what changed, then Later / the release page / Install. */
function updateDialog(u: UpdateInfo) {
  if (document.querySelector(".set-update")) return;
  const back = dialog(`Statusify v${u.tag} is available`, [
    h("p", "set-dialog-text", `${youHave()} Here is what changed:`.trim()),
    h("pre", "set-changelog", changelogText(u)),
  ], [
    { text: "Later", kind: "ghost", run: () => dismiss(u.tag) },
    { text: "Release page", run: () => { void openUrl(u.url).catch(() => undefined); return false; } },
    { text: installLabel(u), kind: "primary", run: () => void installUpdate(u) },
  ]);
  back.classList.add("set-update");
}

async function restartNow() {
  try {
    await call("shell", "restart_app");
  } catch (e) {
    fail(e);
  }
}

// ── the page ─────────────────────────────────────────────────────
let visible = false;
let logTimer = 0;
let logBox: HTMLElement;
const logShown = () => !(P().collapsed ?? ["log"]).includes("log");

async function pollLog(force = false) {
  if (!logBox || !visible || !logShown()) return;
  let rows: { ts: string; msg: string; tag: string }[];
  try {
    rows = await call("settings", "read_log", { lines: 300 });
  } catch {
    return;
  }
  const last = rows[rows.length - 1];
  const key = `${rows.length}|${last?.ts ?? ""}|${last?.msg ?? ""}`;
  if (!force && key === logBox.dataset.key) return;
  logBox.dataset.key = key;
  const pinned = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
  const frag = document.createDocumentFragment();
  for (const r of rows) frag.append(h("div", `ln ${r.tag}`, h("span", "ts", r.ts), " ", h("span", "msg", r.msg)));
  logBox.replaceChildren(frag);
  if (pinned || force) logBox.scrollTop = logBox.scrollHeight;
}

function build(): HTMLElement {
  const page = h("div", "set");
  toastEl = h("div", "set-toast");
  toastEl.setAttribute("role", "status");
  page.append(h("div", "set-title-block", h("h1", "", "Settings"), h("p", "", "Changes save as you make them.")));

  // Banner: no Discord App ID, or a changed one waiting for a restart.
  const bannerText = h("span");
  const bannerBtn = btn("Restart now", () => void restartNow(), "primary");
  page.append(
    reg(h("div", "set-banner", bannerText, bannerBtn), (el) => {
      const missing = shell.app_id_set === false;
      el.hidden = !(missing || shell.needs_restart === true);
      bannerText.textContent = missing
        ? "No Discord Application ID yet, so your status is off. Add one under Discord below."
        : "The saved Discord Application ID is not the one in use yet. Press Reconnect under Discord, or restart Statusify.";
      bannerBtn.hidden = missing;
    }),
  );

  // Banner: a newer release exists.
  const updText = h("span");
  const updBtn = btn("What's new", () => {
    const u = update();
    if (u) updateDialog(u);
  }, "primary");
  page.append(
    reg(h("div", "set-banner", updText, updBtn), (el) => {
      const u = update();
      el.hidden = !u;
      if (u) updText.textContent = `${availableText(u)} ${youHave()}`.trim();
    }),
  );

  // ── Lyrics ──
  const track = () => !!snap?.track;
  // Python's _refresh_track_offset: "global (+N ms)" muted when the track has no
  // offset of its own, else "+N ms" in the accent. core says which one it is.
  const signed = (ms: number) => `${ms >= 0 ? "+" : ""}${ms}`;
  const ownOffset = () => (typeof core().track_offset_is_song === "boolean" ? core().track_offset_is_song === true : Number(core().track_offset_ms ?? 0) !== 0);
  const offsetText = () => {
    if (!track()) return "No track";
    const ms = Number(core().track_offset_ms ?? 0);
    if (ownOffset()) return `${signed(ms)} ms`;
    return `global (${signed(Number(core().global_delay_ms ?? ms))} ms)`;
  };
  const needTrack = (fn: () => Promise<unknown>) => async () => {
    if (!track()) return toast("No track playing, so there is no per-track offset to change");
    try {
      await fn();
    } catch (e) {
      fail(e);
    }
  };
  const overlayHotkey = () => String(shell.hotkeys?.overlay || "").trim();
  const overlayOn = () => (typeof windowsState().overlay === "boolean" ? !!windowsState().overlay : !!P().overlay_enabled);
  const stepLang = async (d: number) => {
    const codes = languages.map((l) => l.code);
    if (!codes.length) return;
    const i = Math.max(0, codes.indexOf(String(P().translate_to)));
    await setPref("translate_to", codes[(i + d + codes.length) % codes.length]).catch(fail);
    syncAll();
  };
  const nudgeOverlay = (d: number) => () => void setPref("overlay_size", Number(P().overlay_size ?? 30) + d).then(syncAll, fail);
  const lyricsCard = card([
    {
      title: "Offset for this track",
      desc: "Shifts the timing of the song that's playing, like the Lyrics page's delay stepper. Shift-click that stepper to change the global delay instead.",
      ctl: group(
        btn("−250", needTrack(() => call("core", "nudge_track_offset", { delta_ms: -250 }))),
        value(offsetText, 96, () => track() && ownOffset()),
        btn("+250", needTrack(() => call("core", "nudge_track_offset", { delta_ms: 250 }))),
        btn("Reset", needTrack(() => call("core", "set_track_offset", { ms: null })), "ghost"),
      ),
    },
    { title: "Search LRCLIB as a fallback", desc: "Used only when Spicy Lyrics and Spotify have nothing for a track.", ctl: prefSwitch("lrclib_fallback") },
    {
      title: "Desktop overlay",
      desc: () => "Shows the current line over other windows and borderless games. Clicks pass through it." + (overlayHotkey() ? ` Toggle with ${overlayHotkey()}.` : ""),
      ctl: sw(overlayOn, async (want) => {
        if (want !== overlayOn()) await call("windows", "toggle_overlay");
      }),
    },
    { title: "Overlay: show the next line", ctl: prefSwitch("overlay_next_line") },
    {
      title: "Overlay text size",
      ctl: group(btn("A−", nudgeOverlay(-2)), value(() => String(P().overlay_size ?? 30), 48, () => P().overlay_size !== 30), btn("A+", nudgeOverlay(2))),
    },
    {
      title: "Overlay position",
      desc: "Unlock, drag it where you want it, then Lock (or double-click it).",
      ctl: group(
        value(() => (P().overlay_locked === false ? "Movable" : "Locked"), 64, () => P().overlay_locked === false),
        reg(btn("", () => void setPref("overlay_locked", P().overlay_locked === false).then(syncAll, fail)), (b) => {
          b.textContent = P().overlay_locked === false ? "Lock" : "Unlock to move";
        }),
      ),
    },
    { title: "Under each line", desc: "Romanised text for non-Latin scripts, a translation, or both. Translations come from Google Translate and are kept for replays." },
    { full: seg([["Off", "off"], ["Romanised", "rom"], ["Translated", "tr"], ["Both", "both"]], () => String(P().lyric_subline || "off"), (k) => setPref("lyric_subline", k), true) },
    {
      title: "Translate to",
      ctl: group(
        btn("‹", () => void stepLang(-1)),
        value(() => {
          const c = String(P().translate_to || "auto");
          return c === "auto" ? `Auto (${langName(sysLang())})` : langName(c);
        }, 120),
        btn("›", () => void stepLang(1)),
      ),
    },
  ]);

  // ── Appearance ──
  const swatch = h("input", "set-swatch");
  swatch.type = "color";
  swatch.title = "Choose accent colour";
  swatch.addEventListener("input", () => setPrefs({ accent_color: swatch.value }, true));
  swatch.addEventListener("change", () => void setPref("accent_color", swatch.value).then(syncAll, fail));
  reg(swatch, (el) => {
    if (document.activeElement !== el) el.value = String(P().accent_color || "#1db954");
  });
  const stepFont = async (d: number) => {
    const i = Math.max(0, fonts.indexOf(String(P().lyric_font)));
    await setPref("lyric_font", fonts[(i + d + fonts.length) % fonts.length]).catch(fail);
    syncAll();
  };
  // The name is drawn in its own font, so a change shows at once.
  const fontName = value(() => String(P().lyric_font || "Segoe UI"), 110);
  reg(fontName, (el) => (el.style.fontFamily = `"${String(P().lyric_font || "Segoe UI")}", "Segoe UI", sans-serif`));
  const SIZES: [string, number][] = [["Small", -2], ["Default", 0], ["Large", 4], ["Huge", 8]];
  const nearestSize = () => {
    const b = Number(P().lyric_font_boost) || 0;
    return String(SIZES.reduce((a, s) => (Math.abs(s[1] - b) < Math.abs(a[1] - b) ? s : a))[1]);
  };
  const appearance = card([
    { title: "Theme", ctl: seg([["Dark", "dark"], ["Light", "light"]], () => (P().dark_mode === false ? "light" : "dark"), (k) => setPref("dark_mode", k === "dark")) },
    { title: "Colours from the album art", desc: "The window and the moving background take their colours from the cover.", ctl: prefSwitch("album_tint") },
    { title: "Accent colour", desc: "Used when album colours are off.", ctl: swatch },
    { title: "Motion", desc: "Moving background, gliding lyrics and animated controls. Turn off to save CPU or reduce motion.", ctl: prefSwitch("animations") },
    {
      title: "Performance",
      desc: "Auto measures how long each frame takes and eases off on slower PCs. Fast keeps the background still.",
      ctl: seg([["Auto", "auto"], ["Smooth", "high"], ["Fast", "low"]], () => String(P().render_quality || "auto"), (k) => setPref("render_quality", k)),
    },
    {
      title: "Lyric font",
      desc: "Japanese, Korean, emoji and other scripts keep their own fonts.",
      ctl: group(btn("‹", () => void stepFont(-1)), fontName, btn("›", () => void stepFont(1))),
    },
    { title: "Lyric size", ctl: seg(SIZES.map(([n, v]) => [n, String(v)] as [string, string]), nearestSize, (k) => setPref("lyric_font_boost", Number(k))) },
    { title: "React to the beat", desc: "The background swells gently on each beat when Spotify shares beat data. Smooth quality only.", ctl: prefSwitch("beat_react") },
  ]);

  // ── Playback: sleep timer ──
  const customInput = h("input", "set-input narrow");
  customInput.type = "text";
  customInput.value = "45";
  customInput.inputMode = "numeric";
  customInput.setAttribute("aria-label", "Minutes");
  const customMsg = h("span", "set-hint", "1–600 minutes");
  const startCustom = async () => {
    const m = parseMinutes(customInput.value);
    if (m === null) {
      customMsg.textContent = "Use 1–600 minutes";
      customMsg.classList.add("bad");
      customInput.focus();
      customInput.select();
      return;
    }
    customInput.value = String(m);
    sleepChoice = String(m);
    sleepCustomOpen = false;
    try {
      await call("core", "sleep_set", { value: m });
    } catch (e) {
      fail(e);
    }
    syncAll();
  };
  customInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") void startCustom();
    if (e.key === "Escape") {
      sleepCustomOpen = false;
      syncAll();
    }
  });
  const customRow = reg(h("div", "set-custom", customInput, h("span", "set-hint", "min"), btn("Start", () => void startCustom(), "primary"), customMsg), (el) => {
    el.hidden = !sleepCustomOpen;
    if (!sleepCustomOpen) {
      customMsg.textContent = "1–600 minutes";
      customMsg.classList.remove("bad");
    }
  });
  const sleepSeg = seg(
    [["Off", "off"], ["15m", "15"], ["30m", "30"], ["1h", "60"], ["Song", "eos"], ["Custom", "custom"]],
    sleepKey,
    async (k) => {
      if (k === "custom") {
        sleepCustomOpen = true;
        syncAll();
        customInput.focus();
        customInput.select();
        return;
      }
      sleepCustomOpen = false;
      sleepChoice = k === "off" ? null : k;
      await call("core", "sleep_set", { value: k === "off" ? null : k === "eos" ? "eos" : parseInt(k, 10) });
    },
    true,
  );
  const playback = card([
    {
      title: "Sleep timer",
      desc: "Pause Spotify after a while, or when this song ends (Song). Forgotten when Statusify closes.",
      ctl: value(() => String(core().sleep_label || "Off"), 72, () => !!core().sleep_label),
    },
    { full: sleepSeg },
    { full: customRow },
  ]);

  // ── Window and startup ──
  const windowCard = card([
    { title: "Always on top", desc: "Ctrl+T", ctl: prefSwitch("always_on_top") },
    { title: "Close to the tray", desc: "Closing the window keeps Statusify and your Discord status running.", ctl: prefSwitch("close_to_tray") },
    { title: "Start minimised to the tray", ctl: prefSwitch("start_minimized") },
    {
      title: "Launch when Windows starts",
      desc: "Starts Statusify when you sign in. This switch is separate from the old Python Statusify's.",
      ctl: sw(() => !!shell.autostart, async (v) => {
        const before = !!shell.autostart;
        shell.autostart = v;
        try {
          shell.autostart = await call("shell", "set_autostart", { enabled: v });
        } catch (e) {
          shell.autostart = before; // the change did not happen; show the truth
          throw e;
        }
      }),
    },
    {
      title: "The old Statusify also starts with Windows",
      desc: "Both would start at sign-in and compete for the connection to Spotify; only one can work at a time.",
      ctl: btn("Turn off the old one", () => void call("shell", "remove_legacy_startup").then((r: any) => {
        shell.legacy_startup = !!r?.legacy_startup;
        toast(r?.removed ? "The old Statusify will no longer start with Windows" : "It was already off");
        syncAll();
      }, fail)),
    },
    { title: "Window position", desc: "Move the window back to the centre of the screen.", ctl: btn("Centre", () => void call("shell", "center_window").catch(fail)) },
    { title: "Desktop shortcut", ctl: btn("Create", () => void call("shell", "create_shortcut").then(() => toast("Shortcut created on the Desktop"), fail)) },
  ]);
  const legacyRow = [...windowCard.querySelectorAll<HTMLElement>(".set-row")].find((r) => r.textContent?.startsWith("The old Statusify"));
  if (legacyRow) reg(legacyRow, (el) => (el.hidden = !shell.legacy_startup));

  // ── Discord ──
  let selectedProfile = "";
  const profiles = (): { name: string; app_id: string; active: boolean }[] => shell.profiles ?? [];
  const list = reg(h("div", "set-list"), (el) => {
    el.replaceChildren();
    if (!profiles().length) {
      el.append(h("div", "item empty", "No saved profiles"));
      return;
    }
    for (const p of profiles()) {
      const it = h("div", "item", h("span", "name", p.name), p.active ? h("span", "tick", "✓") : null, h("span", "id", p.app_id));
      it.setAttribute("role", "option");
      it.classList.toggle("sel", p.name === selectedProfile);
      it.addEventListener("click", () => {
        selectedProfile = p.name;
        syncAll();
      });
      el.append(it);
    }
  });
  const selected = () => profiles().find((p) => p.name === selectedProfile);
  const merge = (r: Promise<any>) =>
    r.then((s) => {
      if (s && typeof s === "object") shell = { ...shell, ...s };
      syncAll();
    }, fail);
  const reconnect = async () => {
    try {
      await call("shell", "reconnect_rpc");
      toast("Reconnecting to Discord…");
    } catch (e) {
      fail(e);
    }
    void refreshShell();
  };
  const appIdBtn = reg(btn("Add…", () => appIdDialog(false)), (b) => (b.textContent = shell.app_id_set ? "Change…" : "Add…"));
  const discord = card([
    {
      title: "Application ID",
      desc: () => (shell.active_app_id ? `In use: ${shell.active_app_id}. A change applies at once, no restart needed.` : "None yet, so your status stays off. Create one in the Discord Developer Portal."),
      ctl: appIdBtn,
    },
    {
      title: "Connection",
      desc: "Reconnect if your status stopped updating.",
      ctl: group(
        btn("Test", () => void call("core", "test_presence").then(() => toast("Test sent. Check your Discord profile."), fail)),
        btn("Reconnect", () => void reconnect()),
      ),
    },
    { title: "Show when paused", desc: 'Keep the status up with a "Paused" state.', ctl: prefSwitch("show_paused_rpc") },
    { title: "Song in the member list", desc: "Show the song under your name instead of the app name.", ctl: prefSwitch("status_shows_song") },
    { title: "Link the song to Spotify", desc: "Clicking the title opens the track.", ctl: prefSwitch("link_track") },
    { title: "Show 'Listen on Spotify' button", desc: "Friends can open the track from your profile. Discord hides it from you.", ctl: prefSwitch("listen_button") },
    { title: "Instrumental text", desc: "Shown on Discord between sung lines.", ctl: field(() => String(P().instrumental_text ?? ""), (v) => setPref("instrumental_text", v), "wide") },
    { title: "App profiles", desc: "Save several Discord App IDs and switch between them." },
    { full: list },
    {
      full: h("div", "set-btnrow",
        btn("Add…", addProfileDialog),
        btn("Switch to selected", () => {
          const p = selected();
          if (p) void merge(call("shell", "switch_profile", { app_id: p.app_id })).then(() => toast(`Switched to ${p.name}. Reconnecting to Discord…`));
        }),
        h("span", "grow"),
        btn("Delete", () => {
          const p = selected();
          if (!p) return;
          selectedProfile = "";
          void merge(call("shell", "delete_profile", { name: p.name }));
        }, "ghost")),
    },
  ]);

  // ── Hotkeys ──
  const hotkeyRow = (title: string, name: string): Row => ({
    title,
    ctl: field(() => String(shell.hotkeys?.[name] ?? ""), async (v) => {
      const r = await call<{ errors: Record<string, string> }>("shell", "set_hotkeys", { [name]: v });
      shell.hotkey_errors = r.errors;
      shell.hotkeys = { ...shell.hotkeys, [name]: v.trim() };
    }, "wide", "e.g. ctrl+alt+n"),
    note: () => String(shell.hotkey_errors?.[name] || ""),
  });
  const hotkeys = card([
    hotkeyRow("Skip track", "skip"),
    hotkeyRow("Skip instrumental", "skip_instr"),
    hotkeyRow("Pause Discord status", "toggle"),
    hotkeyRow("Lyrics overlay on/off", "overlay"),
  ]);

  // ── History and privacy ──
  const bl = h("textarea", "set-textarea");
  bl.rows = 4;
  bl.spellcheck = false;
  let blTimer = 0;
  let blShown = "";
  const saveBl = async () => {
    clearTimeout(blTimer);
    if (bl.value === blShown) return;
    blShown = bl.value;
    try {
      await call("settings", "set", { key: "blacklist", value: bl.value });
    } catch (e) {
      fail(e);
    }
  };
  bl.addEventListener("input", () => {
    clearTimeout(blTimer);
    blTimer = window.setTimeout(saveBl, 700);
  });
  bl.addEventListener("blur", saveBl);
  reg(bl, (el) => {
    if (document.activeElement === el) return;
    blShown = String(P().blacklist ?? "");
    el.value = blShown;
  });
  const privacy = card([
    { title: "Remember history", desc: "Keep plays and lyrics between sessions.", ctl: prefSwitch("save_history") },
    { title: "Blacklist", desc: "Songs whose artist or title contains one of these terms never show on Discord. One term per line." },
    { full: bl },
  ]);

  // ── Spotify lyrics ──
  const spotify = card([
    {
      title: "Lyrics connection",
      desc: () => (snap?.bridge_connected ? "Statusify is reading Spotify through Spicetify." : "Statusify reads Spotify, and gets the lyrics, through Spicetify."),
      ctl: value(() => (snap?.bridge_connected ? "Connected" : "Not connected"), 100, () => !!snap?.bridge_connected),
    },
    {
      title: "Set up or repair",
      desc: "Installs Spicetify if it is missing and adds Statusify's lyrics bridge. Spotify restarts once. Needs the Spotify desktop app, not the Microsoft Store version.",
      ctl: btn("Set up…", () => spotifySetupDialog(false)),
    },
  ]);

  // ── Updates ──
  const checkBtn = reg(btn("Check now", () => void checkNow()), (b) => {
    b.disabled = checking;
    b.textContent = checking ? "Checking…" : "Check now";
  });
  const newsBtn = reg(btn("What's new", () => {
    const u = update();
    if (u) updateDialog(u);
  }), (b) => (b.hidden = !update()));
  const updates = card([
    {
      title: "Check for updates",
      desc: () => `${youHave()} ${updateDescription(update(), updateNote)}`.trim(),
      ctl: group(newsBtn, checkBtn),
    },
  ]);

  // ── Diagnostics ──
  logBox = h("div", "set-log");
  logBox.tabIndex = 0;
  const logBtn = btn("Show", () => {
    const hide = logShown();
    const now: string[] = (P().collapsed ?? ["log"]).filter((x: string) => x !== "log");
    setPrefs({ collapsed: hide ? [...now, "log"] : now }, true);
    void call("settings", "set_collapsed", { id: "log", collapsed: hide }).catch(fail);
    syncAll();
    if (!hide) void pollLog(true);
  });
  const logCard = reg(h("div", "set-log-card", logBox), (el) => {
    el.hidden = !logShown();
    logBtn.textContent = logShown() ? "Hide" : "Show";
  });
  const diag = h("div", "set-card-wrap", card([
    {
      title: "Data folder",
      desc: () => `${shell.data_dir ?? ""}${shell.data_dir_label ? `  ·  ${shell.data_dir_label}` : ""}`,
      ctl: btn("Open", () => void call("shell", "open_data_dir").catch(fail)),
    },
    { title: "Log", desc: "What Statusify has been doing. Useful when something's wrong.", ctl: logBtn },
  ]), logCard);

  page.append(
    section("Lyrics"), lyricsCard,
    section("Appearance"), appearance,
    section("Playback"), playback,
    section("Window and startup"), windowCard,
    section("Discord"), discord,
    section("Spotify lyrics", "Lyrics and what's playing come from the Spotify desktop app, through Spicetify."), spotify,
    section("Global hotkeys", "Work while other apps have focus. Saved when you press Enter or click away."), hotkeys,
    section("History and privacy"), privacy,
    section("Updates"), updates,
    section("Diagnostics"), diag,
    reg(h("p", "set-foot"), (e) => (e.textContent = `Statusify ${shell.version ?? ""}  ·  data in ${shell.data_dir ?? ""}`)),
    toastEl,
  );
  return page;
}

// ── dialogs that need page state ─────────────────────────────────
function addProfileDialog() {
  const name = h("input", "set-input block");
  name.placeholder = "e.g. Main";
  const id = h("input", "set-input block");
  id.placeholder = "e.g. 123456789012345678";
  id.inputMode = "numeric";
  const err = h("div", "set-dialog-err");
  dialog("Add a Discord profile", [h("label", "set-label", "Name", name), h("label", "set-label", "Application ID", id), err], [
    { text: "Cancel", kind: "ghost" },
    {
      text: "Save",
      kind: "primary",
      run: async () => {
        try {
          shell = { ...shell, ...(await call("shell", "add_profile", { name: name.value, app_id: id.value })) };
          syncAll();
        } catch (e) {
          err.textContent = String(e);
          return false;
        }
      },
    },
  ]);
}

/**
 * The Discord Application ID, which Statusify shows your music through. On the
 * very first run it opens by itself ("Welcome"), and once the ID is saved it
 * goes on to the Spotify step.
 */
function appIdDialog(firstRun: boolean) {
  const id = h("input", "set-input block");
  id.placeholder = "e.g. 123456789012345678";
  id.inputMode = "numeric";
  id.value = String(shell.active_app_id ?? "");
  const err = h("div", "set-dialog-err");
  const link = h("a", "set-link", "Open the Discord Developer Portal ↗");
  link.href = "#";
  link.addEventListener("click", (e) => {
    e.preventDefault();
    void openUrl("https://discord.com/developers/applications").catch(() => undefined);
  });
  const steps = h("ol", "set-steps",
    h("li", "", "In the Developer Portal, click New Application."),
    h("li", "", 'Name it what your status should say, for example "Spotify". Discord shows "Listening to Spotify".'),
    h("li", "", "Open General Information, copy the Application ID and paste it below."));
  dialog(firstRun ? "Welcome to Statusify" : "Discord Application ID", [
    h("p", "set-dialog-text", firstRun
      ? "Statusify shows the song you're playing, with synced lyrics, on your Discord profile. First, Discord needs an application to show it through. It's free and takes a minute."
      : "Statusify shows your music through a Discord application of your own."),
    steps,
    link,
    id,
    err,
    h("p", "set-dialog-text small", "The Discord desktop app has to be running for your status to appear."),
  ], [
    { text: firstRun ? "Later" : "Cancel", kind: "ghost" },
    {
      text: "Save and continue",
      kind: "primary",
      run: async () => {
        try {
          shell = { ...shell, ...(await call("shell", "set_app_id", { app_id: id.value })) };
          syncAll();
        } catch (e) {
          err.textContent = String(e);
          return false;
        }
        // The connection starts straight away; no restart needed.
        window.setTimeout(() => toast("Saved. Connecting to Discord…"), 50);
        if (firstRun && !snap?.bridge_connected) window.setTimeout(() => spotifySetupDialog(true), 400);
      },
    },
  ]);
}

/** Spicetify and the lyrics bridge: what it does, then run the setup script in its own window. */
function spotifySetupDialog(firstRun: boolean) {
  const err = h("div", "set-dialog-err");
  dialog(firstRun ? "Now connect Spotify" : "Set up Spotify lyrics", [
    h("p", "set-dialog-text", "Statusify reads what's playing, and gets the lyrics, from the Spotify desktop app through Spicetify."),
    h("p", "set-dialog-text", "Setup installs Spicetify if it's missing and adds Statusify's lyrics bridge. A window shows its progress, and Spotify restarts once at the end. It needs the Spotify app from spotify.com, not the Microsoft Store version."),
    err,
  ], [
    { text: firstRun ? "Not now" : "Cancel", kind: "ghost" },
    {
      text: "Set up (restarts Spotify)",
      kind: "primary",
      run: async () => {
        try {
          await call("core", "repair_bridge");
        } catch (e) {
          err.textContent = String(e);
          return false;
        }
        window.setTimeout(() => toast("Setup started. Follow the window that just opened."), 50);
      },
    },
  ]);
}

/** True while a key press belongs to a text field (or similar), not to the app. */
function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  if (!t || !t.tagName) return false;
  if (t.isContentEditable) return true;
  if (t.tagName === "TEXTAREA" || t.tagName === "SELECT") return true;
  if (t.tagName === "INPUT") return !["checkbox", "button", "submit", "color"].includes((t as HTMLInputElement).type);
  return false;
}

/** Python's volume step: 5 %, kept on the 5 % grid. */
export function volumeStep(v: number, dir: 1 | -1): number {
  return Math.min(1, Math.max(0, Math.round((v + dir * 0.05) * 20) / 20));
}

/** Where a seek key lands: never past one second before the end (Python's _key_seek). */
export function seekTarget(pos: number, delta: number, dur: number): number {
  return Math.max(0, Math.min(dur - 1000, pos + delta));
}

// Window-scoped keys, ported from App._bind_shortcuts. They only fire while Statusify has
// focus (the global hotkeys are separate), so plain keys are safe, but never while typing.
function bindKeys() {
  const pages = ["now", "history", "stats", "settings"];
  const tab = (id: string) => document.querySelector<HTMLElement>(`.tab[data-page="${id}"]`);
  const quiet = (p: Promise<unknown>) => void p.catch(() => undefined);
  window.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.isComposing) return;
    const ctrl = e.ctrlKey && !e.altKey && !e.metaKey;
    const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const used = () => e.preventDefault();

    if (ctrl && !e.shiftKey && key >= "1" && key <= "4") {
      used();
      tab(pages[Number(key) - 1])?.click();
    } else if (ctrl && key === "t") {
      used();
      void setPref("always_on_top", !P().always_on_top).catch(fail);
    } else if (ctrl && key === "m") {
      used();
      quiet(call("windows", "toggle_mini"));
    } else if (ctrl && key === "f") {
      used();
      tab("history")?.click();
      window.setTimeout(() => {
        const box = document.querySelector<HTMLInputElement>('#page-history input[type="search"]');
        box?.focus();
        box?.select();
      }, 30);
    } else if (e.key === "F11" && !e.ctrlKey && !e.altKey) {
      used();
      quiet(call("shell", "toggle_fullscreen"));
    } else if (e.key === "Escape" && plain) {
      // Dialogs and panels close themselves first; this only leaves fullscreen.
      if (!document.querySelector(".set-modal")) quiet(call("shell", "exit_fullscreen"));
    } else if (ctrl && key === "c") {
      // Copy the current lyric, unless something is selected (then it is a normal copy).
      const line = String(core().line ?? "").trim();
      if (!typing(e) && !window.getSelection()?.toString() && line) {
        used();
        void navigator.clipboard?.writeText(line).catch(() => undefined);
      }
    } else if (typing(e)) {
      return;
    } else if (plain && key === " ") {
      // A focused button keeps Space for itself (it clicks it).
      if ((e.target as HTMLElement).closest?.("button, a, [role=switch], summary")) return;
      used();
      void player("toggle");
    } else if (ctrl && key === "ArrowLeft") {
      used();
      void player("prev");
    } else if (ctrl && key === "ArrowRight") {
      used();
      void player("next");
    } else if (ctrl && (key === "ArrowUp" || key === "ArrowDown")) {
      used();
      const v = Number(snap?.extras?.player?.volume ?? 1);
      quiet(call("nowplaying", "set_volume", { value: volumeStep(Number.isFinite(v) ? v : 1, key === "ArrowUp" ? 1 : -1) }));
    } else if (ctrl && (key === "s" || key === "r" || key === "l")) {
      used();
      void player(key === "s" ? "shuffle" : key === "r" ? "repeat" : "like");
    } else if (plain && (key === "ArrowLeft" || key === "ArrowRight")) {
      // Seeking only makes sense on Now Playing.
      if (document.body.dataset.page !== "now" || !snap || snap.duration_ms <= 0) return;
      used();
      void seek(seekTarget(position(snap), key === "ArrowLeft" ? -5000 : 5000, snap.duration_ms));
    }
  });
}

async function refreshShell() {
  try {
    shell = await call("shell", "get_state");
  } catch {
    /* the shell feature is not up yet */
  }
  syncAll();
}

export function mount(root: HTMLElement) {
  root.append(build());
  onPrefs(syncAll);
  onSnapshot((s) => {
    snap = s;
    // The shell pushes small facts on every change; mirror them without a round trip.
    if (s.extras?.shell) shell = { ...shell, ...s.extras.shell };
    // The timer fired (or was cancelled elsewhere): Python closes Custom and forgets the choice.
    const label = String(core().sleep_label || "");
    if (lastSleepLabel && !label) {
      sleepCustomOpen = false;
      sleepChoice = null;
    }
    lastSleepLabel = label;
    // A newer release opens its dialog by itself, once per version (not while
    // another dialog is up, and not again after Later or Escape).
    const u = update();
    if (u && u.tag !== autoShownTag && shouldAutoShow(u, dismissedTag()) && !document.querySelector(".set-modal")) {
      autoShownTag = u.tag;
      updateDialog(u);
    }
    // The controls are brought up to date on show(); off screen, every
    // snapshot (twice a second while playing) would re-run every one of
    // them for nothing.
    if (visible) syncAll();
  });
  call<{ code: string; name: string }[]>("settings", "languages").then((l) => (languages = l)).finally(syncAll).catch(() => undefined);
  call<string[]>("settings", "fonts").then((f) => (fonts = f)).finally(syncAll).catch(() => undefined);
  void refreshPrefs();
  void refreshShell().then(() => {
    if (shell.app_id_set === false) appIdDialog(true);
  });
  bindKeys();
}

export function show() {
  visible = true;
  syncAll();
  void refreshPrefs();
  void refreshShell();
  void pollLog(true);
  window.clearInterval(logTimer);
  logTimer = window.setInterval(() => void pollLog(), 1500);
}

export function hide() {
  visible = false;
  window.clearInterval(logTimer);
}
