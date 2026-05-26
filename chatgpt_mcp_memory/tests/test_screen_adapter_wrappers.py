"""Screen adapter wrapper normalization tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str) -> ModuleType:
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_marlin_wrapper_normalizes_caption_segments() -> None:
    mod = _load("marlin_hf_adapter", "scripts/screen_adapters/marlin_hf_adapter.py")
    out = mod.normalize_result(
        {
            "caption": "User reviews payout history",
            "segments": [
                {"caption": "Export button appears", "timestamp": 4, "duration": 2, "confidence": 0.8}
            ],
        }
    )
    assert out["scene"] == "User reviews payout history"
    assert out["events"][0]["summary"] == "Export button appears"
    assert out["events"][0]["start_sec"] == 4
    assert out["events"][0]["duration"] == 2


def test_omniparser_wrapper_normalizes_element_lists() -> None:
    mod = _load("omniparser_json_adapter", "scripts/screen_adapters/omniparser_json_adapter.py")
    out = mod.normalize_result(
        {
            "parsed_content_list": [
                {"type": "button", "content": "Export", "bbox": [10, 20, 80, 24], "score": 0.9}
            ]
        }
    )
    assert out["visible_elements"][0]["role"] == "button"
    assert out["visible_elements"][0]["label"] == "Export"
    assert out["visible_elements"][0]["source"] == "OmniParser"


def test_general_vlm_wrapper_normalizes_text_caption() -> None:
    mod = _load("general_vlm_json_adapter", "scripts/screen_adapters/general_vlm_json_adapter.py")
    out = mod.normalize_result({"description": "Maybe a Stripe payout dashboard", "confidence": 0.29})
    assert out["scene"] == "Maybe a Stripe payout dashboard"
    assert out["confidence"] == 0.29
