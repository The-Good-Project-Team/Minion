import os
from pathlib import Path

from onboarding_chat import onboarding_reply


def test_onboarding_falls_back_without_gemini(tmp_path: Path) -> None:
    old_key = os.environ.pop("GEMINI_API_KEY", None)
    old_disable_files = os.environ.get("MINION_GEMINI_DISABLE_SECRET_FILES")
    os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = "1"
    try:
        text, used = onboarding_reply(step="name", data_dir=tmp_path)
    finally:
        if old_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_key
        if old_disable_files is None:
            os.environ.pop("MINION_GEMINI_DISABLE_SECRET_FILES", None)
        else:
            os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = old_disable_files


    assert used is False
    assert "keep your data yours" in text.lower()
    assert "call you" in text.lower()


def test_onboarding_uses_local_gemini_server(tmp_path: Path, fake_gemini) -> None:
    fake_gemini("Hi - what should I call you?")
    text, used = onboarding_reply(step="name", data_dir=tmp_path)

    assert used is True
    assert "call you" in text.lower()
