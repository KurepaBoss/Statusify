// The words and rules of the Settings page's update row, kept free of the DOM
// and of imports so they are testable on their own
// (tests-ts/settings_update.test.mjs, run with `node --test`).

/** What core publishes as extras.core.update_available when a newer release exists. */
export type UpdateInfo = {
  tag: string;
  url: string;
  changelog: string;
  /** The installer and its checksum list, when the release carries them. */
  setup?: unknown;
  /** True when this install can replace itself (installed from Setup.exe, installer attached). */
  can_install?: boolean;
};

/** What the check_update action answers. */
export type CheckResult = {
  status: "available" | "up_to_date" | "skipped" | "throttled" | "busy";
  update: UpdateInfo | null;
  wait_s?: number;
  /** Throttled because the previous request failed (so the answer is not "up to date"). */
  failed?: boolean;
  /** Throttled because GitHub asked for a pause. */
  rate_limited?: boolean;
};

/** "3.1.0" -> "Version 3.1.0 is available." */
export const availableText = (u: UpdateInfo): string => `Version ${u.tag} is available.`;

/** The line under "Check for updates" after the user pressed it. */
export function checkMessage(r: CheckResult | null | undefined): string {
  if (!r) return "";
  if (r.update) return availableText(r.update);
  switch (r.status) {
    case "up_to_date":
      return "You're up to date.";
    case "throttled": {
      // Asked again within a minute: nothing was sent, what we know still stands,
      // unless what we know is that the last attempt did not get through.
      if (r.rate_limited) {
        const min = Math.max(1, Math.ceil((r.wait_s ?? 60) / 60));
        return `GitHub has asked for a pause. Try again in about ${min} minute${min === 1 ? "" : "s"}.`;
      }
      if (r.failed) return checkFailedMessage();
      return "You're up to date. (Checked a moment ago.)";
    }
    case "busy":
      return "Already checking…";
    default:
      return "";
  }
}

/** The answer when the check itself failed (offline, GitHub unreachable or limiting requests). */
export function checkFailedMessage(): string {
  return "Couldn't reach GitHub. Check your connection and try again later.";
}

/** The standing description of the row while nothing has been pressed. */
export function updateDescription(u: UpdateInfo | null | undefined, note: string): string {
  if (u) return availableText(u);
  return note || "Statusify checks GitHub for new releases in the background, every few hours.";
}

/**
 * Whether the update dialog opens by itself. Once per version: after "Later"
 * (remembered by tag) the same release only shows in Settings, and a newer one
 * opens the dialog again.
 */
export function shouldAutoShow(u: UpdateInfo | null | undefined, dismissedTag: string | null | undefined): boolean {
  return !!u && !!u.tag && u.tag !== dismissedTag;
}

/** The primary button of the update dialog. */
export function installLabel(u: UpdateInfo): string {
  return u.can_install ? "Install update" : "Open download page";
}

/** The changelog as the dialog shows it: trimmed, with a fallback when a release has no notes. */
export function changelogText(u: UpdateInfo): string {
  const t = (u.changelog || "").trim();
  return t || `See the release page for what's new in v${u.tag}.`;
}
