fn main() {
    // tauri_build::build() compiles icons/icon.ico into the exe's resources (the
    // taskbar, title-bar, tray and Explorer icon all come from there) but never
    // tells cargo to watch it. Swapping the icon therefore left the old one baked
    // in: the Tauri placeholder kept shipping after the Statusify logo was
    // committed. Watch the folder so an icon change always re-embeds.
    println!("cargo:rerun-if-changed=icons");
    tauri_build::build()
}
