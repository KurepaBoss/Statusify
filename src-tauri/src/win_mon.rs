//! The little bit of Win32 the window features need: a monitor's *work area*
//! (the desktop minus the taskbar, which Tauri's monitor API does not give)
//! and its DPI, "is the left mouse button down" (to tell the end of a native
//! window drag), the pointer position, and a few window-style tweaks tao does
//! not offer (stay out of Alt+Tab, re-assert topmost, clip to a pill).
//! Hand-declared FFI, so no extra crate. Off Windows every function returns
//! None / false / does nothing and callers fall back to Tauri's monitor
//! rectangles.

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
    pub struct Point {
        pub x: i32,
        pub y: i32,
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
        fn GetCursorPos(pt: *mut Point) -> i32;
        fn GetWindowLongPtrW(h: isize, idx: i32) -> isize;
        fn SetWindowLongPtrW(h: isize, idx: i32, v: isize) -> isize;
        fn SetWindowPos(h: isize, after: isize, x: i32, y: i32, cx: i32, cy: i32, flags: u32) -> i32;
        fn SetWindowRgn(h: isize, rgn: isize, redraw: i32) -> i32;
    }
    #[link(name = "gdi32")]
    extern "system" {
        fn CreateRoundRectRgn(l: i32, t: i32, r: i32, b: i32, w: i32, h: i32) -> isize;
    }
    #[link(name = "shcore")]
    extern "system" {
        fn GetDpiForMonitor(h: isize, kind: i32, x: *mut u32, y: *mut u32) -> i32;
    }

    const DEFAULT_TO_NULL: u32 = 0;
    const DEFAULT_TO_PRIMARY: u32 = 1;
    const DEFAULT_TO_NEAREST: u32 = 2;
    const VK_LBUTTON: i32 = 0x01;
    pub const GWL_EXSTYLE: i32 = -20;
    #[cfg(test)]
    pub const WS_EX_TOPMOST: isize = 0x8;
    pub const WS_EX_TOOLWINDOW: isize = 0x80;
    pub const WS_EX_APPWINDOW: isize = 0x4_0000;
    const HWND_TOPMOST: isize = -1;
    const SWP_NOSIZE: u32 = 0x1;
    const SWP_NOMOVE: u32 = 0x2;
    const SWP_NOACTIVATE: u32 = 0x10;
    const SWP_FRAMECHANGED: u32 = 0x20;

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

    pub fn cursor_pos() -> Option<(i32, i32)> {
        let mut p = Point { x: 0, y: 0 };
        (unsafe { GetCursorPos(&mut p) } != 0).then_some((p.x, p.y))
    }

    #[cfg(test)]
    pub fn ex_style(hwnd: isize) -> isize {
        unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) }
    }

    /// Alt+Tab / Win+Tab skip tool windows; strip WS_EX_APPWINDOW too.
    pub fn hide_from_switcher(hwnd: isize) {
        unsafe {
            let cur = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
            let ex = (cur | WS_EX_TOOLWINDOW) & !WS_EX_APPWINDOW;
            if ex == cur {
                return;
            }
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex);
            SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED);
        }
    }

    /// Re-assert HWND_TOPMOST (tao only does it once, when the flag changes).
    pub fn raise_topmost(hwnd: isize) {
        unsafe {
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
    }

    /// Clip the window to a w x h pill so its transparent corners stop
    /// catching clicks (Python's colour key did the same). The region is 1 px
    /// roomier than the pill so the anti-aliased rim survives.
    pub fn round_region(hwnd: isize, w: i32, h: i32) {
        unsafe {
            let rgn = CreateRoundRectRgn(-1, -1, w + 2, h + 2, h + 2, h + 2);
            if rgn != 0 {
                SetWindowRgn(hwnd, rgn, 1); // the system owns rgn from here
            }
        }
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

/// Pointer position in physical screen px.
pub fn cursor_pos() -> Option<(i32, i32)> {
    #[cfg(windows)]
    {
        imp::cursor_pos()
    }
    #[cfg(not(windows))]
    {
        None
    }
}

/// Keep the window out of Alt+Tab / Win+Tab (tool window, not app window).
/// Idempotent and cheap when nothing is wrong: tao rewrites the whole extended
/// style whenever one of its own flags changes (show, click-through, ...), so
/// callers re-apply this after each of those.
pub fn hide_from_switcher(hwnd: isize) {
    #[cfg(windows)]
    imp::hide_from_switcher(hwnd);
    #[cfg(not(windows))]
    let _ = hwnd;
}

/// Put the window back above other topmost windows without activating it.
pub fn raise_topmost(hwnd: isize) {
    #[cfg(windows)]
    imp::raise_topmost(hwnd);
    #[cfg(not(windows))]
    let _ = hwnd;
}

/// Clip the window to a pill shape of the given physical size.
pub fn round_region(hwnd: isize, w: i32, h: i32) {
    #[cfg(windows)]
    imp::round_region(hwnd, w, h);
    #[cfg(not(windows))]
    let _ = (hwnd, w, h);
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

    #[link(name = "user32")]
    extern "system" {
        fn CreateWindowExW(
            ex: u32, class: *const u16, name: *const u16, style: u32, x: i32, y: i32, w: i32, h: i32,
            parent: isize, menu: isize, inst: isize, param: isize,
        ) -> isize;
        fn DestroyWindow(h: isize) -> i32;
    }
    #[link(name = "gdi32")]
    extern "system" {
        fn GetWindowRgnBox(h: isize, r: *mut [i32; 4]) -> i32;
    }

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// A hidden STATIC popup: enough HWND to exercise the style calls.
    fn scratch_window() -> Option<isize> {
        let (class, name) = (wide("STATIC"), wide("statusify-test"));
        let h = unsafe {
            CreateWindowExW(imp::WS_EX_APPWINDOW as u32, class.as_ptr(), name.as_ptr(), 0x8000_0000, 0, 0, 200, 60, 0, 0, 0, 0)
        };
        (h != 0).then_some(h)
    }

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

    #[test]
    fn cursor_position_is_available() {
        // None only without an interactive desktop.
        if let Some((x, y)) = cursor_pos() {
            assert!(x > -100_000 && y > -100_000);
        }
    }

    #[test]
    fn switcher_topmost_and_region_calls_change_the_window() {
        let Some(h) = scratch_window() else { return };
        assert_ne!(imp::ex_style(h) & imp::WS_EX_APPWINDOW, 0);
        hide_from_switcher(h);
        let ex = imp::ex_style(h);
        assert_ne!(ex & imp::WS_EX_TOOLWINDOW, 0, "must be a tool window");
        assert_eq!(ex & imp::WS_EX_APPWINDOW, 0, "must not be an app window");
        raise_topmost(h);
        assert_ne!(imp::ex_style(h) & imp::WS_EX_TOPMOST, 0);
        round_region(h, 200, 60);
        let mut b = [0i32; 4];
        assert!(unsafe { GetWindowRgnBox(h, &mut b) } > 0);
        assert_eq!((b[2] - b[0], b[3] - b[1]), (202, 62));
        unsafe { DestroyWindow(h) };
    }
}
