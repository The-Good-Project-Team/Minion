//! Event-driven wake for `screen_reader` on focus / AX changes.

use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn request_path(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("ambient").join("capture_request")
}

pub fn request_capture(data_dir: &Path) {
    let dir = data_dir.join("ambient");
    let _ = std::fs::create_dir_all(&dir);
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let _ = std::fs::write(request_path(data_dir), format!("{ts:.6}"));
}

pub fn capture_requested_recently(data_dir: &Path, within: Duration) -> bool {
    let path = request_path(data_dir);
    let Ok(meta) = std::fs::metadata(&path) else {
        return false;
    };
    let Ok(modified) = meta.modified() else {
        return false;
    };
    let Ok(age) = SystemTime::now().duration_since(modified) else {
        return true;
    };
    age <= within
}
