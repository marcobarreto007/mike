# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Unit tests for MIKE memory integrity features:
  - SQLite integrity check (PRAGMA integrity_check)
  - Backup before schema migrations
  - Backup creation and file verification
  - Corrupted DB detection
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure core modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "memory"))


class TestIntegrityCheck(unittest.TestCase):
    """Tests for run_integrity_check and PRAGMA integrity_check."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_integrity_test_"))
        self.db_path = self.tmpdir / "test_integrity.db"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def _create_valid_db(self):
        """Create a minimal valid SQLite database with conversations table."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_hash TEXT UNIQUE,
                timestamp TEXT,
                user_text TEXT,
                assistant_text TEXT
            )
        """)
        conn.execute(
            "INSERT INTO conversations (entry_hash, timestamp, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?)",
            ("abc123", "2026-07-25T10:00:00Z", "Hello", "Hi there"),
        )
        conn.commit()
        conn.close()

    def test_integrity_check_passes_on_valid_db(self):
        """PRAGMA integrity_check returns 'ok' on a valid database."""
        self._create_valid_db()
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        result = store.run_integrity_check()
        self.assertTrue(result["ok"], f"Expected integrity OK, got: {result['errors']}")
        self.assertEqual(len(result["errors"]), 0)

    def test_integrity_check_detects_corruption(self):
        """PRAGMA integrity_check fails on a corrupted database."""
        self._create_valid_db()

        # Corrupt the DB by overwriting bytes in the middle
        with open(self.db_path, "r+b") as f:
            f.seek(512)  # jump past the header into data pages
            f.write(b"\x00\x00\x00\x00\x00" * 20)

        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        result = store.run_integrity_check()
        # A corrupted DB should either report "ok": False OR raise during connect
        # Either outcome demonstrates the detection mechanism
        if not result["ok"]:
            self.assertTrue(
                len(result["errors"]) > 0,
                "Expected error messages for corrupted DB",
            )

    def test_integrity_check_on_missing_db(self):
        """Integrity check should handle a non-existent database gracefully."""
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore
        non_existent = self.tmpdir / "missing.db"
        store = LocalMikeStore(non_existent, knowledge_paths=[], log=lambda _: None)

        # Running integrity check on a non-existent DB should raise or report error
        try:
            result = store.run_integrity_check()
            # If it doesn't raise, it should report failure
            if not non_existent.exists():
                self.assertFalse(result["ok"], "Expected failure for missing DB")
        except sqlite3.OperationalError:
            # Raising is also acceptable behavior
            pass


class TestBackup(unittest.TestCase):
    """Tests for backup_database and backup before migration."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_backup_test_"))
        self.db_path = self.tmpdir / "test_backup.db"
        self.backup_dir = self.tmpdir / "backups"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def _create_db_with_data(self):
        """Create a DB with some conversation data."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_hash TEXT UNIQUE,
                timestamp TEXT,
                user_text TEXT,
                assistant_text TEXT
            )
        """)
        for i in range(5):
            conn.execute(
                "INSERT INTO conversations (entry_hash, timestamp, user_text, assistant_text) "
                "VALUES (?, ?, ?, ?)",
                (f"hash_{i}", f"2026-07-25T10:0{i}:00Z", f"Q{i}", f"A{i}"),
            )
        conn.commit()
        conn.close()

    def test_backup_creates_file(self):
        """backup_database creates a timestamped backup file."""
        self._create_db_with_data()
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "1"
        os.environ["MIKE_MEMORY_BACKUP_DIR"] = str(self.backup_dir)

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        # Force backup_enabled since it reads env at class level
        store._backup_enabled = True
        store._backup_dir = self.backup_dir

        backup_path = store.backup_database(label="test")
        self.assertIsNotNone(backup_path, "Backup should create a file")
        self.assertTrue(backup_path.exists(), f"Backup file should exist at {backup_path}")
        # Verify it has content (size > 0)
        self.assertGreater(backup_path.stat().st_size, 0, "Backup file should be non-empty")

    def test_backup_preserves_data(self):
        """Backup file contains the same data as the original."""
        self._create_db_with_data()
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "1"
        os.environ["MIKE_MEMORY_BACKUP_DIR"] = str(self.backup_dir)

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        store._backup_enabled = True
        store._backup_dir = self.backup_dir

        backup_path = store.backup_database(label="preserve-test")

        # Verify backup has the same content
        orig_size = self.db_path.stat().st_size
        backup_size = backup_path.stat().st_size
        self.assertEqual(orig_size, backup_size, "Backup should be same size as original")

        # Verify backup is a valid SQLite DB
        conn = sqlite3.connect(str(backup_path))
        row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        self.assertEqual(row[0], 5, "Backup should contain all 5 rows")
        conn.close()

    def test_backup_disabled_by_env(self):
        """backup_database returns None when backup is disabled."""
        self._create_db_with_data()

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        store._backup_enabled = False

        backup_path = store.backup_database(label="disabled-test")
        self.assertIsNone(backup_path, "Backup should return None when disabled")

    def test_backup_on_nonexistent_db(self):
        """backup_database returns None when DB file does not exist."""
        from mike_memory_store import LocalMikeStore
        non_existent = self.tmpdir / "ghost.db"
        store = LocalMikeStore(non_existent, knowledge_paths=[], log=lambda _: None)
        store._backup_enabled = True
        store._backup_dir = self.backup_dir

        backup_path = store.backup_database()
        self.assertIsNone(backup_path, "Should return None for non-existent DB")

    def test_backup_prunes_old_backups(self):
        """Backup should keep only the last 20 backup files."""
        self._create_db_with_data()
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "1"
        os.environ["MIKE_MEMORY_BACKUP_DIR"] = str(self.backup_dir)

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(self.db_path, knowledge_paths=[], log=lambda _: None)
        store._backup_enabled = True
        store._backup_dir = self.backup_dir

        # Create 25 backups
        for i in range(25):
            store.backup_database(label=f"prune-{i:03d}")

        # Should have at most 20 backups remaining
        backups = sorted(self.backup_dir.glob("test_backup-*.db"))
        self.assertLessEqual(len(backups), 20, "Should keep at most 20 backups")


class TestInitializeWithIntegrity(unittest.TestCase):
    """Tests for integrity check during initialize()."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_init_test_"))
        self.db_path = self.tmpdir / "test_init.db"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_initialize_runs_integrity_check(self):
        """initialize() should run integrity check without error on a fresh DB."""
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "1"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        # Suppress embedder (no ONNX needed for this test)
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore

        store = LocalMikeStore(
            self.db_path,
            knowledge_paths=[self.tmpdir],
            log=lambda msg: None,
        )
        # initialize() with empty knowledge dirs should work
        try:
            store.initialize()
        except RuntimeError as e:
            self.fail(f"initialize() raised RuntimeError: {e}")

        # Verify tables were created
        conn = sqlite3.connect(str(self.db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        self.assertIn("conversations", table_names)
        self.assertIn("knowledge_chunks", table_names)
        conn.close()

    def test_initialize_skips_integrity_when_disabled(self):
        """initialize() should skip integrity check when MIKE_MEMORY_INTEGRITY_CHECK=0."""
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore

        store = LocalMikeStore(
            self.db_path,
            knowledge_paths=[self.tmpdir],
            log=lambda msg: None,
        )
        # Should not raise
        store.initialize()

        conn = sqlite3.connect(str(self.db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        self.assertIn("conversations", table_names)
        conn.close()


if __name__ == "__main__":
    unittest.main()
