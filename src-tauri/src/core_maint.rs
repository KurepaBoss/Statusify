//! In-app update and Spicetify-bridge repair (port of statusify_maintenance.py
//! plus main.py's _check_for_updates / _install_bridge / _bridge_needs_apply
//! and statusify_bridge.spotify_running).

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// The project's GitHub repository. Update checks read its releases and every
/// release link points back here.
pub const REPO_URL: &str = "https://github.com/KurepaBoss/Statusify";
pub const RELEASES_URL: &str = "https://api.github.com/repos/KurepaBoss/Statusify/releases?per_page=30";
/// This build's version. It is Cargo.toml's: scripts/check-version-sync.mjs
/// keeps tauri.conf.json, package.json and the README badge equal to it. The
/// update check compares releases against it and LRCLIB sees it in the
/// User-Agent.
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
/// The checksum list published with every release (sha256sum format).
pub const SUMS_NAME: &str = "SHA256SUMS.txt";

/// The versioned installer a release carries (Tauri's NSIS naming). The
/// stable "Statusify-Setup.exe" copy is for people downloading by hand.
pub fn setup_name(version: &str) -> String {
    format!("Statusify_{version}_x64-setup.exe")
}

/// The bridge and the Spicetify setup script we ship (byte-exact copies).
pub const BRIDGE_JS: &[u8] = include_bytes!("../resources/lyrics-bridge.js");
pub const SETUP_PS1: &[u8] = include_bytes!("../resources/setup-spicetify.ps1");

const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED: u32 = 0x0000_0008 | 0x0000_0200; // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

// ── Updates ───────────────────────────────────────────────────────

/// MAJOR.MINOR.PATCH out of a release tag ("v3.1.0" or "3.1.0"). Anything
/// else (v3.1.0-beta, "3.1", "nightly") is not a release this app follows.
pub fn parse_tag(tag: &str) -> Option<[u64; 3]> {
    let tag = tag.trim();
    let mut parts = tag.strip_prefix('v').unwrap_or(tag).split('.');
    let mut out = [0u64; 3];
    for slot in &mut out {
        let p = parts.next()?;
        if p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        *slot = p.parse().ok()?;
    }
    parts.next().is_none().then_some(out)
}

/// The release's page, only ever on this project's repository: the API's own
/// html_url when it is one, else the tag's page.
fn release_page(release: &Value, tag: &str) -> String {
    let given = release.get("html_url").and_then(|u| u.as_str()).unwrap_or("");
    if given.starts_with(&format!("{REPO_URL}/")) {
        given.to_string()
    } else {
        format!("{REPO_URL}/releases/tag/v{tag}")
    }
}

/// {url, sums_url, name} for this release's installer, or None. The installer
/// and SHA256SUMS.txt must both be attached: without a published checksum
/// nothing can verify the download.
pub fn setup_asset(release: &Value) -> Option<Value> {
    let tag = release.get("tag_name").and_then(|t| t.as_str()).unwrap_or("").trim_start_matches('v');
    let name = setup_name(tag);
    let assets = release.get("assets").and_then(|a| a.as_array())?;
    // Only files this repository's releases serve: whatever the API answer
    // says, the updater never fetches (and then runs) an installer from anywhere else.
    let prefix = format!("{REPO_URL}/releases/download/");
    let url = |want: &str| {
        assets
            .iter()
            .find(|a| a.get("name").and_then(|n| n.as_str()) == Some(want))
            .and_then(|a| a.get("browser_download_url").and_then(|u| u.as_str()))
            .filter(|u| u.starts_with(&prefix))
            .map(str::to_string)
    };
    Some(json!({"url": url(&name)?, "sums_url": url(SUMS_NAME)?, "name": name}))
}

/// The newest stable release above `current`, with the notes of every newer
/// release compiled into one changelog (newest first). None when this build
/// is up to date or newer, and when `current` is not a plain MAJOR.MINOR.PATCH
/// (a development version must never be told to "update" to anything).
pub fn find_update(releases: &Value, current: &str) -> Option<Value> {
    let cur = parse_tag(current)?;
    let flag = |r: &Value, k: &str| r.get(k).and_then(|d| d.as_bool()).unwrap_or(false);
    let mut newer: Vec<([u64; 3], &Value)> = releases
        .as_array()?
        .iter()
        .filter(|r| !flag(r, "draft") && !flag(r, "prerelease"))
        .filter_map(|r| Some((parse_tag(r.get("tag_name")?.as_str()?)?, r)))
        .filter(|(v, _)| *v > cur)
        .collect();
    // By version, not by the order the API happens to list them in.
    newer.sort_by(|a, b| b.0.cmp(&a.0));
    let (top_v, top) = *newer.first()?;
    let tag = format!("{}.{}.{}", top_v[0], top_v[1], top_v[2]);
    let mut notes: Vec<String> = Vec::new();
    for (v, r) in &newer {
        notes.push(format!("• v{}.{}.{}", v[0], v[1], v[2]));
        let body = r.get("body").and_then(|b| b.as_str()).unwrap_or("").trim();
        for ln in body.lines().filter(|l| !l.trim().is_empty()) {
            notes.push(format!("  {ln}"));
        }
        notes.push(String::new());
    }
    Some(json!({
        "tag": tag,
        "url": release_page(top, &tag),
        "changelog": notes.join("\n").trim().to_string(),
        "setup": setup_asset(top),
    }))
}

/// A failed check, and how long the server asked us to stay away (GitHub's
/// rate limit), if it did.
#[derive(Debug, PartialEq)]
pub struct UpdateError {
    pub message: String,
    pub retry_after: Option<Duration>,
}

impl UpdateError {
    fn plain(message: impl Into<String>) -> Self {
        UpdateError { message: message.into(), retry_after: None }
    }
}

/// How long to stay away after GitHub says the hourly allowance is spent:
/// until its reset time, within [1 min, 1 h].
fn rate_limit_wait(headers: &reqwest::header::HeaderMap, now_secs: u64) -> Option<Duration> {
    let num = |k: &str| headers.get(k).and_then(|v| v.to_str().ok()).and_then(|v| v.trim().parse::<u64>().ok());
    let exhausted = num("x-ratelimit-remaining") == Some(0);
    let retry = num("retry-after");
    if !exhausted && retry.is_none() {
        return None;
    }
    let secs = retry.or_else(|| num("x-ratelimit-reset").map(|at| at.saturating_sub(now_secs))).unwrap_or(3600);
    Some(Duration::from_secs(secs.clamp(60, 3600)))
}

/// Ask `url` (the releases list) for a release newer than `current`.
pub async fn check_update(client: &reqwest::Client, url: &str, current: &str) -> Result<Option<Value>, UpdateError> {
    let r = client
        .get(url)
        .header("User-Agent", format!("Statusify/{APP_VERSION} (+{REPO_URL})"))
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|e| UpdateError::plain(e.to_string()))?;
    let status = r.status();
    if !status.is_success() {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let limited = if matches!(status.as_u16(), 403 | 429) { rate_limit_wait(r.headers(), now) } else { None };
        return Err(UpdateError { message: format!("HTTP {status}"), retry_after: limited });
    }
    let data: Value = r.json().await.map_err(|e| UpdateError::plain(e.to_string()))?;
    if !data.is_array() {
        return Err(UpdateError::plain("unexpected response from the releases API"));
    }
    Ok(find_update(&data, current))
}

/// When the update check may touch the network. GitHub allows an anonymous
/// address 60 requests an hour, shared by everyone behind it, so the app asks
/// rarely: once per AUTO_EVERY_MS on its own (counted across restarts), at
/// most once per MANUAL_MIN_MS when the user asks, not for AUTO_RETRY_MS after
/// a failure (offline, an error page) and not until the reset time when GitHub
/// says the allowance is spent. Times are epoch milliseconds, passed in so the
/// rules are testable.
#[derive(Debug, Default)]
pub struct UpdateGate {
    last_ok_ms: Option<i64>,
    last_attempt_ms: Option<i64>,
    not_before_ms: i64,
    rate_limited: bool,
    /// The last request that went out failed (so "wait a minute" is not "you are up to date").
    last_failed: bool,
}

pub const AUTO_EVERY_MS: i64 = 6 * 60 * 60 * 1000;
pub const AUTO_RETRY_MS: i64 = 30 * 60 * 1000;
pub const MANUAL_MIN_MS: i64 = 60 * 1000;

impl UpdateGate {
    /// `last_ok_ms`: when a check last succeeded (kept in the config file).
    pub fn new(last_ok_ms: Option<i64>) -> Self {
        UpdateGate { last_ok_ms, ..Default::default() }
    }

    #[cfg(test)]
    pub fn last_ok_ms(&self) -> Option<i64> {
        self.last_ok_ms
    }

    /// Why a manual check is being held back, for the words the UI shows:
    /// (the last request failed, GitHub's rate limit is in force).
    pub fn why_waiting(&self, now: i64) -> (bool, bool) {
        (self.last_failed, self.rate_limited && now < self.not_before_ms)
    }

    /// The background check is due.
    pub fn auto_due(&self, now: i64) -> bool {
        now >= self.not_before_ms && self.last_ok_ms.is_none_or(|t| now - t >= AUTO_EVERY_MS || now < t)
    }

    /// How long a check the user asked for must still wait (0: go ahead).
    pub fn manual_wait_ms(&self, now: i64) -> i64 {
        if self.rate_limited && now < self.not_before_ms {
            return self.not_before_ms - now;
        }
        match self.last_attempt_ms {
            Some(t) if now >= t && now - t < MANUAL_MIN_MS => MANUAL_MIN_MS - (now - t),
            _ => 0,
        }
    }

    /// A request is about to go out (so a second click cannot start another).
    pub fn begin(&mut self, now: i64) {
        self.last_attempt_ms = Some(now);
    }

    pub fn record_ok(&mut self, now: i64) {
        self.last_ok_ms = Some(now);
        self.last_attempt_ms = Some(now);
        self.not_before_ms = 0;
        self.rate_limited = false;
        self.last_failed = false;
    }

    pub fn record_err(&mut self, now: i64, e: &UpdateError) {
        self.last_attempt_ms = Some(now);
        self.last_failed = true;
        match e.retry_after {
            Some(d) => {
                self.not_before_ms = now + d.as_millis() as i64;
                self.rate_limited = true;
            }
            None => {
                self.not_before_ms = now + AUTO_RETRY_MS;
                self.rate_limited = false;
            }
        }
    }
}

/// The SHA-256 `sums` (sha256sum output: "<hash>  <name>" or "<hash> *<name>")
/// lists for `name`.
pub fn checksum_for(sums: &str, name: &str) -> Option<String> {
    sums.lines().find_map(|line| {
        let (hash, file) = line.trim().split_once(char::is_whitespace)?;
        let file = file.trim().trim_start_matches('*');
        (file == name && hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit())).then(|| hash.to_lowercase())
    })
}

/// Download the installer `name` from `url` into `dest_dir`, but only keep it
/// if its SHA-256 matches the one `sums_url` (SHA256SUMS.txt) lists for it;
/// otherwise nothing is written.
pub async fn download_verified(client: &reqwest::Client, url: &str, sums_url: &str, name: &str, dest_dir: &Path) -> Result<PathBuf, String> {
    use sha2::{Digest, Sha256};
    // The name comes from our own naming scheme, never from the URL.
    if name.is_empty() || name.contains(['/', '\\']) {
        return Err("bad installer name".into());
    }
    let get = |u: String, secs: u64| {
        let c = client.clone();
        async move {
            let r = c.get(&u).header("User-Agent", format!("Statusify/{APP_VERSION} (+{REPO_URL})")).timeout(Duration::from_secs(secs)).send().await.map_err(|e| e.to_string())?;
            if !r.status().is_success() {
                return Err(format!("HTTP {}", r.status()));
            }
            r.bytes().await.map_err(|e| e.to_string())
        }
    };
    let sums = get(sums_url.to_string(), 30).await?;
    let expected = checksum_for(&String::from_utf8_lossy(&sums), name).ok_or_else(|| format!("{SUMS_NAME} has no checksum for {name}"))?;
    let data = get(url.to_string(), 180).await?;
    let actual: String = Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect();
    if actual != expected {
        return Err(format!("download does not match published checksum {}…", &expected[..12]));
    }
    std::fs::create_dir_all(dest_dir).map_err(|e| e.to_string())?;
    let path = dest_dir.join(name);
    std::fs::write(&path, &data).map_err(|e| e.to_string())?;
    Ok(path)
}

/// Installed by the Setup.exe (its uninstaller sits beside the app) in a
/// release build. A portable exe or a dev build updates through the browser.
pub fn is_installed(app_dir: &Path) -> bool {
    !cfg!(debug_assertions) && app_dir.join("uninstall.exe").exists()
}

/// Run the installer silently a few seconds from now, detached, so this
/// process can exit first (the installer will not replace a running exe).
/// /S is silent, /R has the installer start the app again afterwards, and
/// /UPDATE tells it this is an update, not a fresh install: it installs over
/// the old files without running the old uninstaller, so the Start-menu and
/// desktop shortcuts and the "launch when Windows starts" entry stay as they were.
pub fn launch_silent_update(setup: &Path) -> std::io::Result<()> {
    use std::os::windows::process::CommandExt;
    let cmd = format!("Start-Sleep -Seconds 3; & '{}' /S /R /UPDATE", setup.display().to_string().replace('\'', "''"));
    std::process::Command::new("powershell")
        .args(["-NoProfile", "-WindowStyle", "Hidden", "-Command", &cmd])
        .creation_flags(DETACHED)
        .spawn()
        .map(|_| ())
}

// ── Bridge ────────────────────────────────────────────────────────

/// Where Spicetify actually loads the bridge from. %APPDATA%\spicetify\
/// Extensions is only the SOURCE folder; `spicetify apply` injects a copy
/// into Spotify's xpui bundle and that copy is what runs. Restarting Spotify
/// does not re-read the source.
pub fn injected_bridge_path(appdata: &Path) -> PathBuf {
    appdata.join("Spotify").join("Apps").join("xpui").join("extensions").join("lyrics-bridge.js")
}

/// True when the bridge Spotify runs differs from ours. False when we can't
/// tell: a spurious "run spicetify apply" nag is worse than silence.
pub fn bridge_needs_apply(appdata: &Path, ours: &[u8]) -> bool {
    match std::fs::read(injected_bridge_path(appdata)) {
        Ok(b) => b != ours,
        Err(_) => false,
    }
}

#[derive(Debug, PartialEq)]
pub struct BridgeInstall {
    pub path: PathBuf,
    pub copied: bool,
    /// The copy has not reached Spotify yet: `spicetify apply` is needed.
    pub needs_apply: bool,
}

/// Copy the bridge into the Spicetify Extensions folder if its content
/// differs (content, not size: a same-size version bump once left a stale
/// copy in place), then check whether Spotify runs it.
pub fn install_bridge(appdata: &Path, ours: &[u8]) -> Result<BridgeInstall, String> {
    let ext = appdata.join("spicetify").join("Extensions");
    if !ext.is_dir() {
        return Err("Spicetify Extensions folder not found — install Spicetify for lyrics".into());
    }
    let dest = ext.join("lyrics-bridge.js");
    let copied = match std::fs::read(&dest) {
        Ok(b) if b == ours => false,
        _ => {
            std::fs::write(&dest, ours).map_err(|e| format!("Could not install Spicetify bridge: {e}"))?;
            true
        }
    };
    Ok(BridgeInstall { path: dest, copied, needs_apply: bridge_needs_apply(appdata, ours) })
}

/// Write the setup script and bridge to a stable folder for the repair.
pub fn stage_repair_files(dir: &Path) -> std::io::Result<(PathBuf, PathBuf)> {
    std::fs::create_dir_all(dir)?;
    let (s, b) = (dir.join("setup-spicetify.ps1"), dir.join("lyrics-bridge.js"));
    std::fs::write(&s, SETUP_PS1)?;
    std::fs::write(&b, BRIDGE_JS)?;
    Ok((s, b))
}

/// Open the Spicetify setup script in its own visible console window (it
/// re-wires the bridge and runs `spicetify apply`, restarting Spotify).
/// Only ever called from the user's "repair" click.
pub fn launch_repair(script: &Path, bridge: &Path) -> std::io::Result<()> {
    use std::os::windows::process::CommandExt;
    std::process::Command::new("powershell")
        .arg("-NoProfile")
        .args(["-ExecutionPolicy", "Bypass", "-File"])
        .arg(script)
        .arg("-Bridge")
        .arg(bridge)
        .creation_flags(CREATE_NEW_CONSOLE)
        .spawn()
        .map(|_| ())
}

/// Spotify.exe is running (false on any failure: no warning).
pub fn spotify_running() -> bool {
    use std::os::windows::process::CommandExt;
    std::process::Command::new("tasklist")
        .args(["/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV", "/NH"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_lowercase().contains("spotify.exe"))
        .unwrap_or(false)
}

/// Shared by this file's tests and the core feature's: a fake GitHub.
#[cfg(test)]
pub(crate) mod test_http {
    use super::*;

    /// A tiny HTTP server for the update tests: every route answers with a
    /// fixed status, headers and body; `hits` counts the requests it served.
    pub(crate) async fn serve(routes: Vec<(&'static str, u16, Vec<(&'static str, String)>, Vec<u8>)>) -> (String, std::sync::Arc<std::sync::atomic::AtomicUsize>) {
        use std::sync::atomic::Ordering;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", l.local_addr().unwrap());
        let hits = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let h = hits.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut c, _)) = l.accept().await else { return };
                let mut buf = vec![0u8; 4096];
                let n = c.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let path = req.split_whitespace().nth(1).unwrap_or("/").to_string();
                h.fetch_add(1, Ordering::SeqCst);
                let (status, headers, body) = routes
                    .iter()
                    .find(|r| path.starts_with(r.0))
                    .map(|r| (r.1, r.2.clone(), r.3.clone()))
                    .unwrap_or((404, vec![], b"not found".to_vec()));
                let mut head = format!("HTTP/1.1 {status} X\r\ncontent-length: {}\r\nconnection: close\r\n", body.len());
                for (k, v) in headers {
                    head.push_str(&format!("{k}: {v}\r\n"));
                }
                head.push_str("\r\n");
                let _ = c.write_all(head.as_bytes()).await;
                let _ = c.write_all(&body).await;
                let _ = c.shutdown().await;
            }
        });
        (base, hits)
    }

    pub(crate) fn release(tag: &str, body: &str, with_setup: bool) -> Value {
        let ver = tag.trim_start_matches('v');
        let assets = if with_setup {
            json!([
                {"name": setup_name(ver), "browser_download_url": format!("https://github.com/KurepaBoss/Statusify/releases/download/{tag}/{}", setup_name(ver))},
                {"name": "Statusify-Setup.exe", "browser_download_url": "https://x/stable.exe"},
                {"name": SUMS_NAME, "browser_download_url": format!("https://github.com/KurepaBoss/Statusify/releases/download/{tag}/{SUMS_NAME}")},
            ])
        } else {
            json!([])
        };
        json!({"tag_name": tag, "html_url": format!("{REPO_URL}/releases/tag/{tag}"), "body": body, "assets": assets})
    }

}

#[cfg(test)]
mod tests {
    use super::test_http::{release, serve};
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-maint-{tag}-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn release_tags_parse_strictly() {
        assert_eq!(parse_tag("v3.1.0"), Some([3, 1, 0]));
        assert_eq!(parse_tag("3.10.12"), Some([3, 10, 12]));
        assert_eq!(parse_tag(" v2.2.0 "), Some([2, 2, 0]));
        for bad in ["", "v", "3.1", "3.1.0.1", "v3.1.0-beta", "3.1.x", "nightly", "v3..1", "-3.1.0", "3.1.0+build"] {
            assert_eq!(parse_tag(bad), None, "{bad}");
        }
        // Compared by number, not by text
        assert!(parse_tag("2.10.0") > parse_tag("2.9.9"));
        assert!(parse_tag("v2.2.0") == parse_tag("2.2.0"));
    }

    #[test]
    fn update_found_with_changelog_and_installer() {
        // The API lists newest created first, which is not always the highest version.
        let rel = json!([
            release("v3.1.0", "- new\n\n- more", true),
            release("v3.2.0", "big one", true),
            release("v3.0.0", "first rust release", true),
            {"tag_name": "v2.2.0", "html_url": format!("{REPO_URL}/releases/tag/v2.2.0"), "body": "old python",
             "assets": [{"name": "Statusify-Setup-2.2.0.exe", "browser_download_url": "https://x/py.exe"}]},
        ]);
        let u = find_update(&rel, "3.0.0").unwrap();
        assert_eq!(u["tag"], "3.2.0");
        assert_eq!(u["url"], format!("{REPO_URL}/releases/tag/v3.2.0"));
        assert_eq!(u["changelog"], "• v3.2.0\n  big one\n\n• v3.1.0\n  - new\n  - more");
        assert_eq!(u["setup"]["name"], "Statusify_3.2.0_x64-setup.exe");
        assert!(u["setup"]["url"].as_str().unwrap().ends_with("/v3.2.0/Statusify_3.2.0_x64-setup.exe"));
        assert!(u["setup"]["sums_url"].as_str().unwrap().ends_with("/v3.2.0/SHA256SUMS.txt"));

        // Equal or newer than the newest release: nothing to say.
        assert!(find_update(&rel, "3.2.0").is_none());
        assert!(find_update(&rel, "3.3.0").is_none());
        assert!(find_update(&rel, "4.0.0").is_none());
        // The Python app's own releases are not an update for this app (they are older).
        assert!(find_update(&json!([{"tag_name": "v2.2.0"}, {"tag_name": "v1.2.0"}]), "3.0.0").is_none());
        assert!(find_update(&json!([]), "3.0.0").is_none());
        assert!(find_update(&json!({"message": "Not Found"}), "3.0.0").is_none());
    }

    #[test]
    fn only_stable_well_formed_releases_count() {
        let mut draft = release("v9.0.0", "", true);
        draft["draft"] = json!(true);
        let mut pre = release("v8.0.0", "", true);
        pre["prerelease"] = json!(true);
        let rel = json!([draft, pre, release("v7.0.0-beta", "", true), release("nightly", "", true), release("v3.0.1", "fix", true)]);
        let u = find_update(&rel, "3.0.0").unwrap();
        assert_eq!(u["tag"], "3.0.1");
        assert_eq!(u["changelog"], "• v3.0.1\n  fix");
        // A development version is never told to update.
        assert!(find_update(&rel, "3.0.0-dev").is_none());
        assert!(find_update(&rel, "garbage").is_none());
        assert!(find_update(&rel, "").is_none());
    }

    #[test]
    fn installer_needs_its_checksum_and_the_page_stays_on_the_repo() {
        // No installer attached (or no SHA256SUMS.txt): the page opens instead of a silent install.
        let u = find_update(&json!([release("v3.1.0", "x", false)]), "3.0.0").unwrap();
        assert!(u["setup"].is_null());
        let mut no_sums = release("v3.1.0", "x", true);
        no_sums["assets"].as_array_mut().unwrap().retain(|a| a["name"] != SUMS_NAME);
        assert!(find_update(&json!([no_sums]), "3.0.0").unwrap()["setup"].is_null());
        // The stable-name copy and the Python app's installer are not what the updater runs.
        let mut only_stable = release("v3.1.0", "x", true);
        only_stable["assets"].as_array_mut().unwrap().retain(|a| a["name"] != "Statusify_3.1.0_x64-setup.exe");
        assert!(find_update(&json!([only_stable]), "3.0.0").unwrap()["setup"].is_null());
        // Download links that do not point into this repository's releases are not trusted.
        let mut foreign = release("v3.1.0", "x", true);
        foreign["assets"][0]["browser_download_url"] = json!("https://evil.example/Statusify_3.1.0_x64-setup.exe");
        assert!(find_update(&json!([foreign]), "3.0.0").unwrap()["setup"].is_null());
        let mut foreign_sums = release("v3.1.0", "x", true);
        foreign_sums["assets"][2]["browser_download_url"] = json!("http://github.com/KurepaBoss/Statusify/releases/download/v3.1.0/SHA256SUMS.txt");
        assert!(find_update(&json!([foreign_sums]), "3.0.0").unwrap()["setup"].is_null());
        // A link to anywhere else is replaced by the tag's own page.
        let mut odd = release("v3.1.0", "x", false);
        odd["html_url"] = json!("https://evil.example/download");
        assert_eq!(find_update(&json!([odd]), "3.0.0").unwrap()["url"], format!("{REPO_URL}/releases/tag/v3.1.0"));
        let mut missing = release("v3.1.0", "x", false);
        missing.as_object_mut().unwrap().remove("html_url");
        assert_eq!(find_update(&json!([missing]), "3.0.0").unwrap()["url"], format!("{REPO_URL}/releases/tag/v3.1.0"));
    }

    #[test]
    fn this_build_is_the_cargo_version_and_never_offered_itself() {
        assert_eq!(APP_VERSION, env!("CARGO_PKG_VERSION"));
        assert!(parse_tag(APP_VERSION).is_some(), "the crate version must be MAJOR.MINOR.PATCH for the update check");
        // The release that ships this build is not an update to it.
        assert!(find_update(&json!([release(&format!("v{APP_VERSION}"), "me", true)]), APP_VERSION).is_none());
        assert!(RELEASES_URL.starts_with("https://api.github.com/repos/KurepaBoss/Statusify/releases"));
        assert!(RELEASES_URL.contains(REPO_URL.trim_start_matches("https://github.com/")));
    }

    #[test]
    fn gate_asks_rarely_and_backs_off() {
        const H: i64 = 3_600_000;
        let t0 = 1_800_000_000_000i64;
        // First run, no history: due now; once asked, not again for 6 hours.
        let mut g = UpdateGate::new(None);
        assert!(g.auto_due(t0));
        g.begin(t0);
        g.record_ok(t0);
        assert!(!g.auto_due(t0 + H));
        assert!(!g.auto_due(t0 + AUTO_EVERY_MS - 1));
        assert!(g.auto_due(t0 + AUTO_EVERY_MS));
        // The time of the last check survives a restart (it is kept in the config file).
        let restarted = UpdateGate::new(g.last_ok_ms());
        assert!(!restarted.auto_due(t0 + 5 * H));
        assert!(restarted.auto_due(t0 + 7 * H));
        // A clock set back must not silence the check for good.
        assert!(restarted.auto_due(t0 - H));

        // The user's button: at most one request a minute.
        assert_eq!(g.manual_wait_ms(t0 + 10_000), MANUAL_MIN_MS - 10_000);
        assert_eq!(g.manual_wait_ms(t0 + MANUAL_MIN_MS), 0);
        let mut fresh = UpdateGate::new(None);
        assert_eq!(fresh.manual_wait_ms(t0), 0);
        fresh.begin(t0);
        assert_eq!(fresh.manual_wait_ms(t0 + 1), MANUAL_MIN_MS - 1, "a second click while the first is in flight waits");

        // Offline: the background check waits half an hour, a manual retry only the minute.
        let mut off = UpdateGate::new(None);
        off.begin(t0);
        off.record_err(t0, &UpdateError { message: "dns error".into(), retry_after: None });
        assert!(!off.auto_due(t0 + AUTO_RETRY_MS - 1));
        assert!(off.auto_due(t0 + AUTO_RETRY_MS));
        assert_eq!(off.manual_wait_ms(t0 + MANUAL_MIN_MS), 0);

        // GitHub's limit: nothing, automatic or manual, until the reset.
        let mut lim = UpdateGate::new(None);
        lim.begin(t0);
        lim.record_err(t0, &UpdateError { message: "HTTP 403".into(), retry_after: Some(Duration::from_secs(1200)) });
        assert!(!lim.auto_due(t0 + 1_199_000));
        assert!(lim.auto_due(t0 + 1_200_000));
        assert_eq!(lim.manual_wait_ms(t0 + 600_000), 600_000);
        assert_eq!(lim.manual_wait_ms(t0 + 1_200_000), 0);
        assert_eq!(lim.why_waiting(t0 + 600_000), (true, true));
        assert_eq!(lim.why_waiting(t0 + 1_200_000), (true, false), "limit over, but the last request still failed");
        assert_eq!(off.why_waiting(t0 + 1), (true, false));
        // ...and a later success clears it.
        lim.record_ok(t0 + 1_200_000);
        assert_eq!(lim.why_waiting(t0 + 1_200_001), (false, false));
        assert_eq!(lim.manual_wait_ms(t0 + 1_200_000 + MANUAL_MIN_MS), 0);
    }

    #[test]
    fn rate_limit_headers_are_read() {
        use reqwest::header::{HeaderMap, HeaderValue};
        let h = |pairs: &[(&'static str, &str)]| {
            let mut m = HeaderMap::new();
            for (k, v) in pairs {
                m.insert(*k, HeaderValue::from_str(v).unwrap());
            }
            m
        };
        // Allowance spent: wait until the reset (but between 1 min and 1 h).
        assert_eq!(rate_limit_wait(&h(&[("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "1000600")]), 1_000_000), Some(Duration::from_secs(600)));
        assert_eq!(rate_limit_wait(&h(&[("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "1000010")]), 1_000_000), Some(Duration::from_secs(60)));
        assert_eq!(rate_limit_wait(&h(&[("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "9999999")]), 1_000_000), Some(Duration::from_secs(3600)));
        assert_eq!(rate_limit_wait(&h(&[("x-ratelimit-remaining", "0")]), 1_000_000), Some(Duration::from_secs(3600)));
        assert_eq!(rate_limit_wait(&h(&[("retry-after", "120")]), 1_000_000), Some(Duration::from_secs(120)));
        // A 403 that is not about the limit is just an error.
        assert_eq!(rate_limit_wait(&h(&[("x-ratelimit-remaining", "12")]), 1_000_000), None);
        assert_eq!(rate_limit_wait(&h(&[]), 1_000_000), None);
    }

    #[tokio::test]
    async fn update_check_against_fake_github() {
        let ok = serde_json::to_vec(&json!([release("v9.0.0", "b", true), release("v3.0.0", "now", true)])).unwrap();
        let (base, hits) = serve(vec![
            ("/releases", 200, vec![("content-type", "application/json".into())], ok),
            ("/limited", 403, vec![("x-ratelimit-remaining", "0".into()), ("x-ratelimit-reset", "1".into())], b"{}".to_vec()),
            ("/forbidden", 403, vec![("x-ratelimit-remaining", "40".into())], b"{}".to_vec()),
            ("/broken", 500, vec![], b"oops".to_vec()),
            ("/html", 200, vec![], b"<html>captive portal</html>".to_vec()),
            ("/object", 200, vec![], b"{\"message\":\"x\"}".to_vec()),
        ])
        .await;
        let c = reqwest::Client::new();
        let u = check_update(&c, &format!("{base}/releases"), "3.0.0").await.unwrap().unwrap();
        assert_eq!(u["tag"], "9.0.0");
        assert_eq!(check_update(&c, &format!("{base}/releases"), "9.0.0").await, Ok(None), "equal: no update");
        assert_eq!(check_update(&c, &format!("{base}/releases"), "10.0.0").await, Ok(None), "newer than the newest: no update");

        let e = check_update(&c, &format!("{base}/limited"), "3.0.0").await.unwrap_err();
        assert_eq!(e.retry_after, Some(Duration::from_secs(60)), "{e:?}"); // reset time long past: the 1 min floor
        let e = check_update(&c, &format!("{base}/forbidden"), "3.0.0").await.unwrap_err();
        assert!(e.retry_after.is_none() && e.message.contains("403"), "{e:?}");
        let e = check_update(&c, &format!("{base}/broken"), "3.0.0").await.unwrap_err();
        assert!(e.retry_after.is_none() && e.message.contains("500"), "{e:?}");
        assert!(check_update(&c, &format!("{base}/html"), "3.0.0").await.is_err(), "a captive portal's page is an error, not 'up to date'");
        assert!(check_update(&c, &format!("{base}/object"), "3.0.0").await.is_err());
        assert!(check_update(&c, &format!("{base}/gone"), "3.0.0").await.is_err(), "404");
        assert!(hits.load(std::sync::atomic::Ordering::SeqCst) >= 8);

        // Offline: nothing listens there.
        let dead = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/releases", dead.local_addr().unwrap());
        drop(dead);
        let e = check_update(&c, &url, "3.0.0").await.unwrap_err();
        assert!(e.retry_after.is_none() && !e.message.is_empty(), "{e:?}");
    }

    #[test]
    fn checksum_lines_are_matched_by_file_name() {
        let (a, b) = ("a".repeat(64), "B".repeat(64));
        let sums = format!("{a}  Statusify-Setup.exe\r\n{b} *Statusify_3.1.0_x64-setup.exe\n\n");
        assert_eq!(checksum_for(&sums, "Statusify_3.1.0_x64-setup.exe"), Some("b".repeat(64)));
        assert_eq!(checksum_for(&sums, "Statusify-Setup.exe"), Some(a));
        assert_eq!(checksum_for(&sums, "other.exe"), None);
        assert_eq!(checksum_for("nothing here", "x.exe"), None);
        // Not a hash: not accepted
        assert_eq!(checksum_for("abc  x.exe", "x.exe"), None);
        assert_eq!(checksum_for(&format!("{}  x.exe", "z".repeat(64)), "x.exe"), None);
    }

    #[tokio::test]
    async fn download_is_kept_only_when_it_matches_the_published_checksum() {
        use sha2::{Digest, Sha256};
        let exe = b"MZ pretend installer".to_vec();
        let sha: String = Sha256::digest(&exe).iter().map(|b| format!("{b:02x}")).collect();
        let name = "Statusify_3.1.0_x64-setup.exe";
        let good = format!("{}  Statusify-Setup.exe\n{sha}  {name}\n", "0".repeat(64)).into_bytes();
        let bad = format!("{}  {name}\n", "1".repeat(64)).into_bytes();
        let other = format!("{sha}  something-else.exe\n").into_bytes();
        let (base, _) = serve(vec![
            ("/setup.exe", 200, vec![], exe.clone()),
            ("/good", 200, vec![], good),
            ("/bad", 200, vec![], bad),
            ("/other", 200, vec![], other),
        ])
        .await;
        let c = reqwest::Client::new();
        let dir = tmp("download");
        let p = download_verified(&c, &format!("{base}/setup.exe"), &format!("{base}/good"), name, &dir).await.unwrap();
        assert_eq!(p, dir.join(name));
        assert_eq!(std::fs::read(&p).unwrap(), exe);
        std::fs::remove_file(&p).unwrap();

        let e = download_verified(&c, &format!("{base}/setup.exe"), &format!("{base}/bad"), name, &dir).await.unwrap_err();
        assert!(e.contains("does not match"), "{e}");
        let e = download_verified(&c, &format!("{base}/setup.exe"), &format!("{base}/other"), name, &dir).await.unwrap_err();
        assert!(e.contains("no checksum"), "{e}");
        let e = download_verified(&c, &format!("{base}/setup.exe"), &format!("{base}/missing"), name, &dir).await.unwrap_err();
        assert!(e.contains("404"), "{e}");
        let e = download_verified(&c, &format!("{base}/setup.exe"), &format!("{base}/good"), "..\\evil.exe", &dir).await.unwrap_err();
        assert!(e.contains("bad installer name"), "{e}");
        assert!(!dir.join(name).exists(), "a failed verification writes nothing");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn an_installed_app_has_its_uninstaller_beside_it() {
        let d = tmp("installed");
        assert!(!is_installed(&d));
        std::fs::write(d.join("uninstall.exe"), b"x").unwrap();
        // Tests build with debug assertions off under --release, as shipped.
        assert_eq!(is_installed(&d), !cfg!(debug_assertions));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn bridge_install_copies_once_and_detects_stale_injection() {
        let appdata = tmp("bridge");
        assert!(install_bridge(&appdata, b"v2").is_err()); // no Spicetify
        std::fs::create_dir_all(appdata.join("spicetify").join("Extensions")).unwrap();
        let r = install_bridge(&appdata, b"v2").unwrap();
        assert!(r.copied && !r.needs_apply); // no injected copy: can't tell
        assert_eq!(std::fs::read(&r.path).unwrap(), b"v2");
        let inj = injected_bridge_path(&appdata);
        std::fs::create_dir_all(inj.parent().unwrap()).unwrap();
        std::fs::write(&inj, b"v1").unwrap();
        let r = install_bridge(&appdata, b"v2").unwrap();
        assert!(!r.copied && r.needs_apply);
        std::fs::write(&inj, b"v2").unwrap();
        assert!(!bridge_needs_apply(&appdata, b"v2"));
        let (s, b) = stage_repair_files(&appdata.join("stage")).unwrap();
        assert_eq!(std::fs::read(s).unwrap(), SETUP_PS1);
        assert_eq!(std::fs::read(b).unwrap(), BRIDGE_JS);
        let _ = std::fs::remove_dir_all(appdata);
    }
}
