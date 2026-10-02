//! The little bit of Win32 the window features need: a monitor's *work area*
//! (the desktop minus the taskbar, which Tauri's monitor API does not give)
//! and its DPI, plus "is the left mouse button down" (to tell the end of a
//! native window drag). Hand-declared FFI, so no extra crate. Off Windows
//! every function returns None / false and callers fall back to Tauri's
//! monitor rectangles.

use super::win_geom::Area;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MonitorInfo {
    pub work: Area,
    pub dpi: u32,
    /// false when (x, y) is on no monitor and the nearest one was used.
    pub found: bool,
}

#[cfg(windows)]
mod imp {
    use super::{Area, MonitorInfo};

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    struct Rect {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }
    #[repr(C)]
    struct Point {
        x: i32,
        y: i32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Mi {
        cb_size: u32,
        rc_monitor: Rect,
        rc_work: Rect,
        dw_flags: u32,
    }

    #[link(name = "user32")]
    extern "system" {
        fn MonitorFromPoint(pt: Point, flags: u32) -> isize;
        fn GetMonitorInfoW(h: isize, mi: *mut Mi) -> i32;
        fn GetAsyncKeyState(vk: i32) -> i16;
    }
    #[link(name = "shcore")]
    extern "system" {
        fn GetDpiForMonitor(h: isize, kind: i32, x: *mut u32, y: *mut u32) -> i32;
    }

    const DEFAULT_TO_NULL: u32 = 0;
    const DEFAULT_TO_PRIMARY: u32 = 1;
    const DEFAULT_TO_NEAREST: u32 = 2;
    const VK_LBUTTON: i32 = 0x01;

    pub fn monitor_at(x: i32, y: i32, primary: bool) -> Option<MonitorInfo> {
        unsafe {
            let flag = if primary { DEFAULT_TO_PRIMARY } else { DEFAULT_TO_NULL };
            let mut hm = MonitorFromPoint(Point { x, y }, flag);
            let found = hm != 0;
            if hm == 0 {
                hm = MonitorFromPoint(Point { x, y }, DEFAULT_TO_NEAREST);
            }
            if hm == 0 {
                return None;
            }
            let mut mi = Mi { cb_size: std::mem::size_of::<Mi>() as u32, ..Default::default() };
            if GetMonitorInfoW(hm, &mut mi) == 0 {
                return None;
            }
            let r = mi.rc_work;
            let (mut dx, mut dy) = (0u32, 0u32);
            let dpi = if GetDpiForMonitor(hm, 0, &mut dx, &mut dy) == 0 && dx > 0 { dx } else { 96 };
            let work: Area = (r.left, r.top, r.right, r.bottom);
            Some(MonitorInfo { work, dpi, found })
        }
    }

    pub fn left_button_down() -> bool {
        unsafe { (GetAsyncKeyState(VK_LBUTTON) as u16 & 0x8000) != 0 }
    }
}

/// Work area + DPI of the monitor at physical point (x, y). With `primary`,
/// an off-screen point resolves to the primary monitor instead of None/nearest.
pub fn monitor_at(x: i32, y: i32, primary: bool) -> Option<MonitorInfo> {
    #[cfg(windows)]
    {
        imp::monitor_at(x, y, primary)
    }
    #[cfg(not(windows))]
    {
        let _ = (x, y, primary);
        None
    }
}

pub fn left_button_down() -> bool {
    #[cfg(windows)]
    {
        imp::left_button_down()
    }
    #[cfg(not(windows))]
    {
        false
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    #[test]
    fn primary_monitor_has_a_sane_work_area() {
        // Needs a desktop session; skip silently when there is none (CI).
        let Some(m) = monitor_at(0, 0, true) else { return };
        assert!(m.work.2 > m.work.0 && m.work.3 > m.work.1);
        assert!(m.dpi >= 96);
        // The far-away point must resolve to some monitor via the nearest fallback.
        let far = monitor_at(-300_000, -300_000, false).unwrap();
        assert!(!far.found);
    }
}
