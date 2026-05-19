//! Per-window PNG capture via macOS `screencapture` (Screen Recording entitlement).

use std::path::Path;
use std::process::Command;

pub fn try_capture_window_png(window_id: &str, out_path: &Path) -> bool {
    let Ok(wid) = window_id.parse::<u32>() else {
        return false;
    };
    if wid == 0 {
        return false;
    }
    let Some(out_str) = out_path.to_str() else {
        return false;
    };
    if let Some(parent) = out_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match Command::new("/usr/sbin/screencapture")
        .args(["-x", "-t", "png", "-l", &wid.to_string(), out_str])
        .status()
    {
        Ok(st) => st.success(),
        Err(_) => false,
    }
}
