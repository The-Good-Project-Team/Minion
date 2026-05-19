//! Logs focused-window metadata (and optional window screenshots) for MCP tools.
//!
//! Writes newline-delimited JSON to `<MINION_DATA_DIR>/screen_context/stream.jsonl`.
//! Screenshots go to `<inbox>/screen-memory/` so the ingest watcher OCRs them.

use std::path::PathBuf;

pub fn spawn_watcher(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
    if minion_screen_context_disabled() {
        return;
    }
    imp::spawn(app, data_dir, inbox);
}

fn minion_screen_context_disabled() -> bool {
    let Ok(v) = std::env::var("MINION_SCREEN_CONTEXT") else {
        return false;
    };
    matches!(
        v.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off"
    )
}

#[cfg(target_os = "macos")]
mod imp {
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use active_win_pos_rs::get_active_window;
    use serde_json::json;
    use tauri::Emitter;

    fn ts_unix_float() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }

    fn fingerprint(app_name: &str, title: &str, window_id: &str) -> String {
        format!("{app_name}\x1f{title}\x1f{window_id}")
    }

    pub fn spawn(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
        thread::spawn(move || run_loop(app, data_dir, inbox));
    }

    fn screen_capture_enabled() -> bool {
        match std::env::var("MINION_SCREEN_CAPTURE") {
            Ok(s) => {
                let t = s.trim().to_ascii_lowercase();
                t == "1" || t == "true" || t == "yes" || t == "on"
            }
            Err(_) => false,
        }
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

    fn poll_interval() -> Duration {
        let secs = std::env::var("MINION_SCREEN_CONTEXT_POLL_SEC")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(3)
            .clamp(2, 120);
        Duration::from_secs(secs)
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

    fn ax_hash(sample: &Option<String>) -> String {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut h = DefaultHasher::new();
        sample.as_deref().unwrap_or("").hash(&mut h);
        format!("{:016x}", h.finish())
    }

    fn run_loop(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
        let _ = std::fs::create_dir_all(data_dir.join("screen_context"));
        let _ = std::fs::create_dir_all(data_dir.join("ambient"));

        let mut last_fp: Option<String> = None;
        let mut last_app: Option<String> = None;
        let mut last_ax_hash: Option<String> = None;
        let mut tick: u64 = 0;

        loop {
            thread::sleep(poll_interval());
            tick = tick.wrapping_add(1);
            if tick % 12 == 0 {
                let _ = crate::life_evidence::snapshot_life_evidence(
                    data_dir.to_string_lossy().into_owned(),
                );
            }

            let win = match get_active_window() {
                Ok(w) => w,
                Err(_) => continue,
            };

            let mut title = win.title.trim().to_string();
            if crate::capture_deny::capture_denied(&data_dir, &win.app_name.trim(), &title) {
                continue;
            }
            let app_name = win.app_name.trim().to_string();
            let mut tab_url = String::new();
            let mut tab_host = String::new();
            let mut browser_page_text = String::new();
            let is_browser = crate::browser_focus::is_browser_app(&app_name);
            if is_browser {
                if let Some(tab) = crate::browser_focus::active_tab(&app_name) {
                    if title.is_empty() && !tab.title.is_empty() {
                        title = tab.title.clone();
                    }
                    tab_url = tab.url;
                    tab_host = tab.host;
                    browser_page_text = tab.visible_text;
                }
            }
            let window_id = win.window_id.trim().to_string();
            let fp = fingerprint(&app_name, &title, &window_id);
            let focus_changed = last_fp.as_ref() != Some(&fp);

            let ts = ts_unix_float();
            let process_path = win.process_path.to_string_lossy().to_string();

            let pid_i32 = if win.process_id > i32::MAX as u64 {
                -1
            } else {
                win.process_id as i32
            };

            let (ax_max_c, ax_max_d) = if is_browser {
                (
                    ax_max_chars().max(20_000),
                    ax_max_depth().max(22),
                )
            } else {
                (ax_max_chars(), ax_max_depth())
            };

            let ax_only = if ax_capture_enabled() && pid_i32 > 0 {
                crate::ax_sample::focused_window_ax_text(pid_i32, ax_max_c, ax_max_d)
            } else {
                None
            };

            let ax_text_sample = merge_visible_text(ax_only, browser_page_text);
            let axh = ax_hash(&ax_text_sample);
            let ax_changed = last_ax_hash.as_ref() != Some(&axh)
                && ax_text_sample.as_ref().map(|s| s.len()).unwrap_or(0) >= 40;

            if !focus_changed && !ax_changed {
                continue;
            }
            if focus_changed {
                last_fp = Some(fp.clone());
            }
            last_ax_hash = Some(axh.clone());

            if focus_changed || ax_changed {
                crate::capture_trigger::request_capture(&data_dir);
            }

            let mut screenshot_rel: Option<String> = None;
            let text_thin = ax_text_sample.as_ref().map(|s| s.len()).unwrap_or(0) < 200;
            let capture_empty = crate::ambient_stream::capture_on_empty_ax_enabled(&data_dir);
            let want_shot = screen_capture_enabled()
                || (capture_empty && ax_text_sample.as_ref().map(|s| s.len()).unwrap_or(0) < 80)
                || (is_browser && text_thin && focus_changed);
            if (focus_changed || ax_changed)
                && want_shot
                && text_thin
                && crate::ambient_stream::collector_enabled(&data_dir, "screenshot_fallback")
            {
                let png_name = format!("{ts}_{window_id}.png");
                let png_path = inbox.join("screen-memory").join(&png_name);
                if crate::window_capture::try_capture_window_png(&window_id, &png_path) {
                    screenshot_rel = Some(format!("screen-memory/{png_name}"));
                }
            }

            let kind = if focus_changed {
                "window_focus"
            } else {
                "ax_content_changed"
            };

            let mut record = json!({
                "ts": ts,
                "kind": kind,
                "app_name": app_name,
                "window_title": title,
                "process_path": process_path,
                "window_id": window_id,
                "ax_text_sample": ax_text_sample,
                "ax_hash": axh,
                "screenshot_inbox_rel": screenshot_rel,
            });
            if !tab_url.is_empty() {
                record["url"] = json!(tab_url);
                record["url_or_host"] = json!(tab_host);
            }

            if crate::ambient_stream::collector_enabled(&data_dir, kind) {
                crate::ambient_stream::append_ambient_record(&data_dir, &record);
            }

            if focus_changed
                && last_app.as_deref() != Some(app_name.as_str())
                && crate::ambient_stream::collector_enabled(&data_dir, "app_launched")
            {
                let launch = json!({
                    "ts": ts,
                    "kind": "app_launched",
                    "app_name": app_name,
                    "bundle_id": process_path,
                    "dedupe_key": format!("launch:{app_name}:{ts:.0}"),
                });
                crate::ambient_stream::append_ambient_record(&data_dir, &launch);
                last_app = Some(app_name.clone());
            }

            if let Some(browser_rec) = crate::ambient_collectors::browser_visit_record(
                &app_name,
                &title,
                if tab_url.is_empty() { None } else { Some(tab_url.as_str()) },
                if tab_host.is_empty() { None } else { Some(tab_host.as_str()) },
            ) {
                if crate::ambient_stream::collector_enabled(&data_dir, "browser_visit") {
                    crate::ambient_stream::append_ambient_record(&data_dir, &browser_rec);
                }
            }

            if screenshot_rel.is_some()
                && crate::ambient_stream::collector_enabled(&data_dir, "screenshot_fallback")
            {
                let shot = json!({
                    "ts": ts,
                    "kind": "screenshot_fallback",
                    "screenshot_inbox_rel": screenshot_rel,
                    "app_name": app_name,
                    "window_title": title,
                });
                crate::ambient_stream::append_ambient_record(&data_dir, &shot);
            }

            let _ = app.emit(
                "screen-context://update",
                json!({
                    "app_name": record["app_name"],
                    "window_title": record["window_title"],
                    "screenshot": screenshot_rel,
                }),
            );
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    use std::path::PathBuf;

    pub fn spawn(
        _app: tauri::AppHandle,
        _data_dir: PathBuf,
        _inbox: PathBuf,
    ) {
    }
}
