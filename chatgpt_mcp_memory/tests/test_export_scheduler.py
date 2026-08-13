"""Tests for export scheduler."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from export_scheduler import (
    _is_export_file,
    export_interval_sec,
    export_profile_id,
    export_watch_path,
    tick,
    trigger_manual_export,
)


def test_is_export_file():
    """Test export file detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # JSON files should be detected
        json_file = tmpdir / "export.json"
        json_file.write_text("{}")
        assert _is_export_file(json_file) is True
        
        # ZIP files should be detected
        zip_file = tmpdir / "export.zip"
        zip_file.write_bytes(b"fake zip")
        assert _is_export_file(zip_file) is True
        
        # Files with export keywords in name
        chatgpt_file = tmpdir / "chatgpt_data.txt"
        chatgpt_file.write_text("data")
        assert _is_export_file(chatgpt_file) is True
        
        # Regular files without export keywords
        regular_file = tmpdir / "document.txt"
        regular_file.write_text("text")
        assert _is_export_file(regular_file) is False
        
        # Directories should not be detected
        dir_path = tmpdir / "folder"
        dir_path.mkdir()
        assert _is_export_file(dir_path) is False


def test_export_interval_sec():
    """Test export interval configuration."""
    # Default interval
    with patch.dict("os.environ", {}, clear=True):
        interval = export_interval_sec(None)
        assert interval == 3600.0  # Default 1 hour
    
    # Environment variable override
    with patch.dict("os.environ", {"MINION_EXPORT_INTERVAL_SEC": "1800"}):
        interval = export_interval_sec(None)
        assert interval == 1800.0
    
    # Minimum interval enforced
    with patch.dict("os.environ", {"MINION_EXPORT_INTERVAL_SEC": "60"}):
        interval = export_interval_sec(None)
        assert interval == 300.0  # Minimum 5 minutes
    
    # Invalid value falls back to default
    with patch.dict("os.environ", {"MINION_EXPORT_INTERVAL_SEC": "invalid"}):
        interval = export_interval_sec(None)
        assert interval == 3600.0


def test_export_watch_path():
    """Test export watch path configuration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.dict("os.environ", {}, clear=True):
            path = export_watch_path(data_dir)
            assert path == data_dir / "inbox" / "exports"
    
    # Environment variable override
    with patch.dict("os.environ", {"MINION_EXPORT_WATCH_PATH": "/custom/path"}):
        path = export_watch_path(None)
        assert path == Path("/custom/path")
    
    # No data_dir and no env var returns None
    with patch.dict("os.environ", {}, clear=True):
        path = export_watch_path(None)
        assert path is None


def test_export_profile_id():
    """Profile namespace for scheduled export ingests."""
    from settings import save_settings

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.dict("os.environ", {}, clear=True):
            assert export_profile_id(data_dir, None) is None

        with patch.dict("os.environ", {"MINION_EXPORT_PROFILE_ID": "env-profile"}):
            assert export_profile_id(data_dir, None) == "env-profile"

        save_settings(data_dir, {"export_profile_id": "saved-profile"})
        with patch.dict("os.environ", {}, clear=True):
            assert export_profile_id(data_dir, None) == "saved-profile"

        empty_dir = data_dir / "no-settings"
        empty_dir.mkdir()
        mock_conn = Mock()
        with patch.dict("os.environ", {}, clear=True):
            with patch("store.profile_get_active", return_value="active-profile"):
                assert export_profile_id(empty_dir, mock_conn) == "active-profile"


def test_tick_no_watch_path():
    """Test tick when no watch path is configured."""
    result = tick(None, Mock())
    assert result["status"] == "no_watch_path"


def test_tick_disabled():
    """Test tick when scheduler is disabled."""
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        watch_path = data_dir / "inbox" / "exports"
        watch_path.mkdir(parents=True)
        original = os.environ.get("MINION_DISABLE_EXPORT_SCHEDULER")
        os.environ["MINION_DISABLE_EXPORT_SCHEDULER"] = "1"
        try:
            result = tick(data_dir, Mock())
            assert result["status"] == "disabled"
        finally:
            if original is None:
                os.environ.pop("MINION_DISABLE_EXPORT_SCHEDULER", None)
            else:
                os.environ["MINION_DISABLE_EXPORT_SCHEDULER"] = original


def test_tick_no_new_exports():
    """Test tick when no new exports are found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        watch_path = data_dir / "inbox" / "exports"
        watch_path.mkdir(parents=True)
        
        # Mock conn factory
        mock_conn = Mock()
        mock_conn_factory = Mock(return_value=mock_conn)
        
        # Mock _should_ingest_export to return True for new files
        with patch("export_scheduler._should_ingest_export", return_value=True):
            result = tick(data_dir, mock_conn_factory)
            assert result["status"] == "no_new_exports"
            assert result["watched"] == str(watch_path)


@patch("export_scheduler._ingest_export_file")
def test_tick_with_new_exports(mock_ingest):
    """Test tick when new exports are found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        watch_path = data_dir / "inbox" / "exports"
        watch_path.mkdir(parents=True)
        
        # Create a fake export file
        export_file = watch_path / "chatgpt_export.json"
        export_file.write_text('{"conversations": []}')
        
        # Mock conn factory
        mock_conn = Mock()
        mock_conn_factory = Mock(return_value=mock_conn)
        
        # Mock _should_ingest_export to return True for new files
        with patch("export_scheduler._should_ingest_export", return_value=True):
            # Mock successful ingest
            mock_ingest.return_value = {
                "path": str(export_file),
                "success": True,
                "source_id": "test-source-id",
                "chunks": 10,
            }
            
            result = tick(data_dir, mock_conn_factory)
            assert result["status"] == "completed"
            assert result["total"] == 1
            assert result["successful"] == 1
            assert result["failed"] == 0
            assert len(result["results"]) == 1


@patch("export_scheduler._ingest_export_file")
def test_tick_with_failed_ingest(mock_ingest):
    """Test tick when ingest fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        watch_path = data_dir / "inbox" / "exports"
        watch_path.mkdir(parents=True)
        
        # Create a fake export file
        export_file = watch_path / "chatgpt_export.json"
        export_file.write_text('{"conversations": []}')
        
        # Mock conn factory
        mock_conn = Mock()
        mock_conn_factory = Mock(return_value=mock_conn)
        
        # Mock _should_ingest_export to return True for new files
        with patch("export_scheduler._should_ingest_export", return_value=True):
            # Mock failed ingest
            mock_ingest.return_value = {
                "path": str(export_file),
                "success": False,
                "error": "ingest failed",
            }
            
            result = tick(data_dir, mock_conn_factory)
            assert result["status"] == "completed"
            assert result["total"] == 1
            assert result["successful"] == 0
            assert result["failed"] == 1
            assert result["results"][0]["success"] is False


@patch("export_scheduler._ingest_export_file")
def test_trigger_manual_export_specific_file(mock_ingest):
    """Test manual trigger for specific file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        export_file = data_dir / "export.json"
        export_file.write_text('{"conversations": []}')
        
        # Mock conn factory
        mock_conn = Mock()
        mock_conn_factory = Mock(return_value=mock_conn)
        
        # Mock successful ingest
        mock_ingest.return_value = {
            "path": str(export_file),
            "success": True,
            "source_id": "test-source-id",
            "chunks": 10,
        }
        
        result = trigger_manual_export(
            data_dir,
            mock_conn_factory,
            export_path=str(export_file),
        )
        
        assert result["success"] is True
        assert result["path"] == str(export_file)
        assert result["source_id"] == "test-source-id"


def test_trigger_manual_export_nonexistent_file():
    """Test manual trigger with nonexistent file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        mock_conn_factory = Mock(return_value=Mock())
        
        result = trigger_manual_export(
            data_dir,
            mock_conn_factory,
            export_path="/nonexistent/file.json",
        )
        
        assert result["status"] == "error"
        assert "not found" in result["error"]


@patch("export_scheduler.tick")
def test_trigger_manual_export_no_path(mock_tick):
    """Test manual trigger without specific path (uses watch path)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        mock_conn_factory = Mock(return_value=Mock())
        
        mock_tick.return_value = {"status": "no_new_exports"}
        
        result = trigger_manual_export(data_dir, mock_conn_factory, export_path=None)
        
        assert result["status"] == "no_new_exports"
        mock_tick.assert_called_once_with(data_dir, mock_conn_factory)
