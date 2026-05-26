//! Continuous all-visible-window text sampler (macOS).
//!
//! Complements `screen_context` (foreground focus + screenshots). Emits per-window
//! `window_snapshot` lines to `<data_dir>/ambient/stream.jsonl`.

use std::path::PathBuf;

pub fn spawn_watcher(data_dir: PathBuf, inbox: PathBuf) {
    if minion_screen_reader_disabled() {
        return;
    }
    imp::spawn(data_dir, inbox);
}

fn minion_screen_reader_disabled() -> bool {
    let Ok(v) = std::env::var("MINION_SCREEN_READER") else {
        return false;
    };
    matches!(
        v.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off"
    )
}

#[cfg(target_os = "macos")]
mod imp {
    use std::collections::HashMap;
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use active_win_pos_rs::get_active_window;
    use core_foundation::base::{TCFType, TCFTypeRef};
    use core_foundation::propertylist::create_data;
    use core_foundation::propertylist::kCFPropertyListXMLFormat_v1_0;
    use core_graphics::window::{
        copy_window_info, kCGNullWindowID, kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    };
    use serde_json::json;

    const SKIP_OWNERS: &[&str] = &[
        "Window Server",
        "Dock",
        "Control Center",
        "Notification Center",
        "SystemUIServer",
        "Wallpaper",
    ];

    #[derive(Clone, Debug)]
    struct VisibleWindow {
        app_name: String,
        title: String,
        window_id: String,
        pid: i32,
        bounds: WindowBounds,
    }

    #[derive(Clone, Debug, Default)]
    struct WindowBounds {
        x: f64,
        y: f64,
        w: f64,
        h: f64,
    }

    fn ts_unix_float() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }

    fn poll_interval() -> Duration {
        let secs = std::env::var("MINION_SCREEN_READER_INTERVAL_SEC")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(5)
            .clamp(3, 120);
        Duration::from_secs(secs)
    }

    fn max_windows_per_tick() -> usize {
        std::env::var("MINION_SCREEN_READER_MAX_WINDOWS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(12)
            .clamp(1, 32)
    }

    fn min_window_dim() -> f64 {
        80.0
    }

    fn ax_max_chars() -> usize {
        std::env::var("MINION_AX_TEXT_MAX_CHARS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(12_000usize)
            .clamp(500, 100_000)
    }

    fn ax_max_depth() -> usize {
        std::env::var("MINION_AX_MAX_DEPTH")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(14usize)
            .clamp(4, 64)
    }

    fn ax_capture_enabled() -> bool {
        match std::env::var("MINION_AX_CAPTURE") {
            Ok(s) => {
                let t = s.trim().to_ascii_lowercase();
                !(t.is_empty() || t == "0" || t == "false" || t == "no" || t == "off")
            }
            Err(_) => true,
        }
    }

    fn ax_hash(sample: &Option<String>) -> String {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut h = DefaultHasher::new();
        sample.as_deref().unwrap_or("").hash(&mut h);
        format!("{:016x}", h.finish())
    }

    fn merge_visible_text(ax: Option<String>, browser_js: String) -> Option<String> {
        let ax_s = ax.unwrap_or_default();
        let js = browser_js.trim().to_string();
        if ax_s.is_empty() && js.is_empty() {
            return None;
        }
        if ax_s.is_empty() {
            return Some(js);
        }
        if js.is_empty() {
            return Some(ax_s);
        }
        if js.len() > ax_s.len() * 2 {
            Some(format!("{js}\n\n--- accessibility ---\n\n{ax_s}"))
        } else {
            Some(format!("{ax_s}\n\n--- page ---\n\n{js}"))
        }
    }

    fn plist_f64(v: &plist::Value) -> f64 {
        match v {
            plist::Value::Real(f) => *f,
            plist::Value::Integer(i) => i.as_signed().unwrap_or(0) as f64,
            _ => 0.0,
        }
    }

    fn plist_i64(v: &plist::Value) -> i64 {
        match v {
            plist::Value::Integer(i) => i.as_signed().unwrap_or(0),
            plist::Value::Real(f) => *f as i64,
            _ => 0,
        }
    }

    fn parse_bounds_obj(v: &plist::Value) -> WindowBounds {
        let Some(dict) = v.as_dictionary() else {
            return WindowBounds::default();
        };
        WindowBounds {
            x: dict.get("X").map(plist_f64).unwrap_or(0.0),
            y: dict.get("Y").map(plist_f64).unwrap_or(0.0),
            w: dict.get("Width").map(plist_f64).unwrap_or(0.0),
            h: dict.get("Height").map(plist_f64).unwrap_or(0.0),
        }
    }

    fn enumerate_visible_windows() -> Vec<VisibleWindow> {
        let option = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements;
        let Some(info) = copy_window_info(option, kCGNullWindowID) else {
            return Vec::new();
        };
        let Ok(data) = create_data(
            info.as_CFTypeRef().as_void_ptr(),
            kCFPropertyListXMLFormat_v1_0,
        ) else {
            return Vec::new();
        };
        let rows: Vec<plist::Value> = match plist::from_bytes(data.bytes()) {
            Ok(plist::Value::Array(a)) => a,
            _ => return Vec::new(),
        };
        let mut out = Vec::new();
        for row in rows {
            let Some(dict) = row.as_dictionary() else {
                continue;
            };
            let layer = dict
                .get("kCGWindowLayer")
                .map(plist_i64)
                .unwrap_or(0);
            if layer != 0 {
                continue;
            }
            let owner = dict
                .get("kCGWindowOwnerName")
                .and_then(|v| v.as_string())
                .unwrap_or("")
                .to_string();
            if owner.is_empty() || SKIP_OWNERS.iter().any(|s| *s == owner) {
                continue;
            }
            let bounds = dict
                .get("kCGWindowBounds")
                .map(parse_bounds_obj)
                .unwrap_or_default();
            if bounds.w < min_window_dim() || bounds.h < min_window_dim() {
                continue;
            }
            let title = dict
                .get("kCGWindowName")
                .and_then(|v| v.as_string())
                .unwrap_or("")
                .to_string();
            let window_id = dict
                .get("kCGWindowNumber")
                .map(plist_i64)
                .unwrap_or(0)
                .to_string();
            if window_id == "0" {
                continue;
            }
            let pid = dict
                .get("kCGWindowOwnerPID")
                .map(plist_i64)
                .unwrap_or(0) as i32;
            if pid <= 0 {
                continue;
            }
            out.push(VisibleWindow {
                app_name: owner,
                title,
                window_id,
                pid,
                bounds,
            });
        }
        out.sort_by(|a, b| {
            (b.bounds.w * b.bounds.h)
                .partial_cmp(&(a.bounds.w * a.bounds.h))
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        out
    }

    pub fn spawn(data_dir: PathBuf, inbox: PathBuf) {
        thread::spawn(move || run_loop(data_dir, inbox));
    }

    fn run_loop(data_dir: PathBuf, inbox: PathBuf) {
        let _ = std::fs::create_dir_all(data_dir.join("ambient"));
        let mut last_hash: HashMap<String, String> = HashMap::new();

        loop {
            let urgent = crate::capture_trigger::capture_requested_recently(
                &data_dir,
                Duration::from_secs(2),
            );
            if !urgent {
                thread::sleep(poll_interval());
            }
            if !crate::ambient_stream::collector_enabled(&data_dir, "screen_reader") {
                continue;
            }
            capture_tick(&data_dir, &inbox, &mut last_hash);
        }
    }

    fn capture_tick(
        data_dir: &PathBuf,
        inbox: &PathBuf,
        last_hash: &mut HashMap<String, String>,
    ) {
            let (foreground_id, foreground_app) = match get_active_window() {
                Ok(w) => (
                    w.window_id.trim().to_string(),
                    w.app_name.trim().to_string(),
                ),
                Err(_) => (String::new(), String::new()),
            };

            let windows = enumerate_visible_windows();
            let cap = max_windows_per_tick();
            let ts = ts_unix_float();

            let mut browser_page_text = String::new();
            let mut tab_url = String::new();
            let mut tab_host = String::new();
            if !foreground_app.is_empty() && crate::browser_focus::is_browser_app(&foreground_app) {
                if let Some(tab) = crate::browser_focus::active_tab(&foreground_app) {
                    browser_page_text = tab.visible_text;
                    tab_url = tab.url;
                    tab_host = tab.host;
                }
            }

            let capture_empty_ax = crate::ambient_stream::capture_on_empty_ax_enabled(data_dir);
            let shot_enabled =
                crate::ambient_stream::collector_enabled(data_dir, "screenshot_fallback");

            for win in windows.into_iter().take(cap) {
                if crate::capture_deny::capture_denied(data_dir, &win.app_name, &win.title) {
                    continue;
                }
                let is_foreground = !foreground_id.is_empty() && win.window_id == foreground_id;

                let mut title = win.title.clone();
                if is_foreground && title.is_empty() {
                    if let Ok(active) = get_active_window() {
                        title = active.title.trim().to_string();
                    }
                }

                let is_browser = crate::browser_focus::is_browser_app(&win.app_name);
                let (ax_max_c, ax_max_d) = if is_browser && is_foreground {
                    (ax_max_chars().max(20_000), ax_max_depth().max(22))
                } else {
                    (ax_max_chars(), ax_max_depth())
                };

                let ax_only = if ax_capture_enabled() && win.pid > 0 {
                    crate::ax_sample::window_ax_text(
                        win.pid,
                        &win.window_id,
                        &title,
                        ax_max_c,
                        ax_max_d,
                    )
                } else {
                    None
                };

                let browser_js = if is_foreground && is_browser {
                    browser_page_text.clone()
                } else {
                    String::new()
                };

                let ax_text_sample = merge_visible_text(ax_only, browser_js);
                let ax_nodes = if ax_capture_enabled() && win.pid > 0 {
                    crate::ax_sample::collect_ax_nodes(win.pid, &title, 40, 12)
                } else {
                    Vec::new()
                };
                let axh = ax_hash(&ax_text_sample);
                let dedupe_key = format!("ws:{}:{}", win.window_id, axh);
                if last_hash.get(&win.window_id) == Some(&axh) {
                    continue;
                }
                last_hash.insert(win.window_id.clone(), axh.clone());

                let text_len = ax_text_sample.as_ref().map(|s| s.len()).unwrap_or(0);
                let mut screenshot_rel: Option<String> = None;
                if shot_enabled && capture_empty_ax && text_len < 80 {
                    let png_name = format!("wsnap_{ts:.0}_{}.png", win.window_id);
                    let png_path = inbox.join("screen-memory").join(&png_name);
                    if crate::window_capture::try_capture_window_png(
                        data_dir,
                        &win.window_id,
                        &png_path,
                    ) {
                        screenshot_rel = Some(format!("screen-memory/{png_name}"));
                    }
                }

                let mut record = json!({
                    "ts": ts,
                    "kind": "window_snapshot",
                    "app_name": win.app_name,
                    "window_title": title,
                    "window_id": win.window_id,
                    "process_id": win.pid,
                    "bounds": {
                        "x": win.bounds.x,
                        "y": win.bounds.y,
                        "w": win.bounds.w,
                        "h": win.bounds.h,
                    },
                    "is_foreground": is_foreground,
                    "ax_text_sample": ax_text_sample,
                    "ax_hash": axh,
                    "dedupe_key": dedupe_key,
                    "ax_nodes": ax_nodes,
                    "screenshot_inbox_rel": screenshot_rel,
                });
                if is_foreground && !tab_url.is_empty() {
                    record["url"] = json!(tab_url);
                    record["url_or_host"] = json!(tab_host);
                }

                crate::ambient_stream::append_ambient_record(data_dir, &record);

                if screenshot_rel.is_some() {
                    let shot = json!({
                        "ts": ts,
                        "kind": "screenshot_fallback",
                        "screenshot_inbox_rel": screenshot_rel,
                        "app_name": win.app_name,
                        "window_title": title,
                        "window_id": win.window_id,
                    });
                    crate::ambient_stream::append_ambient_record(data_dir, &shot);
                }
            }
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    use std::path::PathBuf;

    pub fn spawn(_data_dir: PathBuf, _inbox: PathBuf) {}
}
