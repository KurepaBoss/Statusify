//! Turns the engine's changes into as few UI events as the eye can tell apart.
//!
//! Every `Engine` change used to be one Tauri `emit` of the whole snapshot
//! (about 9 KB of JSON, serialised once per window): a bridge position
//! message twice a second, every feature's `set_extra`, and bursts of them at
//! a track change (track, lyrics, queue, palette, timing, core state within a
//! few ms of each other). The emitter marks what changed and a task sends it:
//! a `Full` change goes out at once, and further ones inside `GAP` are folded
//! into one more emit at the end of the gap (nothing is lost: the emit reads
//! the engine's current state, not the state at the mark). A `Position`
//! change is four fields and goes out as the small "position" event unless a
//! full snapshot is going out anyway.
use crate::engine::Change;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tokio::sync::Notify;

/// Changes closer together than this share one snapshot emit. One display
/// frame: the UI could not have painted them apart.
pub const GAP: Duration = Duration::from_millis(16);

#[derive(Default)]
pub struct Emitter {
    full: AtomicBool,
    position: AtomicBool,
    wake: Notify,
}

impl Emitter {
    /// The engine's callback: remember the change, wake the sender.
    pub fn mark(&self, c: Change) {
        match c {
            Change::Full => self.full.store(true, Ordering::Release),
            Change::Position => self.position.store(true, Ordering::Release),
        }
        self.wake.notify_one();
    }

    /// What is pending, most complete first; clears it.
    fn take(&self) -> Option<Change> {
        if self.full.swap(false, Ordering::AcqRel) {
            self.position.store(false, Ordering::Release);
            Some(Change::Full)
        } else if self.position.swap(false, Ordering::AcqRel) {
            Some(Change::Position)
        } else {
            None
        }
    }

    /// Sends forever. `emit` is given the kind to send; it reads the engine.
    pub async fn run(&self, gap: Duration, mut emit: impl FnMut(Change)) {
        loop {
            self.wake.notified().await;
            while let Some(c) = self.take() {
                emit(c);
                if c == Change::Full {
                    tokio::time::sleep(gap).await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    fn start(gap: Duration) -> (Arc<Emitter>, Arc<Mutex<Vec<(Instant, Change)>>>) {
        let em = Arc::new(Emitter::default());
        let log = Arc::new(Mutex::new(Vec::new()));
        let (e, l) = (em.clone(), log.clone());
        tokio::spawn(async move { e.run(gap, |c| l.lock().unwrap().push((Instant::now(), c))).await });
        (em, log)
    }

    #[tokio::test]
    async fn a_burst_of_changes_is_one_emit_now_and_one_at_the_end_of_the_gap() {
        let (em, log) = start(Duration::from_millis(60));
        let t0 = Instant::now();
        // Yield, not sleep, between the marks: a timer on Windows ticks every
        // 15.6 ms, which would spread the burst over several gaps.
        for _ in 0..20 {
            em.mark(Change::Full);
            tokio::task::yield_now().await;
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
        let got = log.lock().unwrap().clone();
        let kinds: Vec<Change> = got.iter().map(|x| x.1).collect();
        assert_eq!(kinds, vec![Change::Full, Change::Full], "{got:?}");
        assert!(got[0].0.duration_since(t0) < Duration::from_millis(30), "the first goes out at once");
        assert!(got[1].0.duration_since(t0) >= Duration::from_millis(55), "the rest wait for the gap");
    }

    #[tokio::test]
    async fn a_position_alone_is_a_position_event_and_is_folded_into_a_full_one() {
        let (em, log) = start(Duration::from_millis(30));
        em.mark(Change::Position);
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert_eq!(log.lock().unwrap().iter().map(|x| x.1).collect::<Vec<_>>(), vec![Change::Position]);
        // A full emit is pending (inside the gap of the one before): the
        // position rides along with it.
        em.mark(Change::Full);
        em.mark(Change::Position);
        em.mark(Change::Full);
        tokio::time::sleep(Duration::from_millis(60)).await;
        let kinds: Vec<Change> = log.lock().unwrap().iter().map(|x| x.1).collect();
        assert_eq!(kinds, vec![Change::Position, Change::Full]);
    }

    #[tokio::test]
    async fn quiet_means_no_emits() {
        let (_em, log) = start(GAP);
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(log.lock().unwrap().is_empty());
    }
}
