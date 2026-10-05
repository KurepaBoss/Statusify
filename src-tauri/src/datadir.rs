//! Where Statusify keeps its data: history.db, statusify.cfg, .env, the log,
//! the album-art cache and exports.
//!
//! Three places, first match wins:
//!   1. STATUSIFY_DATA_DIR, when set (tests, and anyone who wants it elsewhere);
//!   2. the folder the exe is in, when it already holds data of ours
//!      (history.db, statusify.cfg or .env). This is "portable mode": a copy
//!      of the exe set up that way keeps working exactly as it did, and an
//!      installed build never lands here because its folder has none of them;
//!   3. %APPDATA%\Statusify. Installed builds live under a per-user program
//!      folder that an update replaces wholesale, so their data goes beside
//!      it, in the user's roaming profile.
//!
//! Nothing is ever moved or copied between these places.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// A file of these names beside the exe means "portable".
pub const DATA_FILES: [&str; 3] = ["history.db", "statusify.cfg", ".env"];
/// The folder under %APPDATA%.
pub const APPDATA_FOLDER: &str = "Statusify";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// STATUSIFY_DATA_DIR.
    Env,
    /// Data files already sit beside the exe.
    Portable,
    /// %APPDATA%\Statusify.
    AppData,
}

impl Mode {
    /// A short id for the UI and the log.
    pub fn id(self) -> &'static str {
        match self {
            Mode::Env => "env",
            Mode::Portable => "portable",
            Mode::AppData => "appdata",
        }
    }

    /// What it means, for people.
    pub fn label(self) -> &'static str {
        match self {
            Mode::Env => "set by STATUSIFY_DATA_DIR",
            Mode::Portable => "portable: data files sit next to the app",
            Mode::AppData => "your user profile (%APPDATA%\\Statusify)",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub dir: PathBuf,
    pub mode: Mode,
}

/// The rules above, on explicit inputs (the unit tests drive this).
/// `env_dir`: STATUSIFY_DATA_DIR; `exe_dir`: the folder of the running exe;
/// `appdata`: %APPDATA%.
pub fn resolve(env_dir: Option<OsString>, exe_dir: Option<&Path>, appdata: Option<&Path>) -> Resolved {
    if let Some(d) = env_dir.filter(|d| !d.to_string_lossy().trim().is_empty()) {
        return Resolved { dir: PathBuf::from(d), mode: Mode::Env };
    }
    if let Some(exe) = exe_dir {
        if DATA_FILES.iter().any(|f| exe.join(f).is_file()) {
            return Resolved { dir: exe.to_path_buf(), mode: Mode::Portable };
        }
    }
    match (appdata.filter(|a| !a.as_os_str().is_empty()), exe_dir) {
        (Some(a), _) => Resolved { dir: a.join(APPDATA_FOLDER), mode: Mode::AppData },
        // No %APPDATA% at all (it always exists on a real Windows profile):
        // the exe's own folder is the only place left.
        (None, Some(exe)) => Resolved { dir: exe.to_path_buf(), mode: Mode::Portable },
        (None, None) => Resolved { dir: PathBuf::from("."), mode: Mode::Portable },
    }
}

/// The real environment: STATUSIFY_DATA_DIR, the running exe's folder, %APPDATA%.
pub fn choose() -> Resolved {
    let exe = std::env::current_exe().ok();
    let r = resolve(
        std::env::var_os("STATUSIFY_DATA_DIR"),
        exe.as_deref().and_then(|p| p.parent()),
        std::env::var_os("APPDATA").map(PathBuf::from).as_deref(),
    );
    let _ = MODE.set(r.mode);
    r
}

static MODE: OnceLock<Mode> = OnceLock::new();

/// The mode `choose()` picked in this process (None before it ran, as in unit tests).
pub fn current_mode() -> Option<Mode> {
    MODE.get().copied()
}

/// Make sure the folder exists (a fresh %APPDATA%\Statusify does not).
pub fn prepare(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)
}

/// Keep the log from growing for ever: past `max_bytes`, the current log
/// becomes `<name>.old` (replacing the previous one) and a fresh one starts.
/// Returns whether it rotated.
pub fn rotate_log(log: &Path, max_bytes: u64) -> bool {
    let big = std::fs::metadata(log).map(|m| m.len() > max_bytes).unwrap_or(false);
    if !big {
        return false;
    }
    let mut old = log.as_os_str().to_owned();
    old.push(".old");
    let old = PathBuf::from(old);
    let _ = std::fs::remove_file(&old);
    std::fs::rename(log, &old).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-datadir-{tag}-{}-{:?}", crate::state::now_ms(), std::thread::current().id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn the_environment_variable_wins_over_everything() {
        let exe = tmp("env-exe");
        std::fs::write(exe.join("history.db"), b"x").unwrap();
        let appdata = tmp("env-appdata");
        let r = resolve(Some(r"D:\somewhere\data".into()), Some(&exe), Some(&appdata));
        assert_eq!(r, Resolved { dir: PathBuf::from(r"D:\somewhere\data"), mode: Mode::Env });
        // An empty or blank value is "not set".
        for blank in ["", "   "] {
            let r = resolve(Some(blank.into()), Some(&exe), Some(&appdata));
            assert_eq!(r.mode, Mode::Portable, "{blank:?}");
        }
        let _ = std::fs::remove_dir_all(exe);
        let _ = std::fs::remove_dir_all(appdata);
    }

    #[test]
    fn data_files_beside_the_exe_mean_portable_mode() {
        let appdata = tmp("port-appdata");
        for file in DATA_FILES {
            let exe = tmp("port-exe");
            std::fs::write(exe.join(file), b"x").unwrap();
            let r = resolve(None, Some(&exe), Some(&appdata));
            assert_eq!(r, Resolved { dir: exe.clone(), mode: Mode::Portable }, "{file}");
            let _ = std::fs::remove_dir_all(exe);
        }
        let _ = std::fs::remove_dir_all(appdata);
    }

    #[test]
    fn an_installed_exe_with_no_data_beside_it_uses_appdata() {
        let exe = tmp("inst-exe");
        // Files that are not data of ours, and a folder called like a data file, do not count.
        std::fs::write(exe.join("statusify-rs.log"), b"x").unwrap();
        std::fs::write(exe.join("uninstall.exe"), b"x").unwrap();
        std::fs::create_dir_all(exe.join("history.db")).unwrap();
        let appdata = tmp("inst-appdata");
        let r = resolve(None, Some(&exe), Some(&appdata));
        assert_eq!(r, Resolved { dir: appdata.join("Statusify"), mode: Mode::AppData });
        // The data folder is not created by resolving: nothing is touched until prepare().
        assert!(!r.dir.exists());
        prepare(&r.dir).unwrap();
        assert!(r.dir.is_dir());
        prepare(&r.dir).unwrap(); // already there is fine
        let _ = std::fs::remove_dir_all(exe);
        let _ = std::fs::remove_dir_all(appdata);
    }

    #[test]
    fn without_an_appdata_folder_the_exe_folder_is_the_last_resort() {
        let exe = tmp("noapp-exe");
        assert_eq!(resolve(None, Some(&exe), None), Resolved { dir: exe.clone(), mode: Mode::Portable });
        assert_eq!(resolve(None, Some(&exe), Some(Path::new(""))).mode, Mode::Portable);
        assert_eq!(resolve(None, None, None).dir, PathBuf::from("."));
        let _ = std::fs::remove_dir_all(exe);
    }

    #[test]
    fn modes_have_a_stable_id_and_a_label() {
        assert_eq!(
            [Mode::Env, Mode::Portable, Mode::AppData].map(Mode::id),
            ["env", "portable", "appdata"]
        );
        assert!(Mode::AppData.label().contains("%APPDATA%\\Statusify"));
        assert!(Mode::Env.label().contains("STATUSIFY_DATA_DIR"));
    }

    #[test]
    fn the_log_rotates_once_it_is_big() {
        let d = tmp("log");
        let log = d.join("statusify-rs.log");
        assert!(!rotate_log(&log, 10), "no log yet");
        std::fs::write(&log, b"0123456789").unwrap();
        assert!(!rotate_log(&log, 10), "exactly at the limit stays");
        std::fs::write(&log, b"0123456789a").unwrap();
        std::fs::write(d.join("statusify-rs.log.old"), b"previous").unwrap();
        assert!(rotate_log(&log, 10));
        assert!(!log.exists());
        assert_eq!(std::fs::read(d.join("statusify-rs.log.old")).unwrap(), b"0123456789a");
        let _ = std::fs::remove_dir_all(d);
    }
}
