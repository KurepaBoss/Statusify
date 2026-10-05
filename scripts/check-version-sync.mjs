#!/usr/bin/env node
// Fails when the app's version strings drift apart.
//
//   node scripts/check-version-sync.mjs [--root <dir>] [--strict]
//
// Cargo.toml is the source of truth: the running app reports
// env!("CARGO_PKG_VERSION") (the About line, the log, the LRCLIB user agent and
// the update check all use it). Everything else must say the same thing:
//
//   src-tauri/Cargo.lock       the statusify-rs entry
//   src-tauri/tauri.conf.json  "version" (names the installer file)
//   package.json               "version"
//   package-lock.json          "version" and packages[""].version
//   README.md                  the version badge, a "Statusify vX.Y.Z" header and
//                              a "What's New in vX.Y.Z" heading, whichever exist
//
// A README that has none of those is only a note by default (so the check can
// run before the README is written); --strict, which a release should use,
// makes a missing badge an error. Port of Statusify-1.2.0's
// scripts_check_version_sync.py.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SEMVER = /^\d+\.\d+\.\d+$/;

const read = (root, rel) => {
  const p = join(root, rel);
  return existsSync(p) ? readFileSync(p, "utf8") : null;
};

/** `version = "x"` inside [package] of a Cargo.toml. */
export function cargoTomlVersion(text) {
  const pkg = text.match(/^\[package\]\s*$([\s\S]*?)(?=^\[|(?![\s\S]))/m);
  const m = pkg && pkg[1].match(/^\s*version\s*=\s*"([^"]+)"/m);
  return m ? m[1] : null;
}

/** The version of the `statusify-rs` package entry in a Cargo.lock. */
export function cargoLockVersion(text, name = "statusify-rs") {
  const re = new RegExp(String.raw`^\[\[package\]\]\r?\nname = "${name}"\r?\nversion = "([^"]+)"`, "m");
  const m = text.match(re);
  return m ? m[1] : null;
}

const jsonVersion = (text, pick = (j) => j.version) => {
  try {
    const v = pick(JSON.parse(text));
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
};

/** The version strings a README states, by kind. */
export function readmeVersions(text) {
  const out = {};
  const badge = text.match(/badge\/Statusify-v(\d+\.\d+\.\d+)-/);
  const header = text.match(/Statusify v(\d+\.\d+\.\d+)/);
  const whatsNew = text.match(/What['’]s New in v(\d+\.\d+\.\d+)/i);
  if (badge) out.badge = badge[1];
  if (header) out.header = header[1];
  if (whatsNew) out.whats_new = whatsNew[1];
  return out;
}

/** Every version the repo states, as [label, value|null|undefined] (undefined: file absent). */
export function collect(root) {
  const cargo = read(root, "src-tauri/Cargo.toml");
  const lock = read(root, "src-tauri/Cargo.lock");
  const tauri = read(root, "src-tauri/tauri.conf.json");
  const pkg = read(root, "package.json");
  const pkgLock = read(root, "package-lock.json");
  return {
    source: cargo === null ? null : cargoTomlVersion(cargo),
    others: [
      ["src-tauri/Cargo.lock", lock === null ? undefined : cargoLockVersion(lock)],
      ["src-tauri/tauri.conf.json", tauri === null ? undefined : jsonVersion(tauri)],
      ["package.json", pkg === null ? undefined : jsonVersion(pkg)],
      ["package-lock.json", pkgLock === null ? undefined : jsonVersion(pkgLock)],
      ["package-lock.json packages[\"\"]", pkgLock === null ? undefined : jsonVersion(pkgLock, (j) => j.packages?.[""]?.version)],
    ],
    readme: read(root, "README.md"),
  };
}

/** { version, errors, notes } for the repo at `root`. */
export function check(root, { strict = false } = {}) {
  const errors = [];
  const notes = [];
  const { source, others, readme } = collect(root);
  if (!source) {
    errors.push("src-tauri/Cargo.toml: no [package] version found");
    return { version: null, errors, notes };
  }
  if (!SEMVER.test(source)) errors.push(`src-tauri/Cargo.toml: "${source}" is not MAJOR.MINOR.PATCH`);
  for (const [label, v] of others) {
    if (v === undefined) errors.push(`${label}: file not found`);
    else if (v === null) errors.push(`${label}: no version found`);
    else if (v !== source) errors.push(`${label}: version ${v} != Cargo.toml ${source}`);
  }
  if (readme === null) {
    (strict ? errors : notes).push("README.md not found");
  } else {
    const found = readmeVersions(readme);
    for (const [kind, v] of Object.entries(found)) {
      if (v !== source) errors.push(`README.md ${kind.replace("_", " ")}: version ${v} != Cargo.toml ${source}`);
    }
    if (!found.badge) {
      (strict ? errors : notes).push(
        "README.md has no version badge (badge/Statusify-vX.Y.Z-...), so the README cannot be checked yet",
      );
    }
  }
  return { version: source, errors, notes };
}

function main(argv) {
  let root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  let strict = false;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--strict") strict = true;
    else if (argv[i] === "--root") root = resolve(argv[++i] ?? ".");
    else {
      console.error(`unknown argument ${argv[i]}`);
      return 2;
    }
  }
  const { version, errors, notes } = check(root, { strict });
  for (const n of notes) console.log(`note: ${n}`);
  if (errors.length) {
    console.error("Version sync check failed:");
    for (const e of errors) console.error(`- ${e}`);
    return 1;
  }
  console.log(`Version sync OK: ${version}`);
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
