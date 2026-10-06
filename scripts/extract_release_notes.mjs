#!/usr/bin/env node
// Prints the README's "What's New in vX.Y.Z" section: the body of the GitHub
// release for that tag. Fails (exit 1) rather than publish a release with
// missing, empty or unfinished notes.
//
//   node scripts/extract_release_notes.mjs <vX.Y.Z | X.Y.Z> [--readme README.md] [--out notes.md]
//
// The section runs from its heading to the next horizontal rule (---) or the
// next heading of the same or a higher level, whichever comes first. Lines
// inside fenced code blocks never end it. A heading may start with an emoji.
//
// The release page renders the text somewhere other than the README, so the
// section must stand on its own. These make the script fail:
//   * no section for that version, or more than one
//   * an empty section
//   * an unresolved {{PLACEHOLDER}} (the README keeps {{BRIDGE_NUMBERS}} until
//     the measured numbers are filled in)
//   * a relative link or image (docs/x.png, #anchor): it would break on the
//     release page, so links in this section must be absolute URLs
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** "v3.0.0" or "3.0.0" -> "3.0.0". Anything else (v3.0, v3.0.0-rc1, refs/tags/...) is an error. */
export function normalizeVersion(arg) {
  const m = /^v?(\d+\.\d+\.\d+)$/.exec(String(arg ?? "").trim());
  if (!m) throw new Error(`"${arg}" is not a release version like v3.0.0`);
  return m[1];
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Heading text without its leading emoji, symbols and spaces. */
const headingText = (t) => t.replace(/^[^\p{L}\p{N}]+/u, "").trim();

/** Fenced-code state per line: true on the fence lines and everything between them. */
function codeMask(lines) {
  const mask = [];
  let fence = null;
  for (const line of lines) {
    const m = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      mask.push(true);
      if (m && m[1][0] === fence[0] && m[1].length >= fence.length && /^\s{0,3}[`~]+\s*$/.test(line)) fence = null;
    } else if (m) {
      fence = m[1];
      mask.push(true);
    } else {
      mask.push(false);
    }
  }
  return mask;
}

/** Every "What's New in vX.Y.Z" heading in a README: [{ version, line, level }]. */
export function whatsNewHeadings(markdown) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const code = codeMask(lines);
  const out = [];
  lines.forEach((line, i) => {
    if (code[i]) return;
    const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (!h) return;
    const m = /^What['’]s New in v(\d+\.\d+\.\d+)$/i.exec(headingText(h[2]));
    if (m) out.push({ version: m[1], line: i, level: h[1].length });
  });
  return out;
}

/**
 * The raw text of the "What's New in v<version>" section, trimmed. Throws when
 * there is no such section, more than one, or it is empty. It does not apply
 * the publishing checks (see releaseNotesProblems).
 */
export function extractSection(markdown, version) {
  const want = normalizeVersion(version);
  const heads = whatsNewHeadings(markdown);
  const mine = heads.filter((h) => h.version === want);
  if (mine.length === 0) {
    const have = heads.length ? heads.map((h) => `v${h.version}`).join(", ") : "none";
    throw new Error(`README.md has no "What's New in v${want}" section (found: ${have})`);
  }
  if (mine.length > 1) throw new Error(`README.md has ${mine.length} "What's New in v${want}" sections; there must be one`);

  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const code = codeMask(lines);
  const { line: start, level } = mine[0];
  const body = [];
  for (let i = start + 1; i < lines.length; i++) {
    if (!code[i]) {
      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(lines[i])) break;
      const h = /^(#{1,6})\s+\S/.exec(lines[i]);
      if (h && h[1].length <= level) break;
    }
    body.push(lines[i]);
  }
  const text = body.join("\n").trim();
  if (!text) throw new Error(`The "What's New in v${want}" section of README.md is empty`);
  return text;
}

/** What is wrong with `notes` as release-page text: a list of messages, empty when it is fine. */
export function releaseNotesProblems(notes) {
  const problems = [];
  const lines = notes.split("\n");
  const code = codeMask(lines);
  const prose = lines.filter((_, i) => !code[i]).join("\n");

  const placeholders = [...new Set(notes.match(/\{\{[^}\n]*\}\}/g) ?? [])];
  if (placeholders.length) problems.push(`unresolved placeholder${placeholders.length > 1 ? "s" : ""}: ${placeholders.join(", ")}`);

  const relative = new Set();
  const absolute = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;
  for (const m of prose.matchAll(/!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)/g)) if (!absolute.test(m[1])) relative.add(m[1]);
  for (const m of prose.matchAll(/\b(?:src|href)\s*=\s*["']([^"']+)["']/gi)) if (!absolute.test(m[1])) relative.add(m[1]);
  if (relative.size) problems.push(`relative link${relative.size > 1 ? "s" : ""} (use absolute URLs, the release page is not the README): ${[...relative].join(", ")}`);
  return problems;
}

/** The release body for `version`, or an Error saying exactly what to fix in the README. */
export function releaseNotes(markdown, version) {
  const text = extractSection(markdown, version);
  const problems = releaseNotesProblems(text);
  if (problems.length) {
    throw new Error(`The "What's New in v${normalizeVersion(version)}" section is not ready to publish: ${problems.join("; ")}`);
  }
  return `${text}\n`;
}

function main(argv) {
  let tag = null;
  let readme = resolve(dirname(fileURLToPath(import.meta.url)), "..", "README.md");
  let out = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--readme") readme = resolve(argv[++i] ?? "");
    else if (a === "--out") out = resolve(argv[++i] ?? "");
    else if (!a.startsWith("--") && tag === null) tag = a;
    else {
      console.error(`unknown argument ${a}\nusage: extract_release_notes.mjs <vX.Y.Z> [--readme README.md] [--out notes.md]`);
      return 2;
    }
  }
  if (tag === null) {
    console.error("usage: extract_release_notes.mjs <vX.Y.Z> [--readme README.md] [--out notes.md]");
    return 2;
  }
  try {
    const notes = releaseNotes(readFileSync(readme, "utf8"), tag);
    if (out) writeFileSync(out, notes);
    else process.stdout.write(notes);
    return 0;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // In Actions this also shows up as an annotation on the run.
    if (process.env.GITHUB_ACTIONS) console.error(`::error title=Release notes::${msg}`);
    console.error(`extract_release_notes: ${msg}`);
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
