//! "Start with Windows": this app's own entry in HKCU\...\Run.
//!
//! The entry is called `StatusifyDesktop`, never `Statusify`. The old Python
//! app cleans up "leftover registry Run entries from previous versions" by
//! deleting a Run value named exactly `Statusify` every time ITS Start with
//! Windows switch is flipped, either way (statusify_startup._cleanup_old_startup).
//! An entry of ours under that name would be wiped the first time someone
//! toggled the old app, and the two apps' switches would silently undo each
//! other. Under its own name each app's switch touches only its own entry; the
//! old app keeps a Startup-folder shortcut, which this app never touches
//! either, except when the user asks it to ("Turn off the old one").
//!
//! The first builds of this app did register as `Statusify`. That value is
//! moved to the new name, once, but only when it starts this very exe: a
//! `Statusify` value that starts anything else belongs to someone else.

use std::path::Path;
use windows_registry::{Key, CURRENT_USER};

/// What this app's autostart entry is called.
pub const RUN_VALUE: &str = "StatusifyDesktop";
/// What the first builds called theirs, and what the Python app still deletes.
pub const LEGACY_VALUE: &str = "Statusify";

const RUN_PATH: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
/// Task Manager's Startup tab keeps its enabled/disabled flag here, by value name.
const APPROVED_PATH: &str = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";

/// The two registry keys involved. `real()` is the user's; tests point it at
/// a scratch key so nothing real is ever touched.
pub struct RunKeys {
    run: String,
    approved: String,
}

impl RunKeys {
    pub fn real() -> Self {
        RunKeys { run: RUN_PATH.into(), approved: APPROVED_PATH.into() }
    }

    /// Scratch keys under HKCU\<root>.
    #[cfg(test)]
    fn scratch(root: &str) -> Self {
        RunKeys { run: format!(r"{root}\Run"), approved: format!(r"{root}\Approved") }
    }

    fn open(&self, path: &str) -> Option<Key> {
        CURRENT_USER.options().read().write().open(path).ok()
    }

    /// The command line of a Run value, if there is one.
    pub fn command(&self, name: &str) -> Option<String> {
        self.open(&self.run)?.get_string(name).ok()
    }

    /// Delete a Run value and its Task Manager flag. True when the Run value existed.
    pub fn remove(&self, name: &str) -> bool {
        let existed = self.open(&self.run).is_some_and(|k| k.remove_value(name).is_ok());
        if let Some(k) = self.open(&self.approved) {
            let _ = k.remove_value(name);
        }
        existed
    }

    #[cfg(test)]
    fn set(&self, name: &str, command: &str) {
        CURRENT_USER.create(&self.run).unwrap().set_string(name, command).unwrap();
        CURRENT_USER.create(&self.approved).unwrap().set_bytes(name, windows_registry::Type::Bytes, &[2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).unwrap();
    }

    #[cfg(test)]
    fn approved(&self, name: &str) -> bool {
        self.open(&self.approved).is_some_and(|k| k.get_value(name).is_ok())
    }

    #[cfg(test)]
    fn wipe(root: &str) {
        let _ = CURRENT_USER.remove_tree(root);
    }
}

/// Whether a Run command line starts `exe` (quoted or not, any arguments after).
pub fn command_targets(command: &str, exe: &Path) -> bool {
    let cmd = command.trim().to_lowercase().replace('/', "\\");
    let exe = exe.to_string_lossy().to_lowercase().replace('/', "\\");
    let cmd = cmd.strip_prefix('"').unwrap_or(&cmd);
    cmd.strip_prefix(exe.as_str()).is_some_and(|rest| rest.is_empty() || rest.starts_with(['"', ' ']))
}

#[derive(Debug, PartialEq, Eq)]
pub enum Migration {
    /// There was no `Statusify` Run value.
    NothingToDo,
    /// The value started this exe; it now lives under `StatusifyDesktop`.
    Moved,
    /// Both names were there for this exe; the old one was dropped.
    DroppedDuplicate,
    /// `Statusify` starts something else (the Python app, another copy): not ours, untouched.
    NotOurs,
    Failed(String),
}

/// Move the first builds' `Statusify` entry to `StatusifyDesktop`. `enable`
/// registers the new entry (the autostart plugin, in the app).
pub fn migrate_legacy(keys: &RunKeys, exe: &Path, enable: impl FnOnce() -> Result<(), String>) -> Migration {
    let Some(old) = keys.command(LEGACY_VALUE) else { return Migration::NothingToDo };
    if !command_targets(&old, exe) {
        return Migration::NotOurs;
    }
    let had_new = keys.command(RUN_VALUE).is_some();
    if !had_new {
        if let Err(e) = enable() {
            return Migration::Failed(e);
        }
    }
    keys.remove(LEGACY_VALUE);
    if had_new {
        Migration::DroppedDuplicate
    } else {
        Migration::Moved
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// A scratch registry root that is deleted again afterwards.
    struct Scratch(String, RunKeys);
    impl Scratch {
        fn new(tag: &str) -> Self {
            // Directly under HKCU\Software, so deleting it leaves nothing behind.
            let root = format!(r"Software\StatusifyTest-{tag}-{}-{:?}", crate::state::now_ms(), std::thread::current().id()).replace(['(', ')'], "");
            let keys = RunKeys::scratch(&root);
            Scratch(root, keys)
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            RunKeys::wipe(&self.0);
        }
    }

    const EXE: &str = r"C:\Users\x\AppData\Local\Statusify\Statusify.exe";

    #[test]
    fn our_entry_has_its_own_name_which_the_python_app_never_deletes() {
        assert_ne!(RUN_VALUE, LEGACY_VALUE);
        assert!(RUN_VALUE.starts_with("Statusify"), "clearly ours");
        let s = Scratch::new("python-toggle");
        s.1.set(RUN_VALUE, &format!("\"{EXE}\""));
        // What statusify_startup._cleanup_old_startup does on every toggle of the old app:
        // delete the Run value called "Statusify".
        assert!(!s.1.remove("Statusify"), "nothing of that name to delete");
        assert_eq!(s.1.command(RUN_VALUE).as_deref(), Some(format!("\"{EXE}\"").as_str()));
        assert!(s.1.approved(RUN_VALUE));
        // And this app's own toggle removes its own value, and only that.
        s.1.set("Statusify", r"C:\old\python\Statusify.exe");
        assert!(s.1.remove(RUN_VALUE));
        assert!(s.1.command(RUN_VALUE).is_none() && !s.1.approved(RUN_VALUE));
        assert!(s.1.command("Statusify").is_some(), "the other app's entry is left alone");
    }

    #[test]
    fn a_run_command_is_matched_to_an_exe() {
        let exe = Path::new(EXE);
        assert!(command_targets(&format!("\"{EXE}\""), exe));
        assert!(command_targets(&format!("\"{EXE}\" "), exe)); // the plugin appends " " + args
        assert!(command_targets(&format!("{EXE} --minimized"), exe));
        assert!(command_targets(EXE, exe));
        assert!(command_targets(&format!("\"{}\"", EXE.to_uppercase()), exe));
        assert!(command_targets(&format!("\"{}\"", EXE.replace('\\', "/")), exe));
        assert!(!command_targets(r"C:\Users\x\AppData\Local\Statusify\Statusify.exe.bak", exe));
        assert!(!command_targets(r"C:\Users\x\AppData\Local\Statusify\Statusify2.exe", exe));
        assert!(!command_targets(r#""C:\Other\Statusify.exe""#, exe));
        assert!(!command_targets("", exe));
    }

    #[test]
    fn the_first_builds_entry_moves_to_the_new_name() {
        let s = Scratch::new("move");
        let exe = PathBuf::from(EXE);
        assert_eq!(migrate_legacy(&s.1, &exe, || panic!("nothing to enable")), Migration::NothingToDo);

        s.1.set("Statusify", &format!("\"{EXE}\" "));
        let mut enabled = false;
        let r = migrate_legacy(&s.1, &exe, || {
            enabled = true;
            s.1.set(RUN_VALUE, &format!("\"{EXE}\" "));
            Ok(())
        });
        assert_eq!(r, Migration::Moved);
        assert!(enabled);
        assert!(s.1.command("Statusify").is_none() && !s.1.approved("Statusify"));
        assert!(s.1.command(RUN_VALUE).is_some());
        // Nothing left to migrate the next time.
        assert_eq!(migrate_legacy(&s.1, &exe, || panic!("done already")), Migration::NothingToDo);
    }

    #[test]
    fn an_old_entry_that_starts_something_else_is_not_touched() {
        let s = Scratch::new("foreign");
        s.1.set("Statusify", r#""C:\Users\x\AppData\Local\Programs\Statusify\Statusify.exe""#);
        let r = migrate_legacy(&s.1, Path::new(EXE), || panic!("must not enable"));
        assert_eq!(r, Migration::NotOurs);
        assert!(s.1.command("Statusify").is_some());
        assert!(s.1.command(RUN_VALUE).is_none());
    }

    #[test]
    fn both_names_for_this_exe_collapse_to_the_new_one_and_failures_keep_the_old() {
        let s = Scratch::new("dup");
        s.1.set("Statusify", EXE);
        s.1.set(RUN_VALUE, EXE);
        assert_eq!(migrate_legacy(&s.1, Path::new(EXE), || panic!("already enabled")), Migration::DroppedDuplicate);
        assert!(s.1.command("Statusify").is_none() && s.1.command(RUN_VALUE).is_some());

        let s = Scratch::new("fail");
        s.1.set("Statusify", EXE);
        let r = migrate_legacy(&s.1, Path::new(EXE), || Err("registry said no".into()));
        assert_eq!(r, Migration::Failed("registry said no".into()));
        assert!(s.1.command("Statusify").is_some(), "never leave the user with no entry at all");
    }
}
