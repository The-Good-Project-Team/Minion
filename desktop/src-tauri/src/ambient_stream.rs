//! Shared append-only writer for `<data_dir>/ambient/stream.jsonl`.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::Value;

pub fn ambient_stream_path(data_dir: &Path) -> PathBuf {
    data_dir.join("ambient").join("stream.jsonl")
}

pub fn legacy_stream_path(data_dir: &Path) -> PathBuf {
    data_dir.join("screen_context").join("stream.jsonl")
}

pub fn append_ambient_record(data_dir: &Path, record: &Value) {
    let ambient_dir = data_dir.join("ambient");
    let _ = std::fs::create_dir_all(&ambient_dir);
    write_line(&ambient_stream_path(data_dir), record);
}

#[allow(dead_code)]
pub fn legacy_stream_path_pub(data_dir: &Path) -> PathBuf {
    legacy_stream_path(data_dir)
}

fn write_line(path: &Path, record: &Value) {
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let line = format!("{record}\n");
        let _ = f.write_all(line.as_bytes());
        let _ = f.sync_all();
    }
}

pub fn collector_enabled(data_dir: &Path, key: &str) -> bool {
    if matches!(key, "listening" | "full_listening") && !voice_enabled_by_env() {
        return false;
    }
    let path = data_dir.join("settings.json");
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return collector_default_enabled(key);
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return collector_default_enabled(key);
    };
    if v.get("ambient_sensing_enabled").and_then(|x| x.as_bool()) == Some(false) {
        return false;
    }
    v.get("ambient_collectors")
        .and_then(|c| c.get(key))
        .and_then(|x| x.as_bool())
        .unwrap_or_else(|| collector_default_enabled(key))
}

fn collector_default_enabled(key: &str) -> bool {
    !matches!(
        key,
        "process_snapshot"
            | "listening"
            | "full_listening"
            | "rolling_video_clip"
            | "screenshot_fallback"
    )
}

pub fn voice_enabled_by_env() -> bool {
    std::env::var("MINION_ENABLE_VOICE")
        .map(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "on" | "yes"
            )
        })
        .unwrap_or(false)
}

pub fn ambient_sensing_enabled(data_dir: &Path) -> bool {
    collector_enabled(data_dir, "window_focus")
        || collector_enabled(data_dir, "ax_content_changed")
}

fn load_settings_value(data_dir: &Path) -> Option<serde_json::Value> {
    let path = data_dir.join("settings.json");
    let raw = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn capture_on_empty_ax_enabled(data_dir: &Path) -> bool {
    if let Ok(v) = std::env::var("MINION_SCREEN_CAPTURE_ON_EMPTY_AX") {
        let t = v.trim().to_ascii_lowercase();
        if t == "1" || t == "true" || t == "yes" || t == "on" {
            return true;
        }
        if t == "0" || t == "false" || t == "no" || t == "off" {
            return false;
        }
    }
    if let Ok(v) = std::env::var("MINION_SCREEN_CAPTURE") {
        let t = v.trim().to_ascii_lowercase();
        if t == "1" || t == "true" || t == "yes" || t == "on" {
            return true;
        }
    }
    let Some(v) = load_settings_value(data_dir) else {
        return false;
    };
    if let Some(b) = v.get("capture_on_empty_ax").and_then(|x| x.as_bool()) {
        return b;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_settings_dir() -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!("minion-ambient-stream-test-{stamp}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn voice_collectors_require_env_opt_in() {
        let dir = temp_settings_dir();
        std::fs::write(
            dir.join("settings.json"),
            r#"{"ambient_collectors":{"listening":true,"full_listening":true}}"#,
        )
        .unwrap();
        std::env::remove_var("MINION_ENABLE_VOICE");
        assert!(!collector_enabled(&dir, "listening"));
        assert!(!collector_enabled(&dir, "full_listening"));

        std::env::set_var("MINION_ENABLE_VOICE", "1");
        assert!(collector_enabled(&dir, "listening"));
        assert!(collector_enabled(&dir, "full_listening"));
        std::env::remove_var("MINION_ENABLE_VOICE");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn pixel_capture_collectors_default_off() {
        let dir = temp_settings_dir();
        assert!(!collector_enabled(&dir, "rolling_video_clip"));
        assert!(!collector_enabled(&dir, "screenshot_fallback"));
        assert!(!capture_on_empty_ax_enabled(&dir));
        let _ = std::fs::remove_dir_all(dir);
    }
}
