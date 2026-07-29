"""
MIKE Memory Janitor - Production Module
Provides TTL/expiry, deduplication, and safe archiving for memory stores.

Integration point: Import into mike_memory_service.py and call:
  - janitor.apply_ttl_policy()        (scheduled hourly)
  - janitor.dedup_chunks()            (before bulk inserts)
  - janitor.archive_expired()         (scheduled daily)
  - janitor.get_storage_stats()       (monitoring endpoint)
"""
import sqlite3, os, json, logging, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TTL_CONFIG: Dict[str, Dict] = {
    "conversations": {
        "ttl_days": 365,        # Keep conversations for 1 year
        "archive_days": 90,     # Archive after 90 days
        "min_records": 100,     # Never expire if below this count
    },
    "knowledge_chunks": {
        "ttl_days": None,       # Knowledge is forever (no TTL)
        "archive_days": 180,    # Archive if unused for 180 days
        "min_records": 1000,
    },
    "embedding_cache": {
        "ttl_days": 30,         # Cache entries expire after 30 days
        "archive_days": 7,      # Archive stale entries after 7 days
        "min_records": 100,
    },
    "session_checkpoints": {
        "ttl_days": 90,         # Keep checkpoints for 90 days
        "archive_days": 30,
        "min_records": 10,
    },
    "autonomy_logs": {
        "ttl_days": 90,         # Keep logs for 90 days
        "archive_days": 90,
        "min_records": 10,
    },
    "mesh_checkpoints": {
        "ttl_days": 90,         # Mesh checkpoints expire after 90 days
        "archive_days": 30,
        "min_records": 1,
    },
    "decisions": {
        "ttl_days": 365,        # Keep decisions for 1 year
        "archive_days": 180,
        "min_records": 5,
    },
}

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedup_knowledge_chunks(db_path: str, dry_run: bool = False) -> Dict:
    """
    Remove duplicate knowledge chunks based on content hash.
    Keeps the oldest entry (lowest ID), archives others.

    Returns: {"found": N, "removed": N, "archived_path": str}
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        SELECT content, COUNT(*) as cnt, MIN(id) as keep_id
        FROM knowledge_chunks
        GROUP BY content
        HAVING cnt > 1
    """)
    dupes = cur.fetchall()

    if not dupes:
        conn.close()
        return {"found": 0, "removed": 0, "archived_path": None}

    total_dup_count = sum(cnt - 1 for _, cnt, _ in dupes)

    if dry_run:
        conn.close()
        return {"found": total_dup_count, "removed": 0, "archived_path": None}

    # Archive
    archive_dir = os.path.join(os.path.dirname(db_path), "archives")
    os.makedirs(archive_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = os.path.join(archive_dir, f"deduped_chunks_{stamp}.jsonl")

    removed = 0
    with open(archive_path, "w", encoding="utf-8") as f:
        for content, cnt, keep_id in dupes:
            cur.execute(
                "SELECT * FROM knowledge_chunks WHERE content = ? AND id != ?",
                (content, keep_id),
            )
            kc_cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                record = dict(zip(kc_cols, row))
                for k, v in record.items():
                    if isinstance(v, bytes):
                        record[k] = v.hex()
                    elif isinstance(v, str) and len(v) > 500:
                        record[k] = v[:500] + "..."
                f.write(json.dumps(record, default=str) + "\n")
                removed += 1

            # Clean up related records
            cur.execute(
                "DELETE FROM knowledge_chunks WHERE content = ? AND id != ?",
                (content, keep_id),
            )
            # Also clean orphan embeddings and mesh links
            cur.execute(
                "SELECT id FROM knowledge_chunks WHERE content = ? AND id != ?",
                (content, keep_id),
            )
            for (dup_id,) in cur.fetchall():
                cur.execute("DELETE FROM knowledge_embeddings WHERE chunk_id = ?", (dup_id,))
                cur.execute(
                    "DELETE FROM memory_mesh WHERE source_id = ? OR target_id = ?",
                    (dup_id, dup_id),
                )

    conn.commit()
    conn.close()

    logger.info("Dedup: removed %d duplicate knowledge chunks", removed)
    return {"found": total_dup_count, "removed": removed, "archived_path": archive_path}


def dedup_embedding_cache(db_path: str, dry_run: bool = False) -> Dict:
    """Remove duplicate cache entries (same cache_key."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        SELECT cache_key, COUNT(*)
        FROM embedding_cache
        GROUP BY cache_key
        HAVING COUNT(*) > 1
    """)
    dupe_keys = cur.fetchall()

    if not dupe_keys:
        conn.close()
        return {"found": 0, "removed": 0}

    total = sum(cnt - 1 for _, cnt in dupe_keys)

    if dry_run:
        conn.close()
        return {"found": total, "removed": 0}

    for cache_key, _ in dupe_keys:
        cur.execute(
            "DELETE FROM embedding_cache WHERE cache_key = ? AND rowid NOT IN ("
            "SELECT MIN(rowid) FROM embedding_cache WHERE cache_key = ?)",
            (cache_key, cache_key),
        )

    conn.commit()
    conn.close()
    logger.info("Dedup: removed %d duplicate cache entries", total)
    return {"found": total, "removed": total}


# ---------------------------------------------------------------------------
# TTL / Expiry
# ---------------------------------------------------------------------------

def apply_ttl_policy(
    db_path: str,
    table: str,
    ts_column: str,
    ttl_days: int,
    min_records: int = 100,
    dry_run: bool = False,
) -> Dict:
    """
    Archive records older than TTL, preserving at least min_records.
    NEVER deletes - moves to archive directory.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) FROM [{table}]")
    total = cur.fetchone()[0]

    if total <= min_records:
        conn.close()
        return {"found": 0, "archived": 0, "reason": f"Below min_records ({total} <= {min_records})"}

    cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
    cur.execute(
        f"SELECT COUNT(*) FROM [{table}] WHERE {ts_column} < ?", (cutoff,)
    )
    expired = cur.fetchone()[0]

    remaining = total - expired
    if remaining < min_records:
        # Only archive enough to keep min_records
        expired = max(0, total - min_records)

    if expired == 0:
        conn.close()
        return {"found": 0, "archived": 0, "reason": "No expired records"}

    if dry_run:
        conn.close()
        return {"found": expired, "archived": 0, "reason": "dry_run"}

    # Archive
    archive_dir = os.path.join(os.path.dirname(db_path), "archives")
    os.makedirs(archive_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = os.path.join(archive_dir, f"expired_{table}_{stamp}.jsonl")

    cur.execute(f"SELECT * FROM [{table}] WHERE {ts_column} < ?", (cutoff,))
    cols = [d[0] for d in cur.description]
    archived = 0

    with open(archive_path, "w", encoding="utf-8") as f:
        for row in cur.fetchall():
            record = dict(zip(cols, row))
            for k, v in record.items():
                if isinstance(v, bytes):
                    record[k] = v.hex()
            f.write(json.dumps(record, default=str) + "\n")
            archived += 1

    # Only remove if archive was successful
    if archived > 0:
        cur.execute(f"DELETE FROM [{table}] WHERE {ts_column} < ?", (cutoff,))
        conn.commit()

    conn.close()
    logger.info("TTL: archived %d records from %s", archived, table)
    return {"found": expired, "archived": archived, "archive_path": archive_path}


# ---------------------------------------------------------------------------
# Storage Statistics
# ---------------------------------------------------------------------------

def get_storage_stats(db_path: str) -> Dict:
    """Get comprehensive storage statistics for monitoring."""
    stats = {}
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Table sizes
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    for table in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{table}]")
            count = cur.fetchone()[0]
            stats[f"table_{table}_rows"] = count
        except Exception:
            pass  # Table may not exist or be inaccessible

    # Fragmentation
    cur.execute("PRAGMA freelist_count")
    free = cur.fetchone()[0]
    cur.execute("PRAGMA page_size")
    ps = cur.fetchone()[0]
    cur.execute("PRAGMA page_count")
    pc = cur.fetchone()[0]
    stats["fragmentation_pages"] = free
    stats["fragmentation_kb"] = (free * ps) / 1024
    stats["total_size_kb"] = (pc * ps) / 1024
    stats["fragmentation_pct"] = (free / pc * 100) if pc > 0 else 0

    # File size
    stats["file_size_kb"] = os.path.getsize(db_path) / 1024

    conn.close()
    return stats


def vacuum_if_needed(db_path: str, threshold_pct: float = 10.0) -> Dict:
    """VACUUM database if fragmentation exceeds threshold."""
    stats = get_storage_stats(db_path)

    if stats["fragmentation_pct"] < threshold_pct:
        return {"action": "skip", "fragmentation_pct": stats["fragmentation_pct"]}

    size_before = stats["file_size_kb"]
    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.close()
    size_after = os.path.getsize(db_path) / 1024

    saved = size_before - size_after
    logger.info("VACUUM: saved %.1fKB (fragmentation was %.1f%%)", saved, stats["fragmentation_pct"])
    return {"action": "vacuumed", "saved_kb": saved, "fragmentation_pct": stats["fragmentation_pct"]}


# ---------------------------------------------------------------------------
# Scheduled maintenance
# ---------------------------------------------------------------------------

def run_maintenance(db_path: str, dry_run: bool = False) -> Dict:
    """Run all janitor maintenance tasks. Call this on a schedule."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "actions": [],
    }

    # 1. Dedup knowledge chunks
    try:
        r = dedup_knowledge_chunks(db_path, dry_run)
        results["dedup_chunks"] = r
        if r["removed"]:
            results["actions"].append(f"dedup_chunks: removed {r['removed']} duplicates")
    except Exception as e:
        results["dedup_chunks"] = {"error": str(e)}

    # 2. Dedup embedding cache
    try:
        r = dedup_embedding_cache(db_path, dry_run)
        results["dedup_cache"] = r
        if r.get("removed"):
            results["actions"].append(f"dedup_cache: removed {r['removed']} duplicates")
    except Exception as e:
        results["dedup_cache"] = {"error": str(e)}

    # 3. TTL cleanup for embedding cache (30 days)
    try:
        r = apply_ttl_policy(db_path, "embedding_cache", "created_at", 30, 100, dry_run)
        results["ttl_embedding_cache"] = r
        if r.get("archived"):
            results["actions"].append(f"ttl_cache: archived {r['archived']} entries")
    except Exception as e:
        results["ttl_embedding_cache"] = {"error": str(e)}

    # 4. Vacuum if needed
    try:
        r = vacuum_if_needed(db_path, threshold_pct=15.0)
        results["vacuum"] = r
        if r["action"] == "vacuumed":
            results["actions"].append(f"vacuum: saved {r['saved_kb']:.0f}KB")
    except Exception as e:
        results["vacuum"] = {"error": str(e)}

    # 5. Storage stats
    try:
        results["storage_stats"] = get_storage_stats(db_path)
    except Exception as e:
        results["storage_stats"] = {"error": str(e)}

    return results


# ---------------------------------------------------------------------------
# CLI entry point for manual runs
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MIKE Memory Janitor")
    parser.add_argument("--db", default=r"C:\Users\Admin\Desktop\mike\runtime\memory\mike_memory.db",
                        help="Path to memory database")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--stats", action="store_true", help="Print storage stats only")
    parser.add_argument("--vacuum", action="store_true", help="Vacuum only")
    parser.add_argument("--dedup", action="store_true", help="Dedup only")
    parser.add_argument("--ttl", action="store_true", help="TTL cleanup only")
    args = parser.parse_args()

    if args.stats:
        s = get_storage_stats(args.db)
        print(json.dumps(s, indent=2, default=str))
    elif args.vacuum:
        r = vacuum_if_needed(args.db, threshold_pct=0.0)
        print(json.dumps(r, indent=2, default=str))
    elif args.dedup:
        r1 = dedup_knowledge_chunks(args.db, args.dry_run)
        r2 = dedup_embedding_cache(args.db, args.dry_run)
        print(json.dumps({"chunks": r1, "cache": r2}, indent=2, default=str))
    elif args.ttl:
        r = apply_ttl_policy(args.db, "embedding_cache", "created_at", 30, 100, args.dry_run)
        print(json.dumps(r, indent=2, default=str))
    else:
        r = run_maintenance(args.db, args.dry_run)
        print(json.dumps(r, indent=2, default=str))
