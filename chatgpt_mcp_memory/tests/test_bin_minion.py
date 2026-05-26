from __future__ import annotations

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_MINION = REPO_ROOT / "bin" / "minion"


def _load_minion_bin():
    loader = SourceFileLoader("minion_bin", str(BIN_MINION))
    spec = importlib.util.spec_from_loader("minion_bin", loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_screen_memory_workspace_defaults_to_repo_checkout(monkeypatch) -> None:
    mod = _load_minion_bin()
    monkeypatch.chdir(REPO_ROOT)

    assert mod._screen_memory_workspace(None) == REPO_ROOT / "chatgpt_mcp_memory"


def test_screen_memory_workspace_honors_explicit_path(tmp_path: Path, monkeypatch) -> None:
    mod = _load_minion_bin()
    monkeypatch.chdir(REPO_ROOT)

    assert mod._screen_memory_workspace(str(tmp_path)) == tmp_path.resolve()


def test_screen_memory_cmd_uses_repo_workspace_without_flag(tmp_path: Path, monkeypatch) -> None:
    mod = _load_minion_bin()
    monkeypatch.chdir(REPO_ROOT)
    calls = {}

    def fake_run(script, extra, *, workspace=None):
        calls["script"] = script
        calls["extra"] = extra
        calls["workspace"] = workspace
        return 0

    monkeypatch.setattr(mod, "_run_in_venv", fake_run)
    args = argparse.Namespace(derived_dir=str(tmp_path), workspace=None)

    assert mod._screen_memory_cmd(args, "status", ["--minutes", "60"]) == 0
    assert calls["script"] == "screen_memory_cli.py"
    assert calls["extra"] == ["--data-dir", str(tmp_path.resolve()), "status", "--minutes", "60"]
    assert calls["workspace"] == REPO_ROOT / "chatgpt_mcp_memory"


def test_screen_memory_permissions_no_open_prints_manual_url(monkeypatch, capsys) -> None:
    mod = _load_minion_bin()
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    rc = mod.cmd_screen_memory_permissions(argparse.Namespace(no_open=True))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Screen Recording" in out
    assert mod.SCREEN_RECORDING_SETTINGS_URL in out
    assert calls == []


def test_screen_memory_permissions_opens_macos_settings(monkeypatch, capsys) -> None:
    mod = _load_minion_bin()
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod.cmd_screen_memory_permissions(argparse.Namespace(no_open=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [(["open", mod.SCREEN_RECORDING_SETTINGS_URL], {"check": False})]
    assert "Opened System Settings" in out
