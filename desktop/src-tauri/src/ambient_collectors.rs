//! Additional ambient collectors: process snapshot, browser hints (macOS).

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::json;

#[cfg(target_os = "macos")]
use active_win_pos_rs::get_active_window;

fn ts_unix_float() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn browser_visit_record(
    app_name: &str,
    title: &str,
    tab_url: Option<&str>,
    tab_host: Option<&str>,
) -> Option<serde_json::Value> {
    if !crate::browser_focus::is_browser_app(app_name) {
        return None;
    }
    let url = tab_url.unwrap_or("").trim();
    let host = tab_host
        .filter(|h| !h.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            if !url.is_empty() {
                crate::browser_focus::host_from_url_or_title(url, title)
            } else {
                crate::browser_focus::page_hint_from_title(title)
            }
        });
    let confidence = if url.starts_with("http") {
        "high"
    } else if host.contains('.') {
        "medium"
    } else if !host.is_empty() {
        "low"
    } else {
        "none"
    };
    if confidence == "none" {
        return None;
    }
    let url_key: String = url.chars().take(48).collect();
    Some(json!({
        "ts": ts_unix_float(),
        "kind": "browser_visit",
        "app_name": app_name,
        "window_title": title,
        "url": url,
        "url_or_host": host,
        "browser": app_name,
        "confidence": confidence,
        "dedupe_key": format!("browser:{app_name}:{host}:{url_key}"),
    }))
}

#[cfg(target_os = "macos")]
pub fn spawn_collectors(data_dir: PathBuf) {
    if crate::ambient_stream::collector_enabled(&data_dir, "process_snapshot") {
        let dd = data_dir.clone();
        thread::spawn(move || process_snapshot_loop(dd));
    }
    if crate::ambient_stream::collector_enabled(&data_dir, "clipboard_event") {
        let dd = data_dir.clone();
        thread::spawn(move || clipboard_loop(dd));
    }
    if crate::ambient_stream::collector_enabled(&data_dir, "mouse_event")
        || crate::ambient_stream::collector_enabled(&data_dir, "keyboard_event")
    {
        let dd = data_dir.clone();
        thread::spawn(move || input_event_loop(dd));
    }
    if crate::ambient_stream::collector_enabled(&data_dir, "rolling_video_clip") {
        let dd = data_dir.clone();
        thread::spawn(move || rolling_video_loop(dd));
    }
}

#[cfg(target_os = "macos")]
#[derive(Default)]
struct InputBucket {
    mouse_clicks: u64,
    key_presses: u64,
    last_click_x: Option<f64>,
    last_click_y: Option<f64>,
}

#[cfg(target_os = "macos")]
fn input_event_loop(data_dir: PathBuf) {
    use core_foundation::runloop::{kCFRunLoopCommonModes, CFRunLoop};
    use core_graphics::event::{
        CGEventTap, CGEventTapLocation, CGEventTapOptions, CGEventTapPlacement, CGEventType,
    };

    let bucket = Arc::new(Mutex::new(InputBucket::default()));
    let flush_bucket = Arc::clone(&bucket);
    let flush_dir = data_dir.clone();
    thread::spawn(move || input_event_flush_loop(flush_dir, flush_bucket));

    let tap_bucket = Arc::clone(&bucket);
    let Ok(tap) = CGEventTap::new(
        CGEventTapLocation::HID,
        CGEventTapPlacement::TailAppendEventTap,
        CGEventTapOptions::ListenOnly,
        vec![
            CGEventType::LeftMouseDown,
            CGEventType::RightMouseDown,
            CGEventType::OtherMouseDown,
            CGEventType::KeyDown,
        ],
        move |_proxy, event_type, event| {
            if let Ok(mut b) = tap_bucket.lock() {
                match event_type {
                    CGEventType::LeftMouseDown
                    | CGEventType::RightMouseDown
                    | CGEventType::OtherMouseDown => {
                        b.mouse_clicks = b.mouse_clicks.saturating_add(1);
                        let p = event.location();
                        b.last_click_x = Some(p.x);
                        b.last_click_y = Some(p.y);
                    }
                    CGEventType::KeyDown => {
                        b.key_presses = b.key_presses.saturating_add(1);
                    }
                    _ => {}
                }
            }
            None
        },
    ) else {
        return;
    };
    let current = CFRunLoop::get_current();
    let Ok(loop_source) = tap.mach_port.create_runloop_source(0) else {
        return;
    };
    current.add_source(&loop_source, unsafe { kCFRunLoopCommonModes });
    tap.enable();
    CFRunLoop::run_current();
}

#[cfg(target_os = "macos")]
fn input_event_flush_loop(data_dir: PathBuf, bucket: Arc<Mutex<InputBucket>>) {
    loop {
        thread::sleep(Duration::from_secs(5));
        let mut snap = InputBucket::default();
        if let Ok(mut b) = bucket.lock() {
            std::mem::swap(&mut *b, &mut snap);
        }
        if snap.mouse_clicks == 0 && snap.key_presses == 0 {
            continue;
        }
        let active = get_active_window().ok();
        let app_name = active.as_ref().map(|w| w.app_name.as_str()).unwrap_or("");
        let title = active.as_ref().map(|w| w.title.as_str()).unwrap_or("");
        if crate::capture_deny::capture_denied(&data_dir, app_name, title) {
            continue;
        }
        let ts = ts_unix_float();
        if snap.mouse_clicks > 0 && crate::ambient_stream::collector_enabled(&data_dir, "mouse_event") {
            let record = json!({
                "ts": ts,
                "kind": "mouse_event",
                "app_name": app_name,
                "window_title": title,
                "click_count": snap.mouse_clicks,
                "last_click": {"x": snap.last_click_x, "y": snap.last_click_y},
                "summary": input_summary("clicked", snap.mouse_clicks, app_name, title),
                "dedupe_key": format!("mouse:{:.0}:{}:{}", ts, app_name, title),
            });
            crate::ambient_stream::append_ambient_record(&data_dir, &record);
        }
        if snap.key_presses > 0 && crate::ambient_stream::collector_enabled(&data_dir, "keyboard_event") {
            let record = json!({
                "ts": ts,
                "kind": "keyboard_event",
                "app_name": app_name,
                "window_title": title,
                "key_press_count": snap.key_presses,
                "summary": input_summary("typed", snap.key_presses, app_name, title),
                "content_captured": false,
                "dedupe_key": format!("keyboard:{:.0}:{}:{}", ts, app_name, title),
            });
            crate::ambient_stream::append_ambient_record(&data_dir, &record);
        }
    }
}

#[cfg(target_os = "macos")]
fn clipboard_loop(data_dir: PathBuf) {
    use std::process::Command;

    let mut last_hash = String::new();
    loop {
        thread::sleep(Duration::from_millis(1800));
        if !crate::ambient_stream::collector_enabled(&data_dir, "clipboard_event") {
            continue;
        }
        let Ok(out) = Command::new("/usr/bin/pbpaste").output() else {
            continue;
        };
        if !out.status.success() || out.stdout.is_empty() {
            continue;
        }
        let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if text.is_empty() || text.len() > 64_000 {
            continue;
        }
        let hash = stable_hash(&text);
        if hash == last_hash {
            continue;
        }
        last_hash = hash.clone();
        let active = get_active_window().ok();
        let app_name = active.as_ref().map(|w| w.app_name.as_str()).unwrap_or("");
        let title = active.as_ref().map(|w| w.title.as_str()).unwrap_or("");
        if crate::capture_deny::capture_denied(&data_dir, app_name, title) {
            continue;
        }
        let excerpt = clipboard_excerpt(&text, 500);
        let record = json!({
            "ts": ts_unix_float(),
            "kind": "clipboard_event",
            "app_name": app_name,
            "window_title": title,
            "clipboard_hash": hash,
            "text_len": text.len(),
            "text_excerpt": excerpt,
            "detected_emails": detect_emails(&text),
            "summary": clipboard_summary(&text),
            "dedupe_key": format!("clipboard:{hash}"),
        });
        crate::ambient_stream::append_ambient_record(&data_dir, &record);
    }
}

#[cfg(target_os = "macos")]
fn process_snapshot_loop(data_dir: PathBuf) {
    use std::process::Command;
    loop {
        thread::sleep(Duration::from_secs(60));
        if !crate::ambient_stream::collector_enabled(&data_dir, "process_snapshot") {
            continue;
        }
        let mut apps: Vec<serde_json::Value> = Vec::new();
        if let Ok(out) = Command::new("/bin/ps")
            .args(["-Arc", "-o", "comm=,pcpu="])
            .output()
        {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                for line in text.lines().take(20) {
                    let line = line.trim();
                    if line.is_empty() {
                        continue;
                    }
                    let mut parts = line.rsplitn(2, ' ');
                    let cpu: f32 = parts
                        .next()
                        .and_then(|s| s.trim().parse().ok())
                        .unwrap_or(0.0);
                    let name = parts.next().unwrap_or(line).trim().to_string();
                    apps.push(json!({"name": name, "cpu": cpu}));
                }
            }
        }
        let foreground_app = get_active_window()
            .ok()
            .map(|w| w.app_name)
            .unwrap_or_default();
        let record = json!({
            "ts": ts_unix_float(),
            "kind": "process_snapshot",
            "foreground_app": foreground_app,
            "apps": apps,
            "dedupe_key": format!("proc:{}", (ts_unix_float() as i64) / 60),
        });
        crate::ambient_stream::append_ambient_record(&data_dir, &record);
    }
}

#[cfg(target_os = "macos")]
fn rolling_video_loop(data_dir: PathBuf) {
    thread::sleep(Duration::from_secs(3));
    loop {
        if !crate::ambient_stream::collector_enabled(&data_dir, "rolling_video_clip") {
            thread::sleep(Duration::from_secs(5));
            continue;
        }
        let duration = rolling_video_duration_sec();
        let interval = rolling_video_interval_sec(duration);
        let started_at = ts_unix_float();
        let active = get_active_window().ok();
        let app_name = active.as_ref().map(|w| w.app_name.as_str()).unwrap_or("");
        let title = active.as_ref().map(|w| w.title.as_str()).unwrap_or("");
        if crate::capture_deny::capture_denied(&data_dir, app_name, title) {
            thread::sleep(Duration::from_secs(interval));
            continue;
        }
        let dir = data_dir.join("ambient").join("video");
        let _ = std::fs::create_dir_all(&dir);
        prune_rolling_video_clips(&dir, rolling_video_max_clips());
        let path = dir.join(rolling_video_filename(started_at));
        let args = rolling_video_args(duration, &path);
        let ok = crate::window_capture::run_screencapture_with_backoff(&data_dir, &args);
        if ok && path.is_file() {
            let record = json!({
                "ts": started_at,
                "kind": "rolling_video_clip",
                "app_name": app_name,
                "window_title": title,
                "clip_path": path.to_string_lossy(),
                "duration_sec": duration,
                "started_at": started_at,
                "ended_at": ts_unix_float(),
                "parser_hint": "marlin",
                "dedupe_key": format!("clip:{}", path.file_name().and_then(|s| s.to_str()).unwrap_or("unknown")),
            });
            crate::ambient_stream::append_ambient_record(&data_dir, &record);
        }
        prune_rolling_video_clips(&dir, rolling_video_max_clips());
        thread::sleep(Duration::from_secs(interval));
    }
}

#[cfg(not(target_os = "macos"))]
pub fn spawn_collectors(_data_dir: PathBuf) {}

fn stable_hash(text: &str) -> String {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

fn rolling_video_duration_sec() -> u64 {
    env_u64("MINION_ROLLING_VIDEO_SECONDS", 10).clamp(10, 30)
}

fn rolling_video_interval_sec(duration: u64) -> u64 {
    env_u64("MINION_ROLLING_VIDEO_INTERVAL_SECONDS", 45).max(duration + 5)
}

fn rolling_video_max_clips() -> usize {
    env_u64("MINION_ROLLING_VIDEO_MAX_CLIPS", 24).clamp(1, 240) as usize
}

fn env_u64(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(default)
}

fn rolling_video_filename(ts: f64) -> String {
    format!("screen-{}.mov", (ts * 1000.0).round() as u64)
}

fn rolling_video_args(duration: u64, path: &Path) -> Vec<String> {
    vec![
        "-x".to_string(),
        "-m".to_string(),
        "-v".to_string(),
        format!("-V{duration}"),
        path.to_string_lossy().to_string(),
    ]
}

fn prune_rolling_video_clips(dir: &Path, keep: usize) {
    let Ok(read) = std::fs::read_dir(dir) else {
        return;
    };
    let mut clips: Vec<_> = read
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("mov"))
        .filter_map(|e| {
            let modified = e.metadata().and_then(|m| m.modified()).ok()?;
            Some((modified, e.path()))
        })
        .collect();
    clips.sort_by_key(|(modified, _)| *modified);
    let overflow = clips.len().saturating_sub(keep);
    for (_, path) in clips.into_iter().take(overflow) {
        let _ = std::fs::remove_file(path);
    }
}

fn clipboard_excerpt(text: &str, max_chars: usize) -> String {
    text.chars()
        .take(max_chars)
        .collect::<String>()
        .replace('\n', " ")
        .trim()
        .to_string()
}

fn clipboard_summary(text: &str) -> String {
    let emails = detect_emails(text);
    if !emails.is_empty() {
        return format!("User copied text containing email {}", emails[0]);
    }
    format!("User copied {} characters to the clipboard.", text.chars().count())
}

fn input_summary(verb: &str, count: u64, app_name: &str, title: &str) -> String {
    let unit = if count == 1 { "time" } else { "times" };
    if !app_name.is_empty() && !title.is_empty() {
        return format!("User {verb} {count} {unit} in {app_name}: {title}.");
    }
    if !app_name.is_empty() {
        return format!("User {verb} {count} {unit} in {app_name}.");
    }
    format!("User {verb} {count} {unit}.")
}

fn detect_emails(text: &str) -> Vec<String> {
    text.split(|c: char| c.is_whitespace() || matches!(c, '<' | '>' | '"' | '\'' | ',' | ';' | '(' | ')'))
        .filter_map(|token| {
            let t = token.trim_matches(|c: char| matches!(c, '.' | ':' | '!' | '?' | '[' | ']'));
            if t.len() > 5 && t.contains('@') && t.rsplit_once('.').is_some() {
                Some(t.to_string())
            } else {
                None
            }
        })
        .take(5)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clipboard_helpers_extract_email_and_excerpt() {
        let text = "Investor lead: alex@example.com\nCopied from Sheets";
        assert_eq!(detect_emails(text), vec!["alex@example.com".to_string()]);
        assert_eq!(
            clipboard_summary(text),
            "User copied text containing email alex@example.com"
        );
        assert_eq!(
            clipboard_excerpt(text, 80),
            "Investor lead: alex@example.com Copied from Sheets"
        );
    }

    #[test]
    fn rolling_video_helpers_build_bounded_command() {
        let path = PathBuf::from("/tmp/minion-screen.mov");
        assert_eq!(
            rolling_video_args(10, &path),
            vec!["-x", "-m", "-v", "-V10", "/tmp/minion-screen.mov"]
        );
        assert_eq!(rolling_video_filename(1234.567), "screen-1234567.mov");
        assert!(rolling_video_interval_sec(30) >= 35);
    }

    #[test]
    fn input_summary_does_not_capture_content() {
        assert_eq!(
            input_summary("typed", 3, "Chrome", "Stripe"),
            "User typed 3 times in Chrome: Stripe."
        );
        assert_eq!(input_summary("clicked", 1, "", ""), "User clicked 1 time.");
    }
}
