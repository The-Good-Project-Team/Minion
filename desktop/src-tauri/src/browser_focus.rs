//! Active browser tab URL, title, and visible page text (macOS).

use std::process::Command;

#[derive(Debug, Clone, Default)]
pub struct BrowserTabInfo {
    pub url: String,
    pub title: String,
    pub host: String,
    /// Readable page text (AX supplement via AppleScript JS when permitted).
    pub visible_text: String,
}

pub fn is_browser_app(name: &str) -> bool {
    let n = name.to_lowercase();
    n.contains("chrome")
        || n.contains("safari")
        || n.contains("firefox")
        || n.contains("arc")
        || n.contains("brave")
        || n.contains("edge")
        || n.contains("chromium")
}

/// Best-effort active tab metadata while this browser is frontmost.
#[cfg(target_os = "macos")]
pub fn active_tab(app_name: &str) -> Option<BrowserTabInfo> {
    let script = applescript_for(app_name)?;
    let out = Command::new("/usr/bin/osascript")
        .arg("-e")
        .arg(&script)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    parse_osascript_tab(&text)
}

#[cfg(not(target_os = "macos"))]
pub fn active_tab(_app_name: &str) -> Option<BrowserTabInfo> {
    None
}

#[cfg(target_os = "macos")]
fn browser_apple_name(app_name: &str) -> Option<&'static str> {
    let n = app_name.to_lowercase();
    if n.contains("arc") {
        return Some("Arc");
    }
    if n.contains("brave") {
        return Some("Brave Browser");
    }
    if n.contains("edge") {
        return Some("Microsoft Edge");
    }
    if n.contains("chrome") || n.contains("chromium") {
        return Some("Google Chrome");
    }
    if n.contains("safari") {
        return Some("Safari");
    }
    if n.contains("firefox") {
        return Some("Firefox");
    }
    None
}

#[cfg(target_os = "macos")]
fn applescript_for(app_name: &str) -> Option<String> {
    let apple_name = browser_apple_name(app_name)?;
    let js_snippet = r#"(function(){try{var b=document.body;if(!b)return '';return (b.innerText||b.textContent||'').slice(0,24000);}catch(e){return ''}})()"#;
    if apple_name == "Safari" {
        return Some(format!(
            r#"tell application "Safari"
    if (count of windows) = 0 then return ""
    set t to current tab of front window
    set pageUrl to URL of t as text
    set pageTitle to name of t as text
    set pageText to ""
    try
        set pageText to do JavaScript "{js_snippet}" in t
    end try
    return pageUrl & linefeed & pageTitle & linefeed & pageText
end tell"#
        ));
    }
    if apple_name == "Firefox" {
        return Some(
            r#"tell application "Firefox"
    if (count of windows) = 0 then return ""
    set t to active tab of front window
    return (URL of t as text) & linefeed & (name of t as text) & linefeed
end tell"#
                .to_string(),
        );
    }
    Some(format!(
        r#"tell application "{apple_name}"
    if (count of windows) = 0 then return ""
    set pageUrl to URL of active tab of front window as text
    set pageTitle to title of active tab of front window as text
    set pageText to ""
    try
        set pageText to execute javascript "{js_snippet}" in active tab of front window
    end try
    return pageUrl & linefeed & pageTitle & linefeed & pageText
end tell"#
    ))
}

#[cfg(target_os = "macos")]
fn parse_osascript_tab(raw: &str) -> Option<BrowserTabInfo> {
    let lines: Vec<&str> = raw.lines().collect();
    if lines.is_empty() {
        return None;
    }
    let url = lines.first().copied().unwrap_or("").trim().to_string();
    let title = lines.get(1).copied().unwrap_or("").trim().to_string();
    let visible_text: String = if lines.len() > 2 {
        lines[2..].join("\n").trim().to_string()
    } else {
        String::new()
    };
    if url.is_empty() && title.is_empty() && visible_text.is_empty() {
        return None;
    }
    let host = host_from_url_or_title(&url, &title);
    let visible_text = visible_text.chars().take(24_000).collect();
    Some(BrowserTabInfo {
        url,
        title,
        host,
        visible_text,
    })
}

pub fn host_from_url_or_title(url: &str, title: &str) -> String {
    let u = url.trim();
    if u.starts_with("http://") || u.starts_with("https://") {
        if let Some(rest) = u.split("://").nth(1) {
            let host = rest.split('/').next().unwrap_or(rest);
            if !host.is_empty() {
                return host.to_string();
            }
        }
    }
    page_hint_from_title(title)
}

pub fn page_hint_from_title(title: &str) -> String {
    let t = title.trim();
    if t.is_empty() {
        return String::new();
    }
    const SUFFIXES: &[&str] = &[
        " - Google Chrome",
        " - Chrome",
        " - Safari",
        " - Firefox",
        " - Arc",
        " - Brave Browser",
        " - Microsoft Edge",
    ];
    for suf in SUFFIXES {
        if let Some(page) = t.strip_suffix(suf) {
            return page.trim().to_string();
        }
    }
    if t.contains(" - ") {
        return t.split(" - ").next().unwrap_or(t).trim().to_string();
    }
    t.chars().take(120).collect()
}
