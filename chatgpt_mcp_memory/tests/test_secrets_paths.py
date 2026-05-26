from pathlib import Path

from secrets_paths import read_gemini_api_key_file


def test_read_gemini_key_file(tmp_path: Path) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("test-key-abc\n", encoding="utf-8")
    assert read_gemini_api_key_file(tmp_path) == "test-key-abc"
