from pathlib import Path
from unittest.mock import patch

from onboarding_chat import onboarding_reply


def test_onboarding_falls_back_without_gemini(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: False)

    text, used = onboarding_reply(step="name", data_dir=tmp_path)

    assert used is False
    assert "keep your data yours" in text.lower()
    assert "call you" in text.lower()


def test_onboarding_uses_gemini_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: True)
    monkeypatch.setattr("gemini_client.gemini_model", lambda _dd=None: "gemini-test")

    with patch("gemini_client.gemini_chat", return_value="Hi — what should I call you?"):
        text, used = onboarding_reply(step="name", data_dir=tmp_path)

    assert used is True
    assert "call you" in text.lower()
