//! The last words of a crash.
//!
//! The release profile is `panic = "abort"`: a panic on any task or thread ends
//! the process on the spot, with no unwinding, and with the window subsystem
//! nobody sees stderr, so by default the log would simply stop. The hook below
//! appends one entry to the log file (thread, source location, message, and a
//! backtrace when RUST_BACKTRACE asks for one), then runs the previous hook and
//! lets the process abort as before.
//!
//! It must never wait for anything: it runs on the thread that panicked, which
//! may be the one holding the log's lock, so it only try-locks, a few times.

use std::any::Any;
use std::fs::File;
use std::io::Write;
use std::sync::{Mutex, TryLockError};
use std::time::Duration;

/// How often, and how far apart, the hook tries the log's lock (100 ms in all).
const TRIES: u32 = 20;
const RETRY: Duration = Duration::from_millis(5);

/// Call first thing in `run()`. Entries go to the file `crate::log` writes to,
/// once that exists; before that there is nowhere to write them.
pub fn install() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let thread = std::thread::current();
        let at = info.location().map(|l| (l.file(), l.line(), l.column()));
        let mut entry = describe(thread.name().unwrap_or("unnamed"), at, info.payload());
        // Honours RUST_BACKTRACE and is free when it is unset. (A forced one
        // would resolve symbols through dbghelp, which can block.)
        let trace = std::backtrace::Backtrace::capture();
        if trace.status() == std::backtrace::BacktraceStatus::Captured {
            entry.push_str(&format!("\n{trace}"));
        }
        if let Some(log) = crate::LOG.get() {
            append(log, &entry);
        }
        previous(info);
    }));
}

/// "PANIC on thread 'x' at src/y.rs:12:5: the message".
fn describe(thread: &str, at: Option<(&str, u32, u32)>, payload: &(dyn Any + Send)) -> String {
    let msg = payload
        .downcast_ref::<&str>()
        .map(|s| (*s).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "(no message)".to_string());
    match at {
        Some((file, line, col)) => format!("PANIC on thread '{thread}' at {}:{line}:{col}: {msg}", short_path(file)),
        None => format!("PANIC on thread '{thread}': {msg}"),
    }
}

/// A dependency's source file carries the cargo registry's path, which starts
/// with the user's home folder. Keep what comes after the registry's index folder.
fn short_path(file: &str) -> String {
    const REGISTRY: &str = "/registry/src/";
    let f = file.replace('\\', "/");
    match f.find(REGISTRY) {
        Some(i) => {
            let rest = &f[i + REGISTRY.len()..];
            rest.split_once('/').map_or(rest, |(_, after_index)| after_index).to_string()
        }
        None => f,
    }
}

/// One timestamped entry at the end of the log, in the same shape as `crate::log`.
/// If the lock stays taken for the whole of the retries, the entry is dropped
/// rather than waiting on a thread that may be this one.
fn append(log: &Mutex<Option<File>>, entry: &str) {
    let text = format!("{}  {}\n", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), entry);
    for _ in 0..TRIES {
        let mut guard = match log.try_lock() {
            Ok(g) => g,
            Err(TryLockError::Poisoned(p)) => p.into_inner(),
            Err(TryLockError::WouldBlock) => {
                std::thread::sleep(RETRY);
                continue;
            }
        };
        if let Some(f) = guard.as_mut() {
            let _ = f.write_all(text.as_bytes());
        }
        return;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::Arc;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("statusify-panic-{tag}-{}-{:?}", crate::state::now_ms(), std::thread::current().id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn log_in(dir: &std::path::Path) -> Mutex<Option<File>> {
        Mutex::new(Some(std::fs::OpenOptions::new().create(true).append(true).open(dir.join("statusify-rs.log")).unwrap()))
    }

    fn text_of(dir: &std::path::Path) -> String {
        std::fs::read_to_string(dir.join("statusify-rs.log")).unwrap()
    }

    #[test]
    fn the_entry_names_the_thread_the_place_and_the_message() {
        let literal: &(dyn Any + Send) = &"index out of bounds";
        let formatted: &(dyn Any + Send) = &String::from("slot 7 of 5");
        let odd: &(dyn Any + Send) = &42_i32;
        assert_eq!(
            describe("tokio-runtime-worker", Some(("src\\presence.rs", 431, 9)), literal),
            "PANIC on thread 'tokio-runtime-worker' at src/presence.rs:431:9: index out of bounds"
        );
        assert_eq!(describe("main", Some(("src/lib.rs", 1, 2)), formatted), "PANIC on thread 'main' at src/lib.rs:1:2: slot 7 of 5");
        assert_eq!(describe("unnamed", None, odd), "PANIC on thread 'unnamed': (no message)");
    }

    #[test]
    fn a_dependencys_path_loses_the_home_folder() {
        assert_eq!(
            short_path("C:\\Users\\someone\\.cargo\\registry\\src\\index.crates.io-1949cf8c6b5b557f\\tokio-1.40.0\\src\\runtime\\task\\mod.rs"),
            "tokio-1.40.0/src/runtime/task/mod.rs"
        );
        assert_eq!(short_path("/home/x/.cargo/registry/src/index.crates.io-abc/serde-1.0.0/src/lib.rs"), "serde-1.0.0/src/lib.rs");
        assert_eq!(short_path("src\\engine.rs"), "src/engine.rs");
        assert_eq!(short_path("/rustc/abc123/library/core/src/option.rs"), "/rustc/abc123/library/core/src/option.rs");
    }

    #[test]
    fn an_entry_is_appended_with_a_timestamp_like_the_rest_of_the_log() {
        let d = tmp("append");
        std::fs::write(d.join("statusify-rs.log"), "earlier line\n").unwrap();
        let log = log_in(&d);
        append(&log, "PANIC on thread 'a': first");
        append(&log, "PANIC on thread 'b': second");
        let t = text_of(&d);
        let lines: Vec<&str> = t.lines().collect();
        assert_eq!(lines[0], "earlier line");
        assert_eq!(lines.len(), 3);
        // "2026-10-05 12:00:00  PANIC ..."
        for (line, want) in lines[1..].iter().zip(["first", "second"]) {
            assert!(line.as_bytes()[4] == b'-' && line.as_bytes()[13] == b':', "{line}");
            assert!(line[19..].starts_with("  PANIC on thread"), "{line}");
            assert!(line.ends_with(want), "{line}");
        }
        // No file open (the data folder could not be used): nothing to write, and no panic of its own.
        append(&Mutex::new(None), "nowhere to go");
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn it_waits_a_moment_for_the_lock_but_never_for_good() {
        let d = tmp("lock");
        let log = Arc::new(log_in(&d));

        // Held briefly by another thread: the entry still lands.
        let l = log.clone();
        let holder = std::thread::spawn(move || {
            let _g = l.lock().unwrap();
            std::thread::sleep(Duration::from_millis(30));
        });
        std::thread::sleep(Duration::from_millis(10));
        append(&log, "PANIC after a short wait");
        holder.join().unwrap();
        assert!(text_of(&d).contains("after a short wait"));

        // Held for longer than the retries (or by the panicking thread itself):
        // the entry is dropped and the call returns.
        let held = log.lock().unwrap();
        let started = std::time::Instant::now();
        append(&log, "PANIC while locked for good");
        assert!(started.elapsed() < Duration::from_secs(2), "append must not block");
        drop(held);
        assert!(!text_of(&d).contains("locked for good"));
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn a_poisoned_lock_is_written_through() {
        let d = tmp("poison");
        let log = Arc::new(log_in(&d));
        let l = log.clone();
        let _ = std::thread::spawn(move || {
            let _g = l.lock().unwrap();
            panic!("poison it");
        })
        .join();
        assert!(log.is_poisoned());
        append(&log, "PANIC despite the poison");
        assert!(text_of(&d).contains("despite the poison"));
        let _ = std::fs::remove_dir_all(d);
    }

    /// The installed hook, end to end. The test binary runs itself once more
    /// (the hook is process-wide, so it cannot be installed in the shared test
    /// process); in that child a thread named "doomed" panics and the hook has to
    /// have written the entry to the log file by the time the parent looks.
    #[test]
    fn the_installed_hook_writes_to_the_log_file() {
        let d = tmp("hook");
        let out = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "panic_log::tests::child_panics_with_the_hook_installed", "--test-threads=1"])
            .env("STATUSIFY_PANIC_LOG_TEST_DIR", &d)
            .env_remove("RUST_BACKTRACE")
            .output()
            .unwrap();
        assert!(out.status.success(), "child failed: {}", String::from_utf8_lossy(&out.stdout));
        let t = text_of(&d);
        assert!(t.contains("PANIC on thread 'doomed' at src/panic_log.rs:"), "{t}");
        assert!(t.contains("the connection task blew up"), "{t}");
        assert_eq!(t.lines().count(), 1, "{t}");
        let _ = std::fs::remove_dir_all(d);
    }

    /// Only does anything when the test above started it; a no-op otherwise.
    #[test]
    fn child_panics_with_the_hook_installed() {
        let Some(dir) = std::env::var_os("STATUSIFY_PANIC_LOG_TEST_DIR") else { return };
        let _ = crate::LOG.set(log_in(std::path::Path::new(&dir)));
        install();
        let r = std::thread::Builder::new().name("doomed".into()).spawn(|| panic!("the connection task blew up")).unwrap().join();
        assert!(r.is_err());
    }
}
