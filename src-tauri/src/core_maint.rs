//! In-app update and Spicetify-bridge repair (port of statusify_maintenance.py
//! plus main.py's _check_for_updates / _install_bridge / _bridge_needs_apply
//! and statusify_bridge.spotify_running).

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const RELEASES_URL: &str = "https://api.github.com/repos/KurepaBoss/Statusify/releases";
/// The version update checks compare against (and the LRCLIB User-Agent
/// reports): the Statusify release this rewrite replaces (version.py), NOT
/// the crate's 0.1.0, which would offer every 1.x/2.x release as an update.
pub const APP_VERSION: &str = "2.2.0";
/// Releases install silently only from an installer built for this rewrite.
/// The Python app's Inno Setup "Statusify-Setup-<tag>.exe" would replace it
/// with the Python app, so such a release opens its download page instead.
pub const SETUP_PREFIX: &str = "Statusify-rs-Setup-";

/// The bridge and the Spicetify setup script we ship (byte-exact copies).
pub const BRIDGE_JS: &[u8] = include_bytes!("../resources/lyrics-bridge.js");
pub const SETUP_PS1: &[u8] = include_bytes!("../resources/setup-spicetify.ps1");

const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED: u32 = 0x0000_0008 | 0x0000_0200; // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

// ── Updates ───────────────────────────────────────────────────────

/// "2.10.1" -> [2, 10, 1]; anything unparsable compares as 0.0.0 (as Python's _sv).
pub fn version_key(v: &str) -> Vec<u64> {
    let parts: Result<Vec<u64>, _> = v.trim().trim_start_matches('v').split('.').map(|x| x.parse::<u64>()).collect();
    parts.unwrap_or_else(|_| vec![0, 0, 0])
}

/// (setup_url, sha256_url) for this release's installer, or None. Both must
/// exist: without a published checksum nothing can verify the download.
pub fn setup_asset(release: &Value) -> Option<(String, String)> {
    let tag = release.get("tag_name").and_then(|t| t.as_str()).unwrap_or("").trim_start_matches('v');
    let want = format!("{SETUP_PREFIX}{tag}.exe");
    let assets = release.get("assets").and_then(|a| a.as_array())?;
    let url = |name: &str| {
        assets
            .iter()
            .find(|a| a.get("name").and_then(|n| n.as_str()) == Some(name))
            .and_then(|a| a.get("browser_download_url").and_then(|u| u.as_str()))
            .map(str::to_string)
    };
    Some((url(&want)?, url(&format!("{want}.sha256"))?))
}

/// The newest release above `current`, with every newer release's notes
/// compiled into one changelog. None when up to date.
pub fn find_update(releases: &Value, current: &str) -> Option<Value> {
    let cur = version_key(current);
    let mut latest: Option<(String, String, Option<(String, String)>)> = None;
    let mut notes: Vec<String> = Vec::new();
    for r in releases.as_array()? {
        let tag = r.get("tag_name").and_then(|t| t.as_str()).unwrap_or("").trim_start_matches('v');
        if tag.is_empty() || version_key(tag) <= cur {
            continue;
        }
        if latest.is_none() {
            let url = r.get("html_url").and_then(|u| u.as_str()).unwrap_or("").to_string();
            latest = Some((tag.to_string(), url, setup_asset(r)));
        }
        notes.push(format!("• v{tag}"));
        let body = r.get("body").and_then(|b| b.as_str()).unwrap_or("").trim();
        for ln in body.lines().filter(|l| !l.trim().is_empty()) {
            notes.push(format!("  {ln}"));
        }
        notes.push(String::new());
    }
    let (tag, url, setup) = latest?;
    Some(json!({
        "tag": tag,
        "url": url,
        "changelog": notes.join("\n").trim().to_string(),
        "setup": setup.map(|(a, b)| json!({"url": a, "sha256_url": b})),
    }))
}

pub async fn check_update(client: &reqwest::Client, url: &str, current: &str) -> Result<Option<Value>, String> {
    let r = client
        .get(url)
        .header("User-Agent", "Statusify/UpdateChecker")
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !r.status().is_success() {
        return Err(format!("HTTP {}", r.status()));
    }
    let data: Value = r.json().await.map_err(|e| e.to_string())?;
    Ok(find_update(&data, current))
}

/// The first 64-hex-digit token in a .sha256 file.
pub fn parse_sha256(text: &str) -> Option<String> {
    text.split(|c: char| !c.is_ascii_hexdigit()).find(|t| t.len() == 64).map(|t| t.to_lowercase())
}

/// Download `url` into `dest_dir`, only if its SHA-256 matches the published
/// checksum; otherwise nothing is written.
pub async fn download_verified(client: &reqwest::Client, url: &str, sha_url: &str, dest_dir: &Path) -> Result<PathBuf, String> {
    use sha2::{Digest, Sha256};
    let get = |u: String, secs: u64| {
        let c = client.clone();
        async move {
            let r = c.get(&u).header("User-Agent", "Statusify/Updater").timeout(Duration::from_secs(secs)).send().await.map_err(|e| e.to_string())?;
            if !r.status().is_success() {
                return Err(format!("HTTP {}", r.status()));
            }
            r.bytes().await.map_err(|e| e.to_string())
        }
    };
    let sums = get(sha_url.to_string(), 30).await?;
    let expected = parse_sha256(&String::from_utf8_lossy(&sums)).ok_or("checksum file has no SHA-256")?;
    let data = get(url.to_string(), 120).await?;
    let actual: String = Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect();
    if actual != expected {
        return Err(format!("download does not match published checksum {}…", &expected[..12]));
    }
    std::fs::create_dir_all(dest_dir).map_err(|e| e.to_string())?;
    let name = url.split('?').next().and_then(|u| u.rsplit('/').next()).filter(|n| !n.is_empty()).unwrap_or("Statusify-Setup.exe");
    let path = dest_dir.join(name);
    std::fs::write(&path, &data).map_err(|e| e.to_string())?;
    Ok(path)
}

/// A Setup.exe install (the uninstaller sits beside the app) in a release
/// build. Portable exes and dev builds update through the browser.
pub fn is_installed(app_dir: &Path) -> bool {
    !cfg!(debug_assertions) && (app_dir.join("unins000.exe").exists() || app_dir.join("uninstall.exe").exists())
}

/// Run the installer silently a few seconds from now, detached, so this
/// process can exit first (Setup refuses to run while we hold our mutex).
pub fn launch_silent_update(setup: &Path) -> std::io::Result<()> {
    use std::os::windows::process::CommandExt;
    let cmd = format!(
        "Start-Sleep -Seconds 3; & '{}' /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS",
        setup.display().to_string().replace('\'', "''")
    );
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

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-maint-{tag}-{}", crate::state::now_ms()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn versions_compare_like_python_tuples() {
        assert!(version_key("2.10.0") > version_key("2.9.9"));
        assert!(version_key("v2.2.0") == version_key("2.2.0"));
        assert!(version_key("2.2") < version_key("2.2.0"));
        assert_eq!(version_key("beta"), vec![0, 0, 0]);
    }

    #[test]
    fn update_found_with_changelog_and_installer() {
        let rel = json!([
            {"tag_name": "v2.4.0", "html_url": "https://x/2.4.0", "body": "- new\n\n- more",
             "assets": [{"name": "Statusify-rs-Setup-2.4.0.exe", "browser_download_url": "https://x/s.exe"},
                        {"name": "Statusify-rs-Setup-2.4.0.exe.sha256", "browser_download_url": "https://x/s.sha256"}]},
            {"tag_name": "v2.3.0", "html_url": "https://x/2.3.0", "body": "fix",
             "assets": [{"name": "Statusify-Setup-2.3.0.exe", "browser_download_url": "https://x/py.exe"},
                        {"name": "Statusify-Setup-2.3.0.exe.sha256", "browser_download_url": "https://x/py.sha256"}]},
            {"tag_name": "v2.2.0", "body": "old"},
        ]);
        let u = find_update(&rel, "2.2.0").unwrap();
        assert_eq!(u["tag"], "2.4.0");
        assert_eq!(u["url"], "https://x/2.4.0");
        assert_eq!(u["setup"]["sha256_url"], "https://x/s.sha256");
        assert_eq!(u["changelog"], "• v2.4.0\n  - new\n  - more\n\n• v2.3.0\n  fix");
        assert!(find_update(&rel, "2.4.0").is_none());
        // the Python app's installer is never run silently over this app
        assert!(setup_asset(&rel[1]).is_none());
        // no checksum published: no silent install
        assert!(setup_asset(&json!({"tag_name": "v2.5.0", "assets": [
            {"name": "Statusify-rs-Setup-2.5.0.exe", "browser_download_url": "https://x/s.exe"}]})).is_none());
        // this build is 2.2.0, so the Python app's own releases up to it are no update
        assert_eq!(APP_VERSION, "2.2.0");
        assert!(find_update(&json!([{"tag_name": "v2.2.0"}, {"tag_name": "v1.2.0"}]), APP_VERSION).is_none());
    }

    #[tokio::test]
    async fn update_check_against_fake_github() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/releases", l.local_addr().unwrap());
        tokio::spawn(async move {
            let (mut c, _) = l.accept().await.unwrap();
            let mut buf = [0u8; 2048];
            let _ = c.read(&mut buf).await;
            let body = r#"[{"tag_name":"v9.0.0","html_url":"u","body":"b"}]"#;
            let r = format!("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
            let _ = c.write_all(r.as_bytes()).await;
        });
        let u = check_update(&reqwest::Client::new(), &url, "1.0.0").await.unwrap().unwrap();
        assert_eq!(u["tag"], "9.0.0");
    }

    #[test]
    fn sha256_token_is_found() {
        let h = "A".repeat(64);
        assert_eq!(parse_sha256(&format!("{h}  Statusify-Setup.exe\n")), Some("a".repeat(64)));
        assert_eq!(parse_sha256("nothing here"), None);
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
