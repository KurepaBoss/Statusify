// node --test: scripts/extract_release_notes.mjs, which turns the README's
// "What's New in vX.Y.Z" section into the GitHub release body.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { extractSection, normalizeVersion, releaseNotes, releaseNotesProblems, whatsNewHeadings } from "../scripts/extract_release_notes.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(REPO, "scripts", "extract_release_notes.mjs");

const README = `# Title

## Overview
Words.

---

## 🆕 What's New in v3.0.0

**Rewritten.** Faster.

- one
- two with [a link](https://example.com/x)

---

## Features
- a
`;

test("normalizeVersion accepts v3.0.0 and 3.0.0 and nothing else", () => {
  assert.equal(normalizeVersion("v3.0.0"), "3.0.0");
  assert.equal(normalizeVersion("3.10.12"), "3.10.12");
  assert.equal(normalizeVersion(" v3.0.0 "), "3.0.0", "surrounding whitespace is tolerated");
  for (const bad of ["v3.0", "v3.0.0-rc1", "refs/tags/v3.0.0", "", undefined, "vx.y.z", "v3.0.0.1"]) {
    assert.throws(() => normalizeVersion(bad), /not a release version/, String(bad));
  }
});

test("the section runs from its heading to the next rule, emoji and all", () => {
  const body = extractSection(README, "v3.0.0");
  assert.equal(body, "**Rewritten.** Faster.\n\n- one\n- two with [a link](https://example.com/x)");
  assert.ok(!body.includes("Features"));
});

test("CRLF READMEs give the same text", () => {
  assert.equal(extractSection(README.replace(/\n/g, "\r\n"), "3.0.0"), extractSection(README, "3.0.0"));
});

test("with no rule the next heading of the same or higher level ends it; a deeper one does not", () => {
  const md = "## What's New in v1.2.3\nintro\n### Details\nmore\n## Next\nout\n";
  assert.equal(extractSection(md, "1.2.3"), "intro\n### Details\nmore");
  const last = "## What's New in v1.2.3\nonly line\n";
  assert.equal(extractSection(last, "1.2.3"), "only line");
});

test("a rule or heading inside a fenced code block does not end the section", () => {
  const md = "## What's New in v1.0.0\nrun:\n```\n---\n## not a heading\n```\nafter\n\n---\nout\n";
  assert.equal(extractSection(md, "1.0.0"), "run:\n```\n---\n## not a heading\n```\nafter");
  // ...and a heading-looking line in a fence is not a section start either.
  assert.deepEqual(whatsNewHeadings("```\n## What's New in v9.9.9\n```\n"), []);
});

test("a missing version fails and names the versions that exist", () => {
  assert.throws(() => extractSection(README, "v2.2.0"), /no "What's New in v2\.2\.0" section \(found: v3\.0\.0\)/);
  assert.throws(() => extractSection("# nothing here\n", "v3.0.0"), /found: none/);
});

test("v3.0.1 and v3.0.10 are not mistaken for each other", () => {
  const md = "## What's New in v3.0.10\nten\n---\n## What's New in v3.0.1\none\n---\n";
  assert.equal(extractSection(md, "v3.0.1"), "one");
  assert.equal(extractSection(md, "v3.0.10"), "ten");
  assert.throws(() => extractSection(md, "v3.0.0"), /no "What's New in v3\.0\.0"/);
});

test("two sections for one version and an empty section both fail", () => {
  const twice = "## What's New in v1.0.0\na\n---\n## What's New in v1.0.0\nb\n";
  assert.throws(() => extractSection(twice, "1.0.0"), /2 "What's New in v1\.0\.0" sections/);
  assert.throws(() => extractSection("## What's New in v1.0.0\n\n---\ntext\n", "1.0.0"), /is empty/);
});

test("an unresolved placeholder blocks publishing", () => {
  const md = "## What's New in v3.0.0\nSteadier. {{BRIDGE_NUMBERS}}\n---\n";
  assert.equal(extractSection(md, "3.0.0"), "Steadier. {{BRIDGE_NUMBERS}}");
  assert.throws(() => releaseNotes(md, "3.0.0"), /not ready to publish: unresolved placeholder: \{\{BRIDGE_NUMBERS\}\}/);
  assert.deepEqual(releaseNotesProblems("Steadier. 120 ms to 40 ms."), []);
});

test("relative links and images block publishing, absolute ones do not", () => {
  assert.deepEqual(releaseNotesProblems("[x](https://a.b/c) ![y](https://a.b/i.png) <a href=\"https://a.b\">z</a> [m](mailto:a@b.c)"), []);
  const bad = releaseNotesProblems("![shot](docs/preview.png) see [below](#install) and <img src=\"docs/x.png\"> and [ok](https://a.b)");
  assert.equal(bad.length, 1);
  for (const rel of ["docs/preview.png", "#install", "docs/x.png"]) assert.ok(bad[0].includes(rel), `${rel} in ${bad[0]}`);
  assert.ok(!bad[0].includes("https://a.b"));
  // Text inside a code block is not a link.
  assert.deepEqual(releaseNotesProblems("```\n[x](docs/y.png)\n```"), []);
});

test("releaseNotes returns the body with one trailing newline", () => {
  assert.equal(releaseNotes(README, "v3.0.0"), "**Rewritten.** Faster.\n\n- one\n- two with [a link](https://example.com/x)\n");
});

function cli(args, readme) {
  const dir = mkdtempSync(join(tmpdir(), "statusify-notes-"));
  try {
    const file = join(dir, "README.md");
    writeFileSync(file, readme);
    const out = join(dir, "notes.md");
    const r = spawnSync(process.execPath, [SCRIPT, ...args.map((a) => (a === "<readme>" ? file : a === "<out>" ? out : a))], { encoding: "utf8" });
    let written = null;
    try {
      written = readFileSync(out, "utf8");
    } catch {
      /* not written */
    }
    return { status: r.status, stdout: r.stdout, stderr: r.stderr, written };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("CLI: prints the notes, or writes them with --out", () => {
  const a = cli(["v3.0.0", "--readme", "<readme>"], README);
  assert.equal(a.status, 0, a.stderr);
  assert.equal(a.stdout, "**Rewritten.** Faster.\n\n- one\n- two with [a link](https://example.com/x)\n");
  const b = cli(["3.0.0", "--readme", "<readme>", "--out", "<out>"], README);
  assert.equal(b.status, 0, b.stderr);
  assert.equal(b.stdout, "");
  assert.equal(b.written, a.stdout);
});

test("CLI: fails loudly and writes nothing for a missing section, a placeholder or a bad tag", () => {
  const missing = cli(["v9.9.9", "--readme", "<readme>", "--out", "<out>"], README);
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /no "What's New in v9\.9\.9" section/);
  assert.equal(missing.written, null);

  const placeholder = cli(["v3.0.0", "--readme", "<readme>", "--out", "<out>"], README.replace("Faster.", "Faster. {{BRIDGE_NUMBERS}}"));
  assert.equal(placeholder.status, 1);
  assert.match(placeholder.stderr, /unresolved placeholder: \{\{BRIDGE_NUMBERS\}\}/);
  assert.equal(placeholder.written, null);

  const badTag = cli(["not-a-tag", "--readme", "<readme>"], README);
  assert.equal(badTag.status, 1);
  assert.match(badTag.stderr, /not a release version/);

  assert.equal(cli([], README).status, 2, "no tag given");
  assert.equal(cli(["v3.0.0", "--bogus"], README).status, 2, "unknown flag");
});

test("this repository's README: header, section and links are consistent", () => {
  const readme = readFileSync(join(REPO, "README.md"), "utf8");
  const header = /<h1>\s*Statusify v(\d+\.\d+\.\d+)\s*<\/h1>/.exec(readme);
  assert.ok(header, "README has the <h1>Statusify vX.Y.Z</h1> header");
  const version = header[1];

  const heads = whatsNewHeadings(readme);
  assert.equal(heads.length, 1, "the README keeps only the latest release's What's New");
  assert.equal(heads[0].version, version, "the What's New section is for the version in the header");

  const section = extractSection(readme, version);
  assert.ok(section.length > 200, "the section has real content");
  // Everything wrong with it except the one placeholder the merge step fills in is a bug now.
  const problems = releaseNotesProblems(section).filter((p) => !/^unresolved placeholder: \{\{BRIDGE_NUMBERS\}\}$/.test(p));
  assert.deepEqual(problems, []);
});
