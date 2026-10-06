// node --test: the words and rules of the Settings update row.
import test from "node:test";
import assert from "node:assert/strict";
import * as U from "../src/pages/settings_update.ts";

const upd = (extra = {}) => ({ tag: "3.1.0", url: "https://github.com/KurepaBoss/Statusify/releases/tag/v3.1.0", changelog: "• v3.1.0\n  faster", ...extra });

test("the check button's answer says what is known", () => {
  assert.equal(U.checkMessage({ status: "up_to_date", update: null }), "You're up to date.");
  assert.equal(U.checkMessage({ status: "available", update: upd() }), "Version 3.1.0 is available.");
  assert.equal(U.checkMessage({ status: "busy", update: null }), "Already checking…");
  // Asked twice within a minute: the second answer repeats what the first found.
  assert.equal(U.checkMessage({ status: "throttled", update: upd(), wait_s: 40 }), "Version 3.1.0 is available.");
  assert.match(U.checkMessage({ status: "throttled", update: null, wait_s: 40 }), /up to date/);
  assert.equal(U.checkMessage({ status: "skipped", update: null }), "");
  assert.equal(U.checkMessage(null), "");
  assert.match(U.checkFailedMessage(), /GitHub/);
});

test("a check held back after a failure never claims you are up to date", () => {
  // Offline, pressed again within a minute: the truth is "could not check", not "up to date".
  assert.equal(U.checkMessage({ status: "throttled", update: null, wait_s: 30, failed: true }), U.checkFailedMessage());
  // GitHub asked for a pause: say how long, rounded up to whole minutes.
  assert.match(U.checkMessage({ status: "throttled", update: null, wait_s: 780, failed: true, rate_limited: true }), /about 13 minutes/);
  assert.match(U.checkMessage({ status: "throttled", update: null, wait_s: 20, failed: true, rate_limited: true }), /about 1 minute\./);
  // An update found earlier is still what is shown through an outage.
  assert.equal(U.checkMessage({ status: "throttled", update: upd(), wait_s: 780, failed: true, rate_limited: true }), "Version 3.1.0 is available.");
});

test("the row describes the update when there is one, else the last answer, else the standing note", () => {
  assert.equal(U.updateDescription(upd(), "You're up to date."), "Version 3.1.0 is available.");
  assert.equal(U.updateDescription(null, "You're up to date."), "You're up to date.");
  assert.match(U.updateDescription(null, ""), /in the background/);
  assert.match(U.updateDescription(undefined, ""), /every few hours/);
});

test("the dialog opens by itself once per version", () => {
  assert.equal(U.shouldAutoShow(null, null), false, "no update: never");
  assert.equal(U.shouldAutoShow(undefined, "3.1.0"), false);
  assert.equal(U.shouldAutoShow(upd(), null), true, "a new version");
  assert.equal(U.shouldAutoShow(upd(), "3.0.5"), true, "an older 'Later' does not silence a newer release");
  assert.equal(U.shouldAutoShow(upd(), "3.1.0"), false, "'Later' on this version is remembered");
  assert.equal(U.shouldAutoShow({ ...upd(), tag: "" }, null), false);
});

test("the primary button says what will happen", () => {
  assert.equal(U.installLabel(upd({ can_install: true })), "Install update");
  assert.equal(U.installLabel(upd({ can_install: false })), "Open download page");
  assert.equal(U.installLabel(upd()), "Open download page");
});

test("a release without notes still gets a sentence", () => {
  assert.equal(U.changelogText(upd()), "• v3.1.0\n  faster");
  assert.match(U.changelogText(upd({ changelog: "  \n" })), /release page.*v3\.1\.0/);
});
