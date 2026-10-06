// node --test: the version-sync check (scripts/check-version-sync.mjs) catches
// drift between Cargo, tauri.conf.json, package.json and the README badge.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cargoLockVersion, cargoTomlVersion, check, readmeVersions } from "../scripts/check-version-sync.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..");

/** A throwaway repo layout at version `v`, with `over` replacing files by relative path. */
function fixture(v, over = {}) {
  const root = mkdtempSync(join(tmpdir(), "statusify-versync-"));
  const files = {
    "src-tauri/Cargo.toml": `[package]\nname = "statusify-rs"\nversion = "${v}"\nedition = "2021"\n\n[dependencies]\nserde = { version = "1" }\n`,
    "src-tauri/Cargo.lock": `[[package]]\nname = "serde"\nversion = "1.0.1"\n\n[[package]]\nname = "statusify-rs"\nversion = "${v}"\n`,
    "src-tauri/tauri.conf.json": JSON.stringify({ productName: "Statusify", version: v }),
    "package.json": JSON.stringify({ name: "statusify-rs", version: v }),
    "package-lock.json": JSON.stringify({ name: "statusify-rs", version: v, packages: { "": { name: "statusify-rs", version: v } } }),
    "README.md": `# Statusify v${v}\n![v](https://img.shields.io/badge/Statusify-v${v}-brightgreen)\n## What's New in v${v}\n`,
    ...over,
  };
  for (const [rel, text] of Object.entries(files)) {
    if (text === null) continue;
    mkdirSync(dirname(join(root, rel)), { recursive: true });
    writeFileSync(join(root, rel), text);
  }
  return root;
}

const run = (v, over, opts) => {
  const root = fixture(v, over);
  try {
    return check(root, opts);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
};

test("a consistent repo passes, strictly too", () => {
  const r = run("3.0.0", {}, { strict: true });
  assert.deepEqual(r.errors, []);
  assert.equal(r.version, "3.0.0");
});

test("each manifest that drifts is reported by name", () => {
  const cases = [
    ["src-tauri/Cargo.lock", `[[package]]\nname = "statusify-rs"\nversion = "2.9.9"\n`, "Cargo.lock"],
    ["src-tauri/tauri.conf.json", JSON.stringify({ version: "0.1.0" }), "tauri.conf.json"],
    ["package.json", JSON.stringify({ version: "3.0.1" }), "package.json: version 3.0.1"],
    ["package-lock.json", JSON.stringify({ version: "3.0.0", packages: { "": { version: "3.0.1" } } }), 'packages[""]'],
    ["README.md", "![v](https://img.shields.io/badge/Statusify-v2.2.0-brightgreen)\n", "README.md badge"],
    ["README.md", "![v](https://img.shields.io/badge/Statusify-v3.0.0-brightgreen)\n## What's New in v2.2.0\n", "README.md whats new"],
  ];
  for (const [file, text, expect] of cases) {
    const r = run("3.0.0", { [file]: text });
    assert.equal(r.errors.length, 1, `${file}: ${JSON.stringify(r.errors)}`);
    assert.ok(r.errors[0].includes(expect), `${r.errors[0]} should mention ${expect}`);
  }
});

test("the version in Cargo.toml is the reference, and must be MAJOR.MINOR.PATCH", () => {
  const r = run("3.0", {});
  assert.ok(r.errors.some((e) => e.includes("MAJOR.MINOR.PATCH")), JSON.stringify(r.errors));
  const r2 = run("3.0.0", { "src-tauri/Cargo.toml": '[package]\nname = "x"\n' });
  assert.ok(r2.errors[0].includes("Cargo.toml"));
});

test("a README without a badge is a note, or an error when strict", () => {
  const lax = run("3.0.0", { "README.md": "# Tauri + Vanilla TS\n" });
  assert.deepEqual(lax.errors, []);
  assert.equal(lax.notes.length, 1);
  const strict = run("3.0.0", { "README.md": "# Tauri + Vanilla TS\n" }, { strict: true });
  assert.equal(strict.errors.length, 1);
  assert.ok(strict.errors[0].includes("badge"));
  assert.ok(run("3.0.0", { "README.md": null }, { strict: true }).errors[0].includes("README.md not found"));
});

test("missing files are errors, not silent passes", () => {
  assert.ok(run("3.0.0", { "package.json": null }).errors.some((e) => e.includes("package.json: file not found")));
});

test("parsers read CRLF files and ignore other crates' versions", () => {
  assert.equal(cargoTomlVersion('[package]\r\nname = "a"\r\nversion = "1.2.3"\r\n[dependencies]\r\nx = { version = "9.9.9" }\r\n'), "1.2.3");
  assert.equal(cargoLockVersion('[[package]]\r\nname = "statusify-rs"\r\nversion = "1.2.3"\r\n'), "1.2.3");
  assert.equal(cargoLockVersion('[[package]]\nname = "statusify-rs-other"\nversion = "1.2.3"\n'), null);
  assert.deepEqual(readmeVersions("Statusify v1.0.0 ... What’s New in v1.0.1 ..."), { header: "1.0.0", whats_new: "1.0.1" });
});

test("the real repository is in sync", () => {
  // The app reports Cargo's version at run time (APP_VERSION = env!("CARGO_PKG_VERSION"),
  // asserted by a Rust test); this guards that every other manifest says the same.
  const r = check(REPO);
  assert.deepEqual(r.errors, []);
});
