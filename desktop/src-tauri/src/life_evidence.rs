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
    // v1: AppleScript bridge — fetch names in one call, then let Rust handle JSON escaping.
    let script = r#"
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to (ASCII character 10)
        tell application "Contacts" to set namesList to name of every person
        set outText to namesList as text
        set AppleScript's text item delimiters to oldDelims
        return outText
    "#;
    run_applescript_text(script)
        .map(|s| {
            s.lines()
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .map(|name| serde_json::json!({"display_name": name, "source": "macos_contacts"}))
                .collect()
        })
        .unwrap_or_default()
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

#[cfg(target_os = "macos")]
fn run_applescript_text(script: &str) -> Option<String> {
    use std::process::Command;
    let out = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).to_string())
}
