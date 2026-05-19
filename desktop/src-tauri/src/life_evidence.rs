//! Calendar + Contacts snapshots for council evidence (macOS EventKit / Contacts).

use std::fs;
use std::path::PathBuf;

#[tauri::command]
pub fn snapshot_life_evidence(data_dir: String) -> Result<serde_json::Value, String> {
    let root = PathBuf::from(&data_dir).join("life_evidence");
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;

    #[cfg(target_os = "macos")]
    {
        let contacts = macos_contacts_snapshot();
        let calendar = macos_calendar_snapshot();
        fs::write(
            root.join("contacts_latest.json"),
            serde_json::to_string_pretty(&contacts).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        fs::write(
            root.join("calendar_latest.json"),
            serde_json::to_string_pretty(&calendar).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        return Ok(serde_json::json!({
            "contacts": contacts.len(),
            "events": calendar.get("events").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
        }));
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = root;
        return Ok(serde_json::json!({"contacts": 0, "events": 0, "skipped": "non-macos"}));
    }
}

#[cfg(target_os = "macos")]
fn macos_contacts_snapshot() -> Vec<serde_json::Value> {
    // v1: AppleScript bridge — avoids extra Rust deps; replace with Contacts framework later.
    let script = r#"
        set out to "["
        set sep to ""
        tell application "Contacts"
            repeat with p in people
                set nm to ""
                try
                    set nm to name of p
                end try
                if nm is not "" then
                    set out to out & sep & "{\"display_name\":\"" & nm & "\"}"
                    set sep to ","
                end if
            end repeat
        end tell
        return out & "]"
    "#;
    run_applescript_json_array(script).unwrap_or_default()
}

#[cfg(target_os = "macos")]
fn macos_calendar_snapshot() -> serde_json::Value {
    let script = r#"
        set out to "["
        set sep to ""
        tell application "Calendar"
            repeat with cal in calendars
                repeat with ev in (events of cal whose start date > (current date))
                    set out to out & sep & "{\"title\":\"" & (summary of ev) & "\"}"
                    set sep to ","
                end repeat
            end repeat
        end tell
        return out & "]"
    "#;
    let events = run_applescript_json_array(script).unwrap_or_default();
    serde_json::json!({ "events": events })
}

#[cfg(target_os = "macos")]
fn run_applescript_json_array(script: &str) -> Option<Vec<serde_json::Value>> {
    use std::process::Command;
    let out = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str(s.trim()).ok()
}
