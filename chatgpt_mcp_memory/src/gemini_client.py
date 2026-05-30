"""Google Gemini HTTP client (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)

# Pro default: ~288 scheduler ticks/day + replies stays well under typical 1.5k RPD caps.
DEFAULT_MODEL = "gemini-2.5-pro"
FORTY_TWO_DEFAULT_MODEL = "gemini-2.5-pro"
GRAPH_MINE_DEFAULT_MODEL = "gemini-2.0-flash-lite"
FALLBACK_MODEL = "gemini-2.5-flash"  # when Pro hits free-tier / quota limits
_DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _api_base() -> str:
    return (os.environ.get("MINION_GEMINI_API_BASE") or _DEFAULT_API_BASE).rstrip("/")


def gemini_api_key(data_dir: Optional[Path] = None) -> Optional[str]:
    for name in ("GEMINI_API_KEY", "MINION_GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    if os.environ.get("MINION_GEMINI_DISABLE_SECRET_FILES", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            from secrets_paths import read_gemini_api_key_file

            v = read_gemini_api_key_file(data_dir)
            if v:
                return v
        except Exception:
            log.debug("secrets file gemini key load failed", exc_info=True)
    if data_dir:
        try:
            from settings import load_settings

            v = str(load_settings(Path(data_dir)).get("gemini_api_key") or "").strip()
            if v:
                return v
        except Exception:
            log.debug("settings gemini key load failed", exc_info=True)
    return None


def gemini_configured(data_dir: Optional[Path] = None) -> bool:
    return bool(gemini_api_key(data_dir))


def _model_from_settings(data_dir: Optional[Path], key: str, fallback: str) -> str:
    if not data_dir:
        return fallback
    try:
        from settings import load_settings

        v = str(load_settings(Path(data_dir)).get(key) or "").strip()
        return v or fallback
    except Exception:
        return fallback


def gemini_model(data_dir: Optional[Path] = None) -> str:
    env = (os.environ.get("MINION_GEMINI_MODEL") or "").strip()
    if env:
        return env
    return _model_from_settings(data_dir, "gemini_model", DEFAULT_MODEL)


def forty_two_gemini_model(data_dir: Optional[Path] = None) -> str:
    env = (os.environ.get("MINION_42_GEMINI_MODEL") or "").strip()
    if env:
        return env
    return _model_from_settings(data_dir, "forty_two_gemini_model", FORTY_TWO_DEFAULT_MODEL)


def graph_mine_gemini_model(data_dir: Optional[Path] = None) -> str:
    env = (os.environ.get("MINION_MINE_GEMINI_MODEL") or "").strip()
    if env:
        return env
    return _model_from_settings(data_dir, "graph_mine_gemini_model", GRAPH_MINE_DEFAULT_MODEL)


def _post_json(url: str, payload: Dict[str, Any], *, timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_contents(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role") or "user")
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        gemini_role = "model" if role == "assistant" else "user"
        out.append({"role": gemini_role, "parts": [{"text": text}]})
    return out


def _generate(
    *,
    key: str,
    mdl: str,
    payload: Dict[str, Any],
    timeout_seconds: float,
    stream: bool,
) -> Any:
    path = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = f"{_api_base()}/{mdl}:{path}?key={key}"
    if stream:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout_seconds)
    return _post_json(url, payload, timeout=timeout_seconds)


def _as_runtime_error(exc: BaseException) -> RuntimeError:
    if isinstance(exc, RuntimeError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return RuntimeError(f"gemini_http_{exc.code}: {detail}")
    return RuntimeError(str(exc))


def _should_fallback(exc: BaseException, mdl: str) -> bool:
    if mdl == FALLBACK_MODEL:
        return False
    msg = str(exc)
    return "429" in msg or "404" in msg


def gemini_chat(
    *,
    system: str,
    messages: List[Dict[str, str]],
    data_dir: Optional[Path] = None,
    model: Optional[str] = None,
    temperature: float = 0.45,
    max_output_tokens: int = 700,
    timeout_seconds: float = 60.0,
    response_mime_type: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
) -> str:
    key = gemini_api_key(data_dir)
    if not key:
        raise RuntimeError("gemini_not_configured")
    primary = model or gemini_model(data_dir)
    attempts: List[tuple[str, int]] = [(primary, max_output_tokens)]
    if primary != FALLBACK_MODEL:
        attempts.append((FALLBACK_MODEL, max(max_output_tokens, 8192)))
    last_body: Any = None
    for mdl, tok in attempts:
        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": _build_contents(messages),
            "generationConfig": _generation_config(
                temperature=temperature,
                max_output_tokens=tok,
                response_mime_type=response_mime_type,
                response_schema=response_schema,
            ),
        }
        try:
            body = _generate(
                key=key, mdl=mdl, payload=payload, timeout_seconds=timeout_seconds, stream=False
            )
        except (urllib.error.HTTPError, RuntimeError) as e:
            if _should_fallback(e, mdl) and mdl != FALLBACK_MODEL:
                log.info("gemini %s unavailable, trying %s", mdl, FALLBACK_MODEL)
                continue
            raise _as_runtime_error(e) from e
        last_body = body
        text = _extract_text(body)
        if text.strip():
            return text.strip()
        log.warning(
            "gemini empty text model=%s finish=%s usage=%s",
            mdl,
            _finish_reason(body),
            body.get("usageMetadata") if isinstance(body, dict) else None,
        )
    raise RuntimeError("gemini_empty_response")


def gemini_chat_stream(
    *,
    system: str,
    messages: List[Dict[str, str]],
    data_dir: Optional[Path] = None,
    model: Optional[str] = None,
    temperature: float = 0.45,
    max_output_tokens: int = 700,
    timeout_seconds: float = 90.0,
) -> Iterator[str]:
    key = gemini_api_key(data_dir)
    if not key:
        raise RuntimeError("gemini_not_configured")
    mdl = model or gemini_model(data_dir)
    payload: Dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": _build_contents(messages),
        "generationConfig": _generation_config(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    }
    try:
        resp = _generate(
            key=key, mdl=mdl, payload=payload, timeout_seconds=timeout_seconds, stream=True
        )
    except (urllib.error.HTTPError, RuntimeError) as e:
        if _should_fallback(e, mdl):
            log.info("gemini stream %s unavailable, falling back to %s", mdl, FALLBACK_MODEL)
            try:
                resp = _generate(
                    key=key,
                    mdl=FALLBACK_MODEL,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                    stream=True,
                )
            except (urllib.error.HTTPError, RuntimeError) as e2:
                raise _as_runtime_error(e2) from e2
        else:
            raise _as_runtime_error(e) from e

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        piece = _extract_text(parsed)
        if piece:
            yield piece


def _extract_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    texts: List[str] = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            texts.append(p["text"])
    return "".join(texts)


def _finish_reason(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    return str((candidates[0] or {}).get("finishReason") or "")


def _generation_config(
    *,
    temperature: float,
    max_output_tokens: int,
    response_mime_type: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    gen: Dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if response_mime_type:
        gen["responseMimeType"] = response_mime_type
    if response_schema:
        gen["responseSchema"] = response_schema
    return gen
