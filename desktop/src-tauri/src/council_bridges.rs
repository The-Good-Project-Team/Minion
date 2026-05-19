//! Native handoffs for council skill execution (compose, calendar, checkout stub).

#[tauri::command]
pub fn council_bridge_open(skill_id: String, payload: serde_json::Value) -> Result<String, String> {
    match skill_id.as_str() {
        "send_message" => open_message_compose(&payload),
        "create_calendar_hold" => open_calendar_compose(&payload),
        "execute_purchase" => Ok("checkout_stub:open_native_checkout_when_integrated".into()),
        _ => Err(format!("unknown skill_id: {skill_id}")),
    }
}

fn pct_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn open_message_compose(payload: &serde_json::Value) -> Result<String, String> {
    let body = payload
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let channel = payload
        .get("channel")
        .and_then(|v| v.as_str())
        .unwrap_or("imessage");
    let encoded = pct_encode(body);
    let url = if channel == "imessage" {
        format!("sms:&body={encoded}")
    } else {
        format!("mailto:?body={encoded}")
    };
    open_url(&url)?;
    Ok(url)
}

fn open_calendar_compose(payload: &serde_json::Value) -> Result<String, String> {
    let title = payload
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or("Follow up");
    let encoded = pct_encode(title);
    let url = format!("calshow:?title={encoded}");
    open_url(&url)?;
    Ok(url)
}

fn open_url(url: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        Command::new("open")
            .arg(url)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = url;
        Err("council bridges only supported on macOS".into())
    }
}
