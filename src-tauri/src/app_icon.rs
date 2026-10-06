//! The Statusify icon where Windows looks for it: the title bar, the taskbar
//! button, Alt+Tab and the tray, plus the AppUserModelID that ties them to the
//! shortcuts the installer makes.
//!
//! Three separate things used to leave the Tauri placeholder showing:
//!  * build.rs never told cargo to watch icons/, so a swapped icon.ico never
//!    reached the exe's resources (fixed there);
//!  * tao only sets the *small* window icon, from one 32 px image Windows then
//!    shrinks, and never the big one the taskbar and Alt+Tab ask for;
//!  * no AppUserModelID, so Windows keyed the taskbar button off the exe path
//!    instead of the identifier the installer's shortcuts carry.
//!
//! Hand-declared FFI, so no extra crate (same approach as win_mon.rs). Off
//! Windows every function does nothing.

use tauri::plugin::{Builder, TauriPlugin};
use tauri::{Runtime, Window, WindowEvent};

#[cfg(windows)]
mod imp {
    use tauri::utils::platform::WINDOWS_APP_ICON_RESOURCE_ID;

    #[link(name = "user32")]
    extern "system" {
        fn LoadImageW(hinst: isize, name: *const u16, kind: u32, cx: i32, cy: i32, flags: u32) -> isize;
        fn SendMessageW(hwnd: isize, msg: u32, wparam: usize, lparam: isize) -> isize;
        fn GetDpiForWindow(hwnd: isize) -> u32;
        fn GetSystemMetrics(index: i32) -> i32;
        fn GetSystemMetricsForDpi(index: i32, dpi: u32) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> isize;
    }
    #[link(name = "shell32")]
    extern "system" {
        fn SetCurrentProcessExplicitAppUserModelID(id: *const u16) -> i32;
    }

    const IMAGE_ICON: u32 = 1;
    /// The system caches shared icons per (resource, size) and frees them itself:
    /// nothing to destroy, and applying again at the same size is free.
    const LR_SHARED: u32 = 0x8000;
    const WM_SETICON: u32 = 0x80;
    const ICON_SMALL: usize = 0;
    const ICON_BIG: usize = 1;
    const SM_CXICON: i32 = 11;
    const SM_CYICON: i32 = 12;
    const SM_CXSMICON: i32 = 49;
    const SM_CYSMICON: i32 = 50;

    /// The exe's own icon resource (the one build.rs embeds) at `cx` x `cy`;
    /// Windows picks the closest frame of the .ico and scales it if needed.
    fn load(cx: i32, cy: i32) -> Option<isize> {
        // SAFETY: plain Win32 calls; the resource id is passed as MAKEINTRESOURCE.
        let icon = unsafe {
            LoadImageW(GetModuleHandleW(std::ptr::null()), WINDOWS_APP_ICON_RESOURCE_ID as usize as *const u16, IMAGE_ICON, cx, cy, LR_SHARED)
        };
        (icon != 0).then_some(icon)
    }

    /// Small (title bar) and big (taskbar, Alt+Tab) icons at the sizes this
    /// window's own DPI calls for. Returns the (small, big) pixel sizes used.
    pub fn set_window_icons(hwnd: isize) -> Option<(i32, i32)> {
        // SAFETY: plain Win32 calls on a window handle Tauri just gave us.
        unsafe {
            let dpi = match GetDpiForWindow(hwnd) {
                0 => 96,
                d => d,
            };
            let small = (GetSystemMetricsForDpi(SM_CXSMICON, dpi), GetSystemMetricsForDpi(SM_CYSMICON, dpi));
            let big = (GetSystemMetricsForDpi(SM_CXICON, dpi), GetSystemMetricsForDpi(SM_CYICON, dpi));
            let (s, b) = (load(small.0, small.1)?, load(big.0, big.1)?);
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL, s);
            SendMessageW(hwnd, WM_SETICON, ICON_BIG, b);
            Some((small.0, big.0))
        }
    }

    /// The notification area's icon size on this system (16 px at 100%).
    pub fn tray_px() -> u32 {
        // SAFETY: plain Win32 call.
        match unsafe { GetSystemMetrics(SM_CXSMICON) } {
            n if n > 0 => n as u32,
            _ => 16,
        }
    }

    pub fn set_app_user_model_id(id: &str) -> Result<(), i32> {
        let wide: Vec<u16> = id.encode_utf16().chain(std::iter::once(0)).collect();
        // SAFETY: `wide` is NUL-terminated and outlives the call.
        match unsafe { SetCurrentProcessExplicitAppUserModelID(wide.as_ptr()) } {
            hr if hr < 0 => Err(hr),
            _ => Ok(()),
        }
    }
}

/// Name this process for the taskbar. Must run before the first window exists.
/// The identifier is the one Tauri's NSIS installer stamps on the Start menu and
/// Desktop shortcuts, so a pinned shortcut and the running window are one button
/// and the button keeps the Statusify icon.
pub fn set_app_user_model_id(id: &str) {
    #[cfg(windows)]
    match imp::set_app_user_model_id(id) {
        Ok(()) => crate::log(&format!("Taskbar identity {id}")),
        Err(hr) => crate::log(&format!("Taskbar identity not set (HRESULT {hr:#010X})")),
    }
    #[cfg(not(windows))]
    let _ = id;
}

/// Give `window` the logo as both its small and big icon.
pub fn apply<R: Runtime>(window: &Window<R>) {
    #[cfg(windows)]
    {
        let Ok(hwnd) = window.hwnd() else { return };
        match imp::set_window_icons(hwnd.0 as isize) {
            Some((small, big)) => crate::log(&format!("Window icon set on {} ({small} px small, {big} px big)", window.label())),
            None => crate::log(&format!("Window icon not set on {}: the exe has no icon resource", window.label())),
        }
    }
    #[cfg(not(windows))]
    let _ = window;
}

/// The logo at the size the tray draws, cut from the exe's own resource so it is
/// sharp rather than a 32 px image shrunk by the shell. None off Windows.
pub fn tray_icon() -> Option<tauri::image::Image<'static>> {
    #[cfg(windows)]
    return tauri::image::Image::from_app_icon_resource(imp::tray_px()).ok();
    #[cfg(not(windows))]
    None
}

/// Every window, now and later (main, mini player, overlay), gets the icon as
/// it is created.
pub fn plugin<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("statusify-icon").on_window_ready(|window| apply(&window)).build()
}

/// A window moved to a monitor with another scale factor needs the sizes for
/// that DPI (register with Builder::on_window_event).
pub fn on_window_event<R: Runtime>(window: &Window<R>, event: &WindowEvent) {
    if matches!(event, WindowEvent::ScaleFactorChanged { .. }) {
        apply(window);
    }
}

#[cfg(test)]
mod tests {
    /// (width, height) of every image in an .ico, 0 meaning 256 as the format says.
    fn ico_sizes(ico: &[u8]) -> Vec<(u32, u32)> {
        assert_eq!(&ico[..4], &[0, 0, 1, 0], "not an .ico");
        let n = u16::from_le_bytes([ico[4], ico[5]]) as usize;
        (0..n)
            .map(|i| {
                let e = &ico[6 + 16 * i..22 + 16 * i];
                let dim = |b: u8| if b == 0 { 256 } else { b as u32 };
                (dim(e[0]), dim(e[1]))
            })
            .collect()
    }

    #[test]
    fn icon_has_every_size_windows_asks_for() {
        let sizes = ico_sizes(include_bytes!("../icons/icon.ico"));
        for want in [16, 24, 32, 48, 64, 256] {
            assert!(sizes.contains(&(want, want)), "icons/icon.ico has no {want} px image: {sizes:?}");
        }
    }

    #[test]
    fn config_and_build_script_use_the_one_icon() {
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let icons = conf["bundle"]["icon"].as_array().unwrap();
        assert!(icons.iter().any(|i| i == "icons/icon.ico"), "bundle.icon must list icons/icon.ico (it becomes the exe resource)");
        let nsis = &conf["bundle"]["windows"]["nsis"];
        assert_eq!(nsis["installerIcon"], "icons/icon.ico");
        assert_eq!(nsis["uninstallerIcon"], "icons/icon.ico");
        // The root cause of the placeholder that outlived the icon swap.
        assert!(include_str!("../build.rs").contains("rerun-if-changed=icons"), "build.rs must watch icons/");
    }
}
