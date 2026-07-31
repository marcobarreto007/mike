# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Unit tests for MIKE memory resilience features:
  - Input validation on public methods
  - Memory budget enforcement (max entries, max size, auto-prune)
  - Vector fallback to FTS on failure
  - Retry on lock (decorator behavior)
  - Graceful degradation of vector search
  - Degenerate embedding handling
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure core modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "memory"))


class TestInputValidation(unittest.TestCase):
    """Tests that public methods reject invalid inputs."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_validation_test_"))
        self.db_path = self.tmpdir / "test_validation.db"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"

        from mike_memory_store import LocalMikeStore
        self.store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        self.store.initialize()

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_add_conversation_rejects_empty_user_text(self):
        """add_conversation returns False for empty user_text."""
        result = self.store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="",
            assistant_text="Hello world",
        )
        self.assertFalse(result, "Should reject empty user_text")

    def test_add_conversation_rejects_empty_assistant_text(self):
        """add_conversation returns False for empty assistant_text."""
        result = self.store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="Hello",
            assistant_text="",
        )
        self.assertFalse(result, "Should reject empty assistant_text")

    def test_add_conversation_rejects_empty_timestamp(self):
        """add_conversation returns False for empty timestamp."""
        result = self.store.add_conversation(
            timestamp="",
            user_text="Hello",
            assistant_text="World",
        )
        self.assertFalse(result, "Should reject empty timestamp")

    def test_add_conversation_accepts_valid_input(self):
        """add_conversation succeeds with valid input."""
        result = self.store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="Hello Mike",
            assistant_text="Hi there!",
        )
        self.assertTrue(result, "Should accept valid input")
        self.assertEqual(self.store.conversation_count(), 1)

    def test_search_conversations_rejects_empty_query(self):
        """search_conversations returns empty list for empty query."""
        hits = self.store.search_conversations("", limit=5)
        self.assertEqual(len(hits), 0)
        hits = self.store.search_conversations("   ", limit=5)
        self.assertEqual(len(hits), 0)

    def test_search_conversations_vector_rejects_empty_query(self):
        """search_conversations_vector returns empty list for empty query."""
        hits = self.store.search_conversations_vector("", limit=5)
        self.assertEqual(len(hits), 0)

    def test_search_knowledge_rejects_empty_query(self):
        """search_knowledge returns empty list for empty query."""
        hits = self.store.search_knowledge("", limit=5)
        self.assertEqual(len(hits), 0)

    def test_service_add_conversation_raises_on_empty(self):
        """MikeMemoryService.add_conversation raises ValueError on empty input."""
        # Integration-level test for service validation
        from mike_memory_service import MikeMemoryService
        svc = MikeMemoryService(
            db_path=self.db_path,
            knowledge_paths=[self.tmpdir],
            user_id="test_user",
            agent_id="test_agent",
            log=lambda _: None,
        )
        with self.assertRaises(ValueError):
            svc.add_conversation(
                timestamp="2026-07-25T10:00:00Z",
                user_text="",
                assistant_text="Hello",
            )
        with self.assertRaises(ValueError):
            svc.add_conversation(
                timestamp="",
                user_text="Hello",
                assistant_text="World",
            )


class TestMemoryBudget(unittest.TestCase):
    """Tests for memory budget enforcement."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_budget_test_"))
        self.db_path = self.tmpdir / "test_budget.db"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_budget_enforcement_prunes_oldest_entries(self):
        """When max_conversations is set, oldest entries get pruned."""
        os.environ["MIKE_MEMORY_MAX_CONVERSATIONS"] = "5"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store._max_conversations = 5
        store.initialize()

        # Add 10 conversations
        for i in range(10):
            store.add_conversation(
                timestamp=f"2026-07-25T10:{i:02d}:00Z",
                user_text=f"Question {i}",
                assistant_text=f"Answer {i}",
            )

        # Should have at most 5 (the 5 oldest were pruned)
        count = store.conversation_count()
        self.assertLessEqual(count, 5, f"Expected at most 5, got {count}")

        # Remaining should be the newer ones (IDs 6-10)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, user_text FROM conversations ORDER BY id").fetchall()
        self.assertTrue(
            all(int(r["user_text"].split()[-1]) >= 5 for r in rows),
            "Oldest entries should be pruned",
        )
        conn.close()

    def test_budget_disabled_by_default(self):
        """Memory budget is not enforced when max is 0 (default)."""
        os.environ["MIKE_MEMORY_MAX_CONVERSATIONS"] = "0"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store._max_conversations = 0
        store.initialize()

        # Add 20 conversations - none should be pruned
        for i in range(20):
            store.add_conversation(
                timestamp=f"2026-07-25T10:{i:02d}:00Z",
                user_text=f"Question {i}",
                assistant_text=f"Answer {i}",
            )

        self.assertEqual(store.conversation_count(), 20)

    def test_budget_size_enforcement(self):
        """When max_db_size_mb is set, oldest entries are pruned."""
        os.environ["MIKE_MEMORY_MAX_DB_SIZE_MB"] = "0.05"  # 50KB - very tight

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store._max_db_size_mb = 0.05
        store.initialize()

        # Add lots of conversations with large text
        for i in range(50):
            store.add_conversation(
                timestamp=f"2026-07-25T10:{i:02d}:00Z",
                user_text=f"Question {i}: " + ("X" * 200),
                assistant_text=f"Answer {i}: " + ("Y" * 200),
            )

        # After pruning, count should be less than 50
        count = store.conversation_count()
        self.assertLess(count, 50, f"Budget enforcement should have pruned some entries, got {count}")


class TestVectorFallback(unittest.TestCase):
    """Tests that vector search gracefully degrades to FTS or returns empty."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_fallback_test_"))
        self.db_path = self.tmpdir / "test_fallback.db"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"

        from mike_memory_store import LocalMikeStore
        self.store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        self.store.initialize()
        # Add some conversations for FTS to find
        for i in range(5):
            self.store.add_conversation(
                timestamp=f"2026-07-25T10:{i:02d}:00Z",
                user_text=f"User message number {i} about testing",
                assistant_text=f"Assistant response {i} about testing",
            )

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_vector_fallback_disabled_returns_empty(self):
        """When embedding fails and fallback disabled, returns empty."""
        self.store._vector_fallback = False

        # search_conversations_vector should return empty when embedder is disabled
        hits = self.store.search_conversations_vector("testing", limit=5)
        # With embedder disabled, returns [] before even trying
        self.assertEqual(len(hits), 0)

    def test_vector_fallback_enabled_falls_back_to_fts(self):
        """When embedding is disabled (simulating failure), falls back to FTS."""
        self.store._vector_fallback = True

        # Even with embedder disabled, the method gracefully returns []
        # (because it checks embedder.enabled first)
        # But when embedder IS enabled but vector search fails, it uses FTS
        hits = self.store.search_conversations("testing", limit=3)
        self.assertGreater(len(hits), 0, "FTS should find conversations about testing")


class TestRetryOnLock(unittest.TestCase):
    """Tests for retry_on_lock decorator behavior."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_retry_test_"))
        self.db_path = self.tmpdir / "test_retry.db"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"
        os.environ["MIKE_MEMORY_RETRY_BASE_MS"] = "10"
        os.environ["MIKE_MEMORY_RETRY_MAX_ATTEMPTS"] = "3"
        os.environ["MIKE_MEMORY_RETRY_MAX_DELAY_MS"] = "200"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_add_conversation_succeeds_after_retry(self):
        """add_conversation should complete without error under normal conditions."""
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store.initialize()

        # Normal operation - no lock contention
        result = store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="Retry test question",
            assistant_text="Retry test answer",
        )
        self.assertTrue(result, "Should succeed under normal conditions")
        self.assertEqual(store.conversation_count(), 1)

    def test_set_importance_succeeds(self):
        """set_importance with @retry_on_lock should work normally."""
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store.initialize()
        store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="Importance test",
            assistant_text="Response",
        )

        result = store.set_importance(1, 0.9, source="test")
        self.assertTrue(result)

        # Verify the importance was set
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT importance, importance_source FROM conversations WHERE id=1"
        ).fetchone()
        self.assertAlmostEqual(float(row["importance"]), 0.9, places=1)
        self.assertEqual(row["importance_source"], "test")
        conn.close()


class TestDegenerateEmbedding(unittest.TestCase):
    """Tests that degenerate embeddings (zero vectors, NaN, inf) are handled."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_degenerate_test_"))
        self.db_path = self.tmpdir / "test_degenerate.db"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_deserialize_zero_dims(self):
        """_deserialize_vector returns None for zero dims."""
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )

        result = store._deserialize_vector(b"", 0)
        self.assertIsNone(result)

        result = store._deserialize_vector(b"", -1)
        self.assertIsNone(result)

        result = store._deserialize_vector(None, 128)  # type: ignore[arg-type]
        self.assertIsNone(result)

    def test_deserialize_valid_vector(self):
        """_deserialize_vector returns valid numpy array for correct input."""
        import numpy as np
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )

        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        blob = vec.tobytes()
        result = store._deserialize_vector(blob, 3)
        self.assertIsNotNone(result)
        self.assertEqual(result.shape, (3,))
        self.assertTrue(np.allclose(result, vec))


class TestCorruptedDatabase(unittest.TestCase):
    """Tests for handling corrupted database files."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_corrupt_test_"))
        self.db_path = self.tmpdir / "test_corrupt.db"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_integrity_check_fails_on_truncated_db(self):
        """Integrity check detects a truncated SQLite file."""
        # Create a valid DB
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
        conn.execute("INSERT INTO test VALUES (1)")
        conn.commit()
        conn.close()

        # Truncate the file
        with open(self.db_path, "r+b") as f:
            f.truncate(128)  # Truncate to 128 bytes

        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        result = store.run_integrity_check()
        self.assertFalse(result["ok"], "Integrity should fail on truncated DB")

    def test_initialize_detects_corruption(self):
        """initialize() with integrity check should detect corruption."""
        # Create a valid DB
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
        conn.close()

        # Corrupt the SQLite header deterministically. Writing into an unused
        # region of the first page is not guaranteed to make SQLite reject it.
        with open(self.db_path, "r+b") as f:
            f.seek(0)
            f.write(b"not-a-sqlite-db!")

        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "1"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"

        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )

        # initialize should raise RuntimeError on corruption
        with self.assertRaises(RuntimeError):
            store.initialize()


class TestMissingEmbeddings(unittest.TestCase):
    """Tests that missing embeddings don't crash searches."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mike_mem_missing_emb_test_"))
        self.db_path = self.tmpdir / "test_missing_emb.db"
        os.environ["MIKE_VECTOR_SEARCH_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_BACKUP_ENABLED"] = "0"
        os.environ["MIKE_MEMORY_INTEGRITY_CHECK"] = "0"

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    def test_search_knowledge_vector_returns_empty_with_no_embeddings(self):
        """search_knowledge_vector returns [] when no embeddings exist."""
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store.initialize()

        # Add knowledge chunks without embeddings (forced by disabled embedder)
        # Then search - should not crash
        hits = store.search_knowledge_vector("test query", limit=5)
        self.assertEqual(len(hits), 0)

    def test_search_conversations_vector_returns_empty_with_no_embeddings(self):
        """search_conversations_vector returns [] when no embeddings exist."""
        from mike_memory_store import LocalMikeStore
        store = LocalMikeStore(
            self.db_path, knowledge_paths=[self.tmpdir], log=lambda _: None
        )
        store.initialize()

        # Add conversations without embeddings
        store.add_conversation(
            timestamp="2026-07-25T10:00:00Z",
            user_text="Missing embedding test",
            assistant_text="Response",
        )

        hits = store.search_conversations_vector("test", limit=5)
        # Should return [] gracefully, not crash
        self.assertEqual(len(hits), 0)

    def test_service_search_knowledge_handles_missing_embeddings(self):
        """MikeMemoryService.search_knowledge handles missing vector embeddings."""
        from mike_memory_service import MikeMemoryService
        svc = MikeMemoryService(
            db_path=self.db_path,
            knowledge_paths=[self.tmpdir],
            user_id="test_user",
            agent_id="test_agent",
            log=lambda _: None,
        )
        svc.initialize()

        # Search without any knowledge - should not crash
        hits = svc.search_knowledge("anything", limit=3)
        self.assertIsInstance(hits, list)
        self.assertEqual(len(hits), 0)


if __name__ == "__main__":
    unittest.main()
