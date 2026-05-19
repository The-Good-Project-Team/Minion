//! Vault-local deny lists for sensitive windows (settings.json `ambient_deny`).

use std::path::Path;

const DEFAULT_DENY_APPS: &[&str] = &["1Password", "Keychain Access"];
const DEFAULT_DENY_TITLE_SUBSTRINGS: &[&str] = &["password"];

fn read_deny(data_dir: &Path) -> (Vec<String>, Vec<String>) {
    let path = data_dir.join("settings.json");
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return (default_apps(), default_subs());
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return (default_apps(), default_subs());
    };
    let deny = v.get("ambient_deny").and_then(|d| d.as_object());
    let Some(deny) = deny else {
        return (default_apps(), default_subs());
    };
    let apps: Vec<String> = deny
        .get("app_names")
        .and_then(|a| a.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_else(default_apps);
    let subs: Vec<String> = deny
        .get("title_substrings")
        .and_then(|a| a.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_else(default_subs);
    let apps = if apps.is_empty() {
        default_apps()
    } else {
        apps
    };
    let subs = if subs.is_empty() {
        default_subs()
    } else {
        subs
    };
    (apps, subs)
}

fn default_apps() -> Vec<String> {
    DEFAULT_DENY_APPS.iter().map(|s| s.to_string()).collect()
}

fn default_subs() -> Vec<String> {
    DEFAULT_DENY_TITLE_SUBSTRINGS
        .iter()
        .map(|s| s.to_string())
        .collect()
}

pub fn capture_denied(data_dir: &Path, app_name: &str, title: &str) -> bool {
    let (apps, subs) = read_deny(data_dir);
    let app_l = app_name.trim().to_ascii_lowercase();
    let title_l = title.trim().to_ascii_lowercase();
    for a in &apps {
        if app_l == a.trim().to_ascii_lowercase() {
            return true;
        }
    }
    for sub in &subs {
        let sub_l = sub.trim().to_ascii_lowercase();
        if !sub_l.is_empty() && title_l.contains(&sub_l) {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_deny_password_in_title() {
        let dir = std::env::temp_dir().join(format!("minion-deny-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        assert!(capture_denied(&dir, "Safari", "Enter password"));
        assert!(!capture_denied(&dir, "Safari", "GitHub"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
