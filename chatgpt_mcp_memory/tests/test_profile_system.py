"""Tests for profile system."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import sqlite3

from store import (
    Profile,
    profile_create,
    profile_delete,
    profile_get,
    profile_get_active,
    profile_get_default,
    profile_initialize_defaults,
    profile_list,
    profile_set_active,
    profile_update,
)


def test_profile_create():
    """Test creating a new profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile = profile_create(
            conn,
            profile_id="test-profile",
            name="Test Profile",
            kind="custom",
            is_default=False,
        )
        
        assert profile.profile_id == "test-profile"
        assert profile.name == "Test Profile"
        assert profile.kind == "custom"
        assert profile.is_default is False
        assert profile.created_at > 0
        assert profile.updated_at > 0


def test_profile_create_default():
    """Test creating a default profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile = profile_create(
            conn,
            profile_id="default-profile",
            name="Default",
            kind="default",
            is_default=True,
        )
        
        assert profile.is_default is True
        
        # Verify it's the only default
        default = profile_get_default(conn)
        assert default is not None
        assert default.profile_id == "default-profile"


def test_profile_list():
    """Test listing profiles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        # Create multiple profiles
        profile_create(conn, "profile-1", "Profile 1", "custom", False)
        profile_create(conn, "profile-2", "Profile 2", "custom", True)
        
        profiles = profile_list(conn)
        assert len(profiles) == 2
        assert profiles[0].is_default is True  # Default should be first
        assert profiles[1].is_default is False


def test_profile_get():
    """Test getting a specific profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile_create(conn, "test-profile", "Test Profile", "custom", False)
        
        profile = profile_get(conn, "test-profile")
        assert profile is not None
        assert profile.profile_id == "test-profile"
        assert profile.name == "Test Profile"
        
        # Test non-existent profile
        profile = profile_get(conn, "non-existent")
        assert profile is None


def test_profile_update():
    """Test updating a profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile_create(conn, "test-profile", "Test Profile", "custom", False)
        
        # Update name
        updated = profile_update(conn, "test-profile", name="Updated Name")
        assert updated.name == "Updated Name"
        
        # Update to default
        updated = profile_update(conn, "test-profile", is_default=True)
        assert updated.is_default is True
        
        # Verify old default is unset
        default = profile_get_default(conn)
        assert default.profile_id == "test-profile"


def test_profile_delete():
    """Test deleting a profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL, _apply_schema_upgrades
        conn.executescript(_SCHEMA_SQL)
        _apply_schema_upgrades(conn)  # Add profile_id columns
        
        profile_create(conn, "test-profile", "Test Profile", "custom", False)
        
        # Delete profile
        result = profile_delete(conn, "test-profile")
        assert result is True
        
        # Verify it's gone
        profile = profile_get(conn, "test-profile")
        assert profile is None


def test_profile_delete_default():
    """Test that default profile cannot be deleted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile_create(conn, "default-profile", "Default", "default", True)
        
        # Try to delete default profile
        with pytest.raises(ValueError, match="Cannot delete default profile"):
            profile_delete(conn, "default-profile")


def test_profile_set_active():
    """Test setting active profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile_create(conn, "test-profile", "Test Profile", "custom", False)
        
        # Set active
        profile_set_active(conn, "test-profile")
        
        # Verify active
        active_id = profile_get_active(conn)
        assert active_id == "test-profile"


def test_profile_get_active():
    """Test getting active profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        profile_create(conn, "test-profile", "Test Profile", "custom", False)
        profile_set_active(conn, "test-profile")
        
        active_id = profile_get_active(conn)
        assert active_id == "test-profile"
        
        # Test fallback to default when no active profile
        conn.execute("DELETE FROM meta WHERE key = 'active_profile_id'")
        profile_create(conn, "default-profile", "Default", "default", True)
        
        active_id = profile_get_active(conn)
        assert active_id == "default-profile"


def test_profile_initialize_defaults():
    """Test initializing default profiles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Initialize schema
        from store import _SCHEMA_SQL
        conn.executescript(_SCHEMA_SQL)
        
        # Initialize defaults
        profile_initialize_defaults(conn)
        
        profiles = profile_list(conn)
        assert len(profiles) == 2
        
        profile_ids = {p.profile_id for p in profiles}
        assert "default" in profile_ids
        assert "personal" in profile_ids
        
        # Verify default profile is default
        default = profile_get_default(conn)
        assert default.profile_id == "default"
        
        # Verify active profile is set to default
        active_id = profile_get_active(conn)
        assert active_id == "default"
        
        # Test idempotency - running again should not create duplicates
        profile_initialize_defaults(conn)
        profiles = profile_list(conn)
        assert len(profiles) == 2


def test_profile_consent_policy():
    """Test profile-specific consent policy loading."""
    from consent_policy import load_policy, load_policy_for_profile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        
        # Test default policy has profiles
        policy = load_policy(data_dir)
        assert "profiles" in policy
        assert "default" in policy["profiles"]
        assert "personal" in policy["profiles"]
        
        # Test loading policy for specific profile
        default_policy = load_policy_for_profile(data_dir, "default")
        assert "readers" in default_policy
        
        personal_policy = load_policy_for_profile(data_dir, "personal")
        assert "readers" in personal_policy
        
        # Verify personal profile has stricter MCP consent
        default_mcp = default_policy["readers"]["mcp"]
        personal_mcp = personal_policy["readers"]["mcp"]
        
        # Personal should have lower max_release_level
        assert personal_mcp["max_release_level"] < default_mcp["max_release_level"]
        
        # Personal should not allow screen context tools
        assert personal_mcp["allow_screen_context_tools"] is False
        assert default_mcp["allow_screen_context_tools"] is True


def test_screen_tools_respects_active_profile():
    """Personal profile blocks screen context tools; default allows them."""
    from consent_policy import screen_tools_allowed_for_mcp

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        assert screen_tools_allowed_for_mcp(data_dir, profile_id="default") is True
        assert screen_tools_allowed_for_mcp(data_dir, profile_id="personal") is False


def test_profile_consent_policy_fallback():
    """Test fallback to top-level readers for unknown profile."""
    from consent_policy import load_policy_for_profile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        
        # Test unknown profile falls back to top-level readers
        unknown_policy = load_policy_for_profile(data_dir, "unknown-profile")
        assert "readers" in unknown_policy
        assert "mcp" in unknown_policy["readers"]


def test_profile_scoped_counts():
    """Sources and chunks counts respect profile_id filters."""
    from store import _apply_schema_upgrades, _new_id, connect, count_chunks, count_sources

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = connect(db_path)
        _apply_schema_upgrades(conn)
        profile_initialize_defaults(conn)

        for profile_id, path in (("default", "/tmp/default-doc.txt"), ("personal", "/tmp/personal-doc.txt")):
            sid = _new_id("src")
            cid = _new_id("chk")
            conn.execute(
                "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at, profile_id) "
                "VALUES (?, ?, 'text', ?, 1.0, 10, 'text', '{}', 1.0, ?)",
                (sid, path, f"hash-{profile_id}", profile_id),
            )
            conn.execute(
                "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json, profile_id) "
                "VALUES (?, ?, 0, 'user', ?, '{}', ?)",
                (cid, sid, f"{profile_id} chunk", profile_id),
            )
        conn.commit()

        assert count_sources(conn) == 2
        assert count_chunks(conn) == 2
        assert count_sources(conn, profile_id="default") == 1
        assert count_chunks(conn, profile_id="default") == 1
        assert count_sources(conn, profile_id="personal") == 1
        assert count_chunks(conn, profile_id="personal") == 1

        conn.close()


def test_source_id_default_profile_backward_compat():
    from store import source_id_for

    path = "/inbox/shared-note.md"
    assert source_id_for(path) == source_id_for(path, "default")
    assert source_id_for(path, "personal") != source_id_for(path, "default")


def test_upsert_same_path_isolated_by_profile():
    """Same logical path in two profiles is two sources; search stays scoped."""
    import numpy as np

    from store import (
        _apply_schema_upgrades,
        connect,
        count_sources,
        profile_initialize_defaults,
        seed_sync_sources,
        source_id_for,
        keyword_search,
        upsert_source,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = connect(db_path)
        seed_sync_sources(conn)
        _apply_schema_upgrades(conn)
        profile_initialize_defaults(conn)

        path = "/inbox/shared-note.md"
        emb = np.ones((1, 768), dtype=np.float32)
        upsert_source(
            conn,
            path=path,
            kind="text",
            sha256="hash-default",
            mtime=1.0,
            bytes_=10,
            parser="text",
            source_meta={},
            chunks=[("default-only secret phrase xyzzy", None, {})],
            embeddings=emb,
            profile_id="default",
        )
        upsert_source(
            conn,
            path=path,
            kind="text",
            sha256="hash-personal",
            mtime=1.0,
            bytes_=10,
            parser="text",
            source_meta={},
            chunks=[("personal-only secret phrase plugh", None, {})],
            embeddings=emb,
            profile_id="personal",
        )

        assert source_id_for(path, "default") != source_id_for(path, "personal")
        assert count_sources(conn, profile_id="default") == 1
        assert count_sources(conn, profile_id="personal") == 1

        default_hits = keyword_search(conn, "xyzzy", top_k=5, profile_id="default")
        personal_hits = keyword_search(conn, "xyzzy", top_k=5, profile_id="personal")
        assert len(default_hits) == 1
        assert len(personal_hits) == 0

        conn.close()
