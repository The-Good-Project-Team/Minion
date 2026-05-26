//! Per-window PNG capture via macOS `screencapture` (Screen Recording entitlement).

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

const SCREEN_CAPTURE_BACKOFF_SECS: u64 = 60 * 60;

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn backoff_path(data_dir: &Path) -> PathBuf {
    data_dir.join(".screen_recording_denied_until")
}

pub fn screen_capture_backoff_active(data_dir: &Path) -> bool {
    let path = backoff_path(data_dir);
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return false;
    };
    let Ok(until) = raw.trim().parse::<u64>() else {
        let _ = std::fs::remove_file(path);
        return false;
    };
    if until > now_secs() {
        return true;
    }
    let _ = std::fs::remove_file(path);
    false
}

pub fn record_screen_capture_success(data_dir: &Path) {
    let _ = std::fs::remove_file(backoff_path(data_dir));
}

pub fn record_screen_capture_failure(data_dir: &Path) {
    let _ = std::fs::create_dir_all(data_dir);
    let until = now_secs().saturating_add(SCREEN_CAPTURE_BACKOFF_SECS);
    let _ = std::fs::write(backoff_path(data_dir), until.to_string());
}

pub fn run_screencapture_with_backoff(data_dir: &Path, args: &[String]) -> bool {
    if screen_capture_backoff_active(data_dir) {
        return false;
    }
    let ok = Command::new("/usr/sbin/screencapture")
        .args(args)
        .status()
        .map(|st| st.success())
        .unwrap_or(false);
    if ok {
        record_screen_capture_success(data_dir);
    } else {
        record_screen_capture_failure(data_dir);
    }
    ok
}

pub fn try_capture_window_png(data_dir: &Path, window_id: &str, out_path: &Path) -> bool {
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
    let args = vec![
        "-x".to_string(),
        "-t".to_string(),
        "png".to_string(),
        "-l".to_string(),
        wid.to_string(),
        out_str.to_string(),
    ];
    run_screencapture_with_backoff(data_dir, &args)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir() -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!("minion-window-capture-test-{stamp}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn screen_capture_failure_sets_and_success_clears_backoff() {
        let dir = temp_dir();
        assert!(!screen_capture_backoff_active(&dir));
        record_screen_capture_failure(&dir);
        assert!(screen_capture_backoff_active(&dir));
        record_screen_capture_success(&dir);
        assert!(!screen_capture_backoff_active(&dir));
        let _ = std::fs::remove_dir_all(dir);
    }
}
