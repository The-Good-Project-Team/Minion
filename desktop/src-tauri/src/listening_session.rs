//! Mic listening: manual sessions (`ls_*`) and full-time mode (`fl_*`, persisted).

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::json;
use tauri::{AppHandle, Emitter};

static RECORDING: AtomicBool = AtomicBool::new(false);
static FULL_MODE: AtomicBool = AtomicBool::new(false);
static SESSION_ID: Mutex<Option<String>> = Mutex::new(None);

const FULL_SESSION_FILE: &str = "ambient/listening/full_session.json";
const WAKE_STATE_FILE: &str = "ambient/listening_wake_state.json";
pub const WAKE_NOTIFY_FILE: &str = "ambient/listening_wake_notify.json";

#[derive(Serialize, Deserialize)]
struct FullSessionState {
    session_id: String,
    segment: u32,
}

#[derive(Serialize, Deserialize, Default)]
struct WakeState {
    last_wake_ts: f64,
    last_wake_excerpt: String,
}

fn ts_unix_float() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn full_listening_enabled(data_dir: &Path) -> bool {
    let path = data_dir.join("settings.json");
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return false;
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return false;
    };
    if v.get("full_listening_enabled").and_then(|x| x.as_bool()) == Some(true) {
        return true;
    }
    v.get("ambient_collectors")
        .and_then(|c| c.get("full_listening"))
        .and_then(|x| x.as_bool())
        .unwrap_or(false)
}

fn load_full_session(data_dir: &Path) -> Option<FullSessionState> {
    let p = data_dir.join(FULL_SESSION_FILE);
    let raw = std::fs::read_to_string(&p).ok()?;
    serde_json::from_str(&raw).ok()
}

fn save_full_session(data_dir: &Path, state: &FullSessionState) -> Result<(), String> {
    let p = data_dir.join(FULL_SESSION_FILE);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&p, serde_json::to_string(state).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

fn clear_full_session(data_dir: &Path) {
    let _ = std::fs::remove_file(data_dir.join(FULL_SESSION_FILE));
}

fn load_wake_state(data_dir: &Path) -> WakeState {
    let p = data_dir.join(WAKE_STATE_FILE);
    let Ok(raw) = std::fs::read_to_string(&p) else {
        return WakeState::default();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

fn save_wake_state(data_dir: &Path, state: &WakeState) -> Result<(), String> {
    let p = data_dir.join(WAKE_STATE_FILE);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&p, serde_json::to_string(state).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

fn start_recording(data_dir: &Path, session_id: String, full: bool) -> Result<(), String> {
    if RECORDING.swap(true, Ordering::SeqCst) {
        return Err(if FULL_MODE.load(Ordering::SeqCst) {
            "Full listening already active".into()
        } else {
            "Listening session already active".into()
        });
    }
    FULL_MODE.store(full, Ordering::SeqCst);
    {
        let mut g = SESSION_ID.lock().map_err(|e| e.to_string())?;
        *g = Some(session_id.clone());
    }
    let session_dir = data_dir.join("ambient/listening").join(&session_id);
    std::fs::create_dir_all(&session_dir).map_err(|e| e.to_string())?;
    let record = json!({
        "ts": ts_unix_float(),
        "kind": if full { "full_listening_start" } else { "listening_start" },
        "session_id": session_id,
        "dedupe_key": format!("listen_start:{session_id}"),
    });
    crate::ambient_stream::append_ambient_record(data_dir, &record);
    let dd = data_dir.to_path_buf();
    let sid_bg = session_id;
    let start_segment = if full {
        load_full_session(data_dir)
            .map(|s| s.segment)
            .unwrap_or(0)
    } else {
        0
    };
    thread::spawn(move || record_loop(dd, sid_bg, full, start_segment));
    Ok(())
}

fn stop_recording(data_dir: &Path, full: bool) -> Result<serde_json::Value, String> {
    RECORDING.store(false, Ordering::SeqCst);
    FULL_MODE.store(false, Ordering::SeqCst);
    let sid = SESSION_ID
        .lock()
        .map_err(|e| e.to_string())?
        .take()
        .unwrap_or_default();
    if full {
        clear_full_session(data_dir);
    }
    let kind = if full {
        "full_listening_end"
    } else {
        "listening_end"
    };
    let record = json!({
        "ts": ts_unix_float(),
        "kind": kind,
        "session_id": sid,
        "dedupe_key": format!("listen_end:{sid}"),
    });
    crate::ambient_stream::append_ambient_record(data_dir, &record);
    Ok(json!({ "session_id": sid, "active": false, "full_listening": false }))
}

#[tauri::command]
pub fn listening_start(data_dir: String) -> Result<serde_json::Value, String> {
    if FULL_MODE.load(Ordering::SeqCst) {
        return Err("Full listening is on — disable it in Settings first".into());
    }
    let dir = PathBuf::from(&data_dir);
    let sid = format!("ls_{}", (ts_unix_float() * 1000.0) as u64);
    start_recording(&dir, sid.clone(), false)?;
    Ok(json!({ "session_id": sid, "active": true, "full_listening": false }))
}

#[tauri::command]
pub fn listening_stop(data_dir: String) -> Result<serde_json::Value, String> {
    if FULL_MODE.load(Ordering::SeqCst) {
        return Err("Use full listening stop or disable in Settings".into());
    }
    stop_recording(&PathBuf::from(&data_dir), false)
}

#[tauri::command]
pub fn full_listening_start(data_dir: String) -> Result<serde_json::Value, String> {
    let dir = PathBuf::from(&data_dir);
    if RECORDING.load(Ordering::SeqCst) && !FULL_MODE.load(Ordering::SeqCst) {
        return Err("Manual listening session active — stop it first".into());
    }
    if RECORDING.load(Ordering::SeqCst) && FULL_MODE.load(Ordering::SeqCst) {
        return Ok(status_payload(&dir));
    }
    let sid = load_full_session(&dir)
        .map(|s| s.session_id)
        .unwrap_or_else(|| format!("fl_{}", (ts_unix_float() * 1000.0) as u64));
    let seg = load_full_session(&dir).map(|s| s.segment).unwrap_or(0);
    save_full_session(
        &dir,
        &FullSessionState {
            session_id: sid.clone(),
            segment: seg,
        },
    )?;
    start_recording(&dir, sid, true)?;
    Ok(status_payload(&dir))
}

#[tauri::command]
pub fn full_listening_stop(data_dir: String) -> Result<serde_json::Value, String> {
    let dir = PathBuf::from(&data_dir);
    if !FULL_MODE.load(Ordering::SeqCst) && !RECORDING.load(Ordering::SeqCst) {
        clear_full_session(&dir);
        return Ok(status_payload(&dir));
    }
    stop_recording(&dir, true)
}

#[tauri::command]
pub fn full_listening_status(data_dir: String) -> serde_json::Value {
    status_payload(&PathBuf::from(&data_dir))
}

#[tauri::command]
pub fn full_listening_sync(data_dir: String) -> Result<serde_json::Value, String> {
    let dir = PathBuf::from(&data_dir);
    if full_listening_enabled(&dir) {
        full_listening_start(data_dir)
    } else if FULL_MODE.load(Ordering::SeqCst) {
        full_listening_stop(dir.to_string_lossy().to_string())
    } else {
        Ok(status_payload(&dir))
    }
}

pub fn maybe_auto_start_full(data_dir: PathBuf) {
    if !full_listening_enabled(&data_dir) {
        return;
    }
    let dd = data_dir.to_string_lossy().to_string();
    let _ = full_listening_start(dd);
}

pub fn spawn_wake_watcher(app: AppHandle, data_dir: PathBuf) {
    thread::spawn(move || {
        let notify = data_dir.join(WAKE_NOTIFY_FILE);
        loop {
            thread::sleep(Duration::from_millis(400));
            let Ok(raw) = std::fs::read_to_string(&notify) else {
                continue;
            };
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
                let _ = std::fs::remove_file(&notify);
                continue;
            };
            let excerpt = v
                .get("excerpt")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
            let ts = v.get("ts").and_then(|x| x.as_f64()).unwrap_or_else(ts_unix_float);
            let payload = json!({ "excerpt": excerpt, "ts": ts });
            let _ = app.emit("listening://wake", &payload);
            let _ = save_wake_state(
                &data_dir,
                &WakeState {
                    last_wake_ts: ts,
                    last_wake_excerpt: excerpt,
                },
            );
            let _ = std::fs::remove_file(&notify);
        }
    });
}

fn status_payload(data_dir: &Path) -> serde_json::Value {
    let wake = load_wake_state(data_dir);
    let sid = SESSION_ID.lock().ok().and_then(|g| g.clone()).unwrap_or_default();
    json!({
        "active": is_active(),
        "full_listening": FULL_MODE.load(Ordering::SeqCst),
        "session_id": sid,
        "last_wake_ts": wake.last_wake_ts,
        "last_wake_excerpt": wake.last_wake_excerpt,
    })
}

pub fn is_active() -> bool {
    RECORDING.load(Ordering::SeqCst)
}

pub fn is_full_active() -> bool {
    FULL_MODE.load(Ordering::SeqCst)
}

#[tauri::command]
pub fn listening_status(data_dir: String) -> serde_json::Value {
    status_payload(&PathBuf::from(&data_dir))
}

fn record_loop(data_dir: PathBuf, session_id: String, full: bool, mut segment: u32) {
    let session_dir = data_dir.join("ambient/listening").join(&session_id);
    while RECORDING.load(Ordering::SeqCst) {
        let wav_path = session_dir.join(format!("{segment:04}.wav"));
        let ok = record_wav_segment(&wav_path, Duration::from_secs(30));
        if ok {
            let record = json!({
                "ts": ts_unix_float(),
                "kind": "listening_chunk",
                "session_id": session_id,
                "wav": wav_path.file_name().and_then(|n| n.to_str()).unwrap_or("seg.wav"),
                "duration_sec": 30.0,
                "full_listening": full,
                "dedupe_key": format!("listen_chunk:{session_id}:{segment}"),
            });
            crate::ambient_stream::append_ambient_record(&data_dir, &record);
            if full {
                segment = segment.wrapping_add(1);
                let _ = save_full_session(
                    &data_dir,
                    &FullSessionState {
                        session_id: session_id.clone(),
                        segment,
                    },
                );
            }
        }
        if !full {
            segment = segment.wrapping_add(1);
        }
        if !RECORDING.load(Ordering::SeqCst) {
            break;
        }
    }
}

fn record_wav_segment(path: &Path, duration: Duration) -> bool {
    #[cfg(target_os = "macos")]
    {
        return record_wav_segment_cpal(path, duration);
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (path, duration);
        false
    }
}

#[cfg(target_os = "macos")]
fn record_wav_segment_cpal(path: &Path, duration: Duration) -> bool {
    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
    use std::sync::{Arc, Mutex};

    let host = cpal::default_host();
    let device = match host.default_input_device() {
        Some(d) => d,
        None => return false,
    };
    let config = match device.default_input_config() {
        Ok(c) => c,
        Err(_) => return false,
    };
    let sample_rate = config.sample_rate().0;
    let channels = config.channels() as usize;
    let buffer: Arc<Mutex<Vec<f32>>> = Arc::new(Mutex::new(Vec::new()));
    let buf_cap = buffer.clone();
    let err_fn = |e| eprintln!("listening stream error: {e}");
    let stream = match config.sample_format() {
        cpal::SampleFormat::F32 => device.build_input_stream(
            &config.into(),
            move |data: &[f32], _| {
                if let Ok(mut b) = buf_cap.lock() {
                    b.extend_from_slice(data);
                }
            },
            err_fn,
            None,
        ),
        cpal::SampleFormat::I16 => {
            let buf_i16 = buffer.clone();
            device.build_input_stream(
                &config.into(),
                move |data: &[i16], _| {
                    if let Ok(mut b) = buf_i16.lock() {
                        for &s in data {
                            b.push(s as f32 / i16::MAX as f32);
                        }
                    }
                },
                err_fn,
                None,
            )
        }
        _ => return false,
    };
    let stream = match stream {
        Ok(s) => s,
        Err(_) => return false,
    };
    if stream.play().is_err() {
        return false;
    }
    thread::sleep(duration);
    drop(stream);
    let samples = match buffer.lock() {
        Ok(b) => b.clone(),
        Err(_) => return false,
    };
    if samples.len() < sample_rate as usize / 10 {
        return false;
    }
    write_wav_f32(path, &samples, sample_rate, channels as u16)
}

fn write_wav_f32(path: &Path, samples: &[f32], rate: u32, channels: u16) -> bool {
    use std::fs::File;
    use std::io::Write;
    let mut file = match File::create(path) {
        Ok(f) => f,
        Err(_) => return false,
    };
    let mut pcm: Vec<i16> = Vec::with_capacity(samples.len());
    for &s in samples {
        let v = (s.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
        pcm.push(v);
    }
    let byte_rate = rate * channels as u32 * 2;
    let data_size = (pcm.len() * 2) as u32;
    let mut header = Vec::new();
    header.extend_from_slice(b"RIFF");
    header.extend_from_slice(&(36 + data_size).to_le_bytes());
    header.extend_from_slice(b"WAVEfmt ");
    header.extend_from_slice(&16u32.to_le_bytes());
    header.extend_from_slice(&1u16.to_le_bytes());
    header.extend_from_slice(&channels.to_le_bytes());
    header.extend_from_slice(&rate.to_le_bytes());
    header.extend_from_slice(&byte_rate.to_le_bytes());
    header.extend_from_slice(&(channels as u32 * 2).to_le_bytes());
    header.extend_from_slice(&16u16.to_le_bytes());
    header.extend_from_slice(b"data");
    header.extend_from_slice(&data_size.to_le_bytes());
    if file.write_all(&header).is_err() {
        return false;
    }
    for s in pcm {
        if file.write_all(&s.to_le_bytes()).is_err() {
            return false;
        }
    }
    true
}
