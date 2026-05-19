//! macOS Keychain: add generic passwords; search index + optional dump.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Serialize, Deserialize, Clone)]
pub struct KeychainItemMeta {
    pub service: String,
    pub account: String,
    pub label: String,
    pub vault_ref: String,
}

fn index_path(data_dir: &str) -> PathBuf {
    PathBuf::from(data_dir).join("keychain_index.json")
}

fn load_index(data_dir: &str) -> Vec<KeychainItemMeta> {
    let p = index_path(data_dir);
    if !p.is_file() {
        return Vec::new();
    }
    let Ok(raw) = fs::read_to_string(&p) else {
        return Vec::new();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

fn save_index(data_dir: &str, items: &[KeychainItemMeta]) -> Result<(), String> {
    let p = index_path(data_dir);
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(&p, serde_json::to_string_pretty(items).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn keychain_search(data_dir: String, query: Option<String>) -> Result<Vec<KeychainItemMeta>, String> {
    let q = query.unwrap_or_default().trim().to_lowercase();
    let mut items = load_index(&data_dir);
    #[cfg(target_os = "macos")]
    {
        items.extend(macos_dump_generic_passwords()?);
    }
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for it in items {
        if !seen.insert(it.vault_ref.clone()) {
            continue;
        }
        if q.is_empty()
            || it.service.to_lowercase().contains(&q)
            || it.account.to_lowercase().contains(&q)
            || it.label.to_lowercase().contains(&q)
        {
            out.push(it);
        }
    }
    out.sort_by(|a, b| a.label.cmp(&b.label));
    out.truncate(200);
    Ok(out)
}

#[tauri::command]
pub fn keychain_add(
    data_dir: String,
    service: String,
    account: String,
    secret: String,
    label: Option<String>,
) -> Result<KeychainItemMeta, String> {
    #[cfg(target_os = "macos")]
    {
        use security_framework::passwords::set_generic_password;
        set_generic_password(&service, &account, secret.as_bytes()).map_err(|e| e.to_string())?;
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (&service, &account, &secret);
        return Err("Keychain add is macOS-only".into());
    }

    let display = label.unwrap_or_else(|| format!("{service} · {account}"));
    let item = KeychainItemMeta {
        vault_ref: format!("keychain:{service}:{account}"),
        service: service.clone(),
        account: account.clone(),
        label: display,
    };
    let mut items = load_index(&data_dir);
    items.retain(|i| i.vault_ref != item.vault_ref);
    items.push(item.clone());
    save_index(&data_dir, &items)?;
    Ok(item)
}

#[cfg(target_os = "macos")]
fn macos_dump_generic_passwords() -> Result<Vec<KeychainItemMeta>, String> {
    let home = dirs::home_dir().ok_or("no home dir")?;
    let kc = home.join("Library/Keychains/login.keychain-db");
    if !kc.is_file() {
        return Ok(Vec::new());
    }
    let output = std::process::Command::new("/usr/bin/security")
        .args(["dump-keychain", kc.to_str().unwrap_or("")])
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Ok(Vec::new());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut out = Vec::new();
    let mut service = String::new();
    let mut account = String::new();
    for line in text.lines() {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("\"svce\"<blob>=") {
            service = rest.trim_matches('"').to_string();
        } else if let Some(rest) = t.strip_prefix("\"acct\"<blob>=") {
            account = rest.trim_matches('"').to_string();
        } else if (t.starts_with("class:") || t.is_empty()) && !service.is_empty() {
            let vault_ref = format!("keychain:{}:{}", service, account);
            let label = if account.is_empty() {
                service.clone()
            } else {
                format!("{service} · {account}")
            };
            out.push(KeychainItemMeta {
                service: service.clone(),
                account: account.clone(),
                label,
                vault_ref,
            });
            service.clear();
            account.clear();
        }
    }
    Ok(out)
}
