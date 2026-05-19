//! Additional ambient collectors: process snapshot, browser hints (macOS).

use std::path::PathBuf;
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

pub fn spawn_collectors(data_dir: PathBuf) {
    #[cfg(target_os = "macos")]
    {
        if crate::ambient_stream::collector_enabled(&data_dir, "process_snapshot") {
            let dd = data_dir.clone();
            thread::spawn(move || process_snapshot_loop(dd));
        }
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

#[cfg(not(target_os = "macos"))]
pub fn spawn_collectors(_data_dir: PathBuf) {}
