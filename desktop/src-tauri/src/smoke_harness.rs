//! Loopback-only production smoke harness for the installed Minion app.
//!
//! Binds 127.0.0.1 on an ephemeral port, writes `<data_dir>/smoke-harness.json`
//! for CLI discovery, and requires the same `.minion_api_token` as the sidecar.

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::AppHandle;
use tauri::Manager;

use crate::AppState;

const MAX_BODY: usize = 64 * 1024;

static RUNNING: AtomicBool = AtomicBool::new(false);

#[derive(Clone)]
struct HarnessShared {
    app: AppHandle,
    data_dir: PathBuf,
    api_token: String,
}

pub fn spawn_smoke_harness(app: AppHandle, data_dir: PathBuf, api_token: String) {
    if minion_env_truthy("MINION_DISABLE_SMOKE_HARNESS") {
        return;
    }
    if RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    let shared = HarnessShared {
        app,
        data_dir,
        api_token,
    };
    thread::spawn(move || run_listener(shared));
}

fn minion_env_truthy(name: &str) -> bool {
    std::env::var(name)
        .ok()
        .map(|s| {
            matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn run_listener(shared: HarnessShared) {
    let listener = match TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(_) => {
            RUNNING.store(false, Ordering::SeqCst);
            return;
        }
    };
    let port = match listener.local_addr() {
        Ok(a) => a.port(),
        Err(_) => {
            RUNNING.store(false, Ordering::SeqCst);
            return;
        }
    };
    if let Err(_) = write_discovery_file(&shared.data_dir, port) {
        RUNNING.store(false, Ordering::SeqCst);
        return;
    }
    for stream in listener.incoming().flatten() {
        let shared = shared.clone();
        thread::spawn(move || handle_conn(stream, shared));
    }
}

fn write_discovery_file(data_dir: &Path, port: u16) -> std::io::Result<()> {
    let payload = serde_json::json!({
        "service": "minion-smoke-harness",
        "host": "127.0.0.1",
        "port": port,
        "data_dir": data_dir.to_string_lossy(),
        "auth": {
            "scheme": "Bearer",
            "token_file": data_dir.join(".minion_api_token").to_string_lossy(),
        },
        "endpoints": {
            "health": "GET /health",
            "info": "GET /info",
            "restart_sidecar": "POST /restart-sidecar",
            "open_permissions": "POST /open-permissions",
            "run_probe": "POST /run-probe",
        },
    });
    fs::create_dir_all(data_dir)?;
    let path = data_dir.join("smoke-harness.json");
    fs::write(path, serde_json::to_string_pretty(&payload).unwrap_or_default() + "\n")
}

fn handle_conn(mut stream: TcpStream, shared: HarnessShared) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
    let req = match read_http_request(&mut stream) {
        Some(r) => r,
        None => return,
    };
    let (status, body) = dispatch(&shared, &req);
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
}

struct HttpRequest {
    method: String,
    path: String,
    headers: std::collections::HashMap<String, String>,
    body: String,
}

fn read_http_request(stream: &mut TcpStream) -> Option<HttpRequest> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > MAX_BODY + 8192 {
                    break;
                }
                if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&buf);
    let (head, body) = text
        .split_once("\r\n\r\n")
        .or_else(|| text.split_once("\n\n"))
        .unwrap_or((&text, ""));
    let mut lines = head.lines();
    let first = lines.next()?;
    let mut parts = first.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();
    let mut headers = std::collections::HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    Some(HttpRequest {
        method,
        path: path.split('?').next().unwrap_or(&path).to_string(),
        headers,
        body: body.to_string(),
    })
}

fn authorized(shared: &HarnessShared, req: &HttpRequest) -> bool {
    let auth = req.headers.get("authorization").map(String::as_str).unwrap_or("");
    auth == format!("Bearer {}", shared.api_token)
}

fn dispatch(shared: &HarnessShared, req: &HttpRequest) -> (u16, String) {
    match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/health") => (200, json_ok(serde_json::json!({"ok": true, "service": "minion-smoke-harness"}))),
        ("GET", "/info") => {
            if !authorized(shared, req) {
                return (401, json_err("Unauthorized"));
            }
            (200, json_ok(build_info(shared)))
        }
        ("POST", "/restart-sidecar") => {
            if !authorized(shared, req) {
                return (401, json_err("Unauthorized"));
            }
            match restart_sidecar_via_app(&shared.app) {
                Ok(v) => (200, json_ok(v)),
                Err(e) => (500, json_err(&e)),
            }
        }
        ("POST", "/open-permissions") => {
            if !authorized(shared, req) {
                return (401, json_err("Unauthorized"));
            }
            let pane = parse_json_body(&req.body)
                .and_then(|v| v.get("pane").and_then(|p| p.as_str()).map(str::to_string))
                .unwrap_or_else(|| "screen-recording".to_string());
            match open_permissions(&pane) {
                Ok(()) => (200, json_ok(serde_json::json!({"opened": pane}))),
                Err(e) => (400, json_err(&e)),
            }
        }
        ("POST", "/run-probe") => {
            if !authorized(shared, req) {
                return (401, json_err("Unauthorized"));
            }
            let body = parse_json_body(&req.body).unwrap_or(serde_json::json!({}));
            let mutating = body.get("mutating").and_then(|v| v.as_bool()).unwrap_or(false);
            let include_mcp = body.get("include_mcp").and_then(|v| v.as_bool()).unwrap_or(false);
            match run_probe(shared, mutating, include_mcp) {
                Ok(report) => (200, json_ok(report)),
                Err(e) => (500, json_err(&e)),
            }
        }
        _ => (404, json_err("Not found")),
    }
}

fn json_ok(v: serde_json::Value) -> String {
    serde_json::to_string(&v).unwrap_or_else(|_| "{}".into())
}

fn json_err(msg: &str) -> String {
    serde_json::json!({"ok": false, "error": msg}).to_string()
}

fn parse_json_body(raw: &str) -> Option<serde_json::Value> {
    if raw.trim().is_empty() {
        return Some(serde_json::json!({}));
    }
    serde_json::from_str(raw.trim()).ok()
}

fn build_info(shared: &HarnessShared) -> serde_json::Value {
    let state = shared.app.try_state::<AppState>();
    let (api_port, sidecar_running, bootstrapped, data_dir, inbox) = state
        .as_ref()
        .map(|s| {
            (
                s.api_port,
                s.sidecar.lock().ok().and_then(|g| g.as_ref().map(|c| c.id())).is_some(),
                s.sidecar_python.lock().ok().and_then(|g| g.clone()).is_some(),
                s.data_dir.to_string_lossy().to_string(),
                s.inbox.to_string_lossy().to_string(),
            )
        })
        .unwrap_or((0, false, false, shared.data_dir.to_string_lossy().to_string(), String::new()));
    let sidecar_ready = api_port > 0 && sidecar_status_responds(api_port, 1200);
    let screen = state
        .as_ref()
        .map(|s| crate::screen_context_payload(s.inner()))
        .unwrap_or(serde_json::Value::Null);
    serde_json::json!({
        "service": "minion-smoke-harness",
        "data_dir": data_dir,
        "inbox": inbox,
        "api_port": api_port,
        "api_base": format!("http://127.0.0.1:{api_port}"),
        "sidecar_running": sidecar_running,
        "sidecar_bootstrapped": bootstrapped,
        "sidecar_ready": sidecar_ready,
        "screen_context": screen,
    })
}

fn restart_sidecar_via_app(app: &AppHandle) -> Result<serde_json::Value, String> {
    let state = app.try_state::<AppState>().ok_or("app state unavailable")?;
    crate::restart_sidecar(state)
}

fn open_permissions(pane: &str) -> Result<(), String> {
    crate::open_macos_privacy_settings(pane.to_string())
}

fn sidecar_status_responds(port: u16, timeout_ms: u64) -> bool {
    crate::sidecar_status_responds(port, timeout_ms)
}

const ST_PASS: &str = "pass";
const ST_FAIL: &str = "fail";
const ST_SKIP: &str = "skip";

/// Roll a pillar's checks up to one status: fail wins, then pass, then skip.
/// A pillar with no checks at all is "skip" (not exercised), never a false pass.
fn pillar_status(checks: &[serde_json::Value], pillar: &str) -> &'static str {
    let mut seen = false;
    let mut any_pass = false;
    for c in checks {
        if c.get("pillar").and_then(|v| v.as_str()) != Some(pillar) {
            continue;
        }
        seen = true;
        match c.get("status").and_then(|v| v.as_str()) {
            Some(s) if s == ST_FAIL => return ST_FAIL,
            Some(s) if s == ST_PASS => any_pass = true,
            _ => {}
        }
    }
    if !seen {
        return ST_SKIP;
    }
    if any_pass {
        ST_PASS
    } else {
        ST_SKIP
    }
}

fn run_probe(shared: &HarnessShared, mutating: bool, include_mcp: bool) -> Result<serde_json::Value, String> {
    let state = shared.app.try_state::<AppState>().ok_or("app state unavailable")?;
    let api_port = state.api_port;
    let api_token = state.api_token.clone();
    let data_dir = state.data_dir.clone();
    if !sidecar_status_responds(api_port, 3000) {
        return Err(format!("sidecar not ready on port {api_port}"));
    }
    let started = Instant::now();
    let mut checks: Vec<serde_json::Value> = Vec::new();
    let mut marker: Option<String> = None;
    let mut failures: Vec<String> = Vec::new();

    // A check records a single probe assertion against one product pillar.
    //   pillar: which capability it proves (core/import/ambient/recording/output)
    //   level:  how deeply it proves it (foundation/status/pipeline/capture/gate)
    //   status: pass | fail | skip — skip means "not exercised here", never a pass
    macro_rules! record {
        ($name:expr, $pillar:expr, $level:expr, $status:expr, $detail:expr) => {{
            let st: &str = $status;
            checks.push(serde_json::json!({
                "name": $name,
                "pillar": $pillar,
                "level": $level,
                "status": st,
                // `ok` retained for back-compat with older report readers.
                "ok": st == ST_PASS,
                "detail": $detail,
            }));
            if st == ST_FAIL {
                failures.push($name.to_string());
            }
        }};
    }
    macro_rules! check {
        ($name:expr, $pillar:expr, $level:expr, $ok:expr, $detail:expr) => {
            record!(
                $name,
                $pillar,
                $level,
                if $ok { ST_PASS } else { ST_FAIL },
                $detail
            )
        };
    }

    let status = http_get_json(api_port, "/status", &api_token, 5000)?;
    check!("status", "core", "foundation", status.get("database").and_then(|d| d.get("ok")).and_then(|v| v.as_bool()).unwrap_or(false), status.clone());

    let health = http_get_json(api_port, "/health", &api_token, 5000)?;
    check!("health", "core", "foundation", health.get("status").and_then(|v| v.as_str()) == Some("ok"), health.clone());

    if mutating {
        marker = Some(append_dogfood_event(&data_dir)?);
        let remembered = http_post_json(
            api_port,
            "/screen-memory/remember",
            &api_token,
            &serde_json::json!({"ingest_screenshots": false, "run_adapters": false, "max_lines": 2000}),
            60_000,
        )?;
        let ambient = remembered
            .get("ambient")
            .and_then(|a| a.get("ingested"))
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        check!("remember_screen", "ambient", "pipeline", ambient >= 1, remembered.clone());

        let recall = http_get_json(api_port, "/screen-memory/what-was-i-doing?minutes=30", &api_token, 15_000)?;
        let recall_text = recall.to_string();
        let recall_ok = recall_text.contains("DOGFOOD_EXPECTATION")
            || recall_text.contains("Dogfood Minion production smoke");
        check!("recall", "ambient", "pipeline", recall_ok, recall.clone());

        let guidance = http_get_json(api_port, "/screen-memory/guidance?minutes=30", &api_token, 15_000)?;
        let guidance_ok = guidance.get("do_this").and_then(|v| v.as_str()).map(|s| !s.trim().is_empty()).unwrap_or(false);
        check!("guidance", "ambient", "pipeline", guidance_ok, guidance.clone());

        let search = http_get_json(
            api_port,
            "/screen-memory/search?q=DOGFOOD_EXPECTATION%20Minion%20production%20smoke&top_k=5",
            &api_token,
            30_000,
        )?;
        let hits = search.get("hits").and_then(|v| v.as_array()).map(|a| !a.is_empty()).unwrap_or(false);
        check!("screen_search", "ambient", "pipeline", hits, search.clone());
    } else {
        let screen_status = http_get_json(api_port, "/screen-memory/status?minutes=60", &api_token, 15_000)?;
        check!("screen_memory_status", "ambient", "status", screen_status.is_object(), screen_status.clone());
        let feed = http_get_json(api_port, "/feed", &api_token, 10_000)?;
        check!("feed", "ambient", "status", feed.is_object(), feed.clone());
    }

    if include_mcp {
        let denied = http_post_json(
            api_port,
            "/mcp",
            "",
            &serde_json::json!({"jsonrpc": "2.0", "id": 0, "method": "ping"}),
            10_000,
        )
        .unwrap_or_else(|_| serde_json::json!({"detail": ""}));
        let denied_ok = denied
            .get("detail")
            .and_then(|v| v.as_str())
            .map(|s| s.contains("Password"))
            .unwrap_or(false);
        check!("mcp_auth_gate", "output", "gate", denied_ok, denied.clone());
    }

    let ok = failures.is_empty();
    let elapsed_ms = started.elapsed().as_millis() as u64;
    let pillars = serde_json::json!({
        "core": pillar_status(&checks, "core"),
        "import": pillar_status(&checks, "import"),
        "ambient": pillar_status(&checks, "ambient"),
        "recording": pillar_status(&checks, "recording"),
        "output": pillar_status(&checks, "output"),
    });
    let report = serde_json::json!({
        "ok": ok,
        "mutating": mutating,
        "marker": marker,
        "failures": failures,
        "pillars": pillars,
        "checks": checks,
        "elapsed_ms": elapsed_ms,
        "api_port": api_port,
        "data_dir": data_dir.to_string_lossy(),
    });
    let _ = write_audit_log(&data_dir, &report);
    Ok(report)
}

fn append_dogfood_event(data_dir: &Path) -> Result<String, String> {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let marker = format!("dogfood-prod-smoke-{}", ts as u64);
    let stream = data_dir.join("ambient").join("stream.jsonl");
    fs::create_dir_all(stream.parent().unwrap_or(data_dir)).map_err(|e| e.to_string())?;
    let event = serde_json::json!({
        "ts": ts,
        "kind": "window_snapshot",
        "app_name": "Cursor",
        "window_title": "Dogfood Minion production smoke",
        "window_id": marker,
        "ax_hash": marker,
        "ax_text_sample": "DOGFOOD_EXPECTATION: user worked on Minion production smoke acceptance test. Minion should remember this work and suggest a next action.",
        "dedupe_key": marker,
    });
    let line = serde_json::to_string(&event).map_err(|e| e.to_string())? + "\n";
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&stream)
        .and_then(|mut f| f.write_all(line.as_bytes()))
        .map_err(|e| e.to_string())?;
    Ok(marker)
}

fn write_audit_log(data_dir: &Path, report: &serde_json::Value) -> std::io::Result<PathBuf> {
    let dir = data_dir.join("smoke-runs");
    fs::create_dir_all(&dir)?;
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let path = dir.join(format!("prod-smoke-{ts}.json"));
    fs::write(&path, serde_json::to_string_pretty(report).unwrap_or_default() + "\n")?;
    Ok(path)
}

fn http_get_json(port: u16, path: &str, token: &str, timeout_ms: u64) -> Result<serde_json::Value, String> {
    let body = http_request("GET", port, path, token, None, timeout_ms)?;
    serde_json::from_str(body.trim()).map_err(|e| format!("invalid json from {path}: {e}"))
}

fn http_post_json(
    port: u16,
    path: &str,
    token: &str,
    payload: &serde_json::Value,
    timeout_ms: u64,
) -> Result<serde_json::Value, String> {
    let raw = payload.to_string();
    let body = http_request("POST", port, path, token, Some(&raw), timeout_ms)?;
    if body.trim().is_empty() {
        return Ok(serde_json::json!({}));
    }
    serde_json::from_str(body.trim()).map_err(|e| format!("invalid json from {path}: {e}"))
}

fn http_request(
    method: &str,
    port: u16,
    path: &str,
    token: &str,
    body: Option<&str>,
    timeout_ms: u64,
) -> Result<String, String> {
    let addr: SocketAddr = format!("127.0.0.1:{port}")
        .parse::<SocketAddr>()
        .map_err(|e: std::net::AddrParseError| e.to_string())?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms))
        .map_err(|e| format!("connect {port}: {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(timeout_ms)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(timeout_ms)));
    let payload = body.unwrap_or("");
    let auth = if !token.is_empty() && method != "GET" {
        format!("Authorization: Bearer {token}\r\n")
    } else {
        String::new()
    };
    let req = if method == "POST" {
        format!(
            "POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n{auth}\r\n{payload}",
            payload.len()
        )
    } else {
        format!(
            "GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: application/json\r\nConnection: close\r\n{auth}\r\n"
        )
    };
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write request: {e}"))?;
    read_http_body(&mut stream).ok_or_else(|| "empty response".to_string())
}

fn read_http_body(stream: &mut TcpStream) -> Option<String> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > 4_000_000 {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&buf).into_owned();
    if let Some(idx) = text.find("\r\n\r\n") {
        return Some(text[idx + 4..].to_string());
    }
    text.find("\n\n").map(|idx| text[idx + 2..].to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_empty_post_body() {
        let v = parse_json_body("").unwrap();
        assert!(v.is_object());
    }

    #[test]
    fn bearer_token_format() {
        let token = "secret";
        let auth = format!("Bearer {token}");
        assert_eq!(auth, "Bearer secret");
    }

    #[test]
    fn pillar_status_rolls_up() {
        let checks = vec![
            serde_json::json!({"pillar": "core", "status": "pass"}),
            serde_json::json!({"pillar": "ambient", "status": "pass"}),
            serde_json::json!({"pillar": "ambient", "status": "fail"}),
            serde_json::json!({"pillar": "recording", "status": "skip"}),
        ];
        assert_eq!(pillar_status(&checks, "core"), ST_PASS);
        // fail wins within a pillar
        assert_eq!(pillar_status(&checks, "ambient"), ST_FAIL);
        // present but only skips → skip
        assert_eq!(pillar_status(&checks, "recording"), ST_SKIP);
        // pillar with no checks → skip, never a false pass
        assert_eq!(pillar_status(&checks, "import"), ST_SKIP);
    }
}
