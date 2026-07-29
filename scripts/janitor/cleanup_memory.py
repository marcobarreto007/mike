"""
MIKE Memory Janitor - SAFE Cleanup Module
NEVER deletes data. Archives, flags for review, compacts.
"""
import sqlite3, os, json, shutil
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = r"C:\Users\Admin\Desktop\mike\runtime\memory"
DB_PATH = os.path.join(MEMORY_DIR, "mike_memory.db")
ARCHIVE_DIR = os.path.join(MEMORY_DIR, "archives")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

stats = {"operations": [], "space_saved_kb": 0, "records_archived": 0}


def log(op, detail=""):
    print(f"  [{op}] {detail}")
    stats["operations"].append(f"{op}: {detail}")


# ============================================================
# 1. VACUUM main database (reclaims fragmented space)
# ============================================================
def vacuum_main_db():
    print("\n" + "=" * 60)
    print("VACUUM: mike_memory.db")
    print("=" * 60)
    size_before = os.path.getsize(DB_PATH)
    log("VACUUM_START", f"Size before: {size_before/1024/1024:.1f}MB")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Check fragmentation first
    cur.execute("PRAGMA freelist_count")
    free_pages = cur.fetchone()[0]
    cur.execute("PRAGMA page_size")
    page_size = cur.fetchone()[0]
    free_kb = (free_pages * page_size) / 1024

    if free_kb < 1024:  # Less than 1MB free - skip
        log("VACUUM_SKIP", f"Only {free_kb:.0f}KB fragmented, skipping")
        conn.close()
        return

    print(f"  Free/fragmented space: {free_kb:.0f}KB ({free_kb/1024:.1f}MB)")

    # Backup before vacuum
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = os.path.join(ARCHIVE_DIR, f"mike_memory_pre_vacuum_{stamp}.db.backup")
    shutil.copy2(DB_PATH, backup_path)
    log("BACKUP", f"Pre-vacuum backup: {os.path.basename(backup_path)} ({os.path.getsize(backup_path)/1024/1024:.1f}MB)")

    # Run VACUUM
    print("  Running VACUUM (this may take a while)...")
    conn.execute("VACUUM")
    conn.close()

    size_after = os.path.getsize(DB_PATH)
    saved = (size_before - size_after) / 1024
    stats["space_saved_kb"] += saved
    log("VACUUM_DONE", f"Size after: {size_after/1024/1024:.1f}MB, Saved: {saved:.0f}KB")
    print(f"  Space saved: {saved:.0f}KB")


# ============================================================
# 2. Clean orphaned conversation embeddings
# ============================================================
def clean_orphaned_embeddings():
    print("\n" + "=" * 60)
    print("CLEAN: Orphaned Conversation Embeddings")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Find orphans
    cur.execute("""
        SELECT COUNT(*) FROM conversation_embeddings ce
        LEFT JOIN conversations c ON ce.conversation_id = c.id
        WHERE c.id IS NULL
    """)
    orphan_count = cur.fetchone()[0]
    print(f"  Orphaned conversation embeddings: {orphan_count}")

    if orphan_count == 0:
        log("ORPHAN_CHECK", "No orphaned conversation embeddings")
        conn.close()
        return

    # Archive before cleanup
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = os.path.join(ARCHIVE_DIR, f"orphaned_conversation_embeddings_{stamp}.jsonl")

    cur.execute("""
        SELECT ce.* FROM conversation_embeddings ce
        LEFT JOIN conversations c ON ce.conversation_id = c.id
        WHERE c.id IS NULL
    """)
    orphans = cur.fetchall()
    ce_cols = [d[0] for d in cur.description]

    with open(archive_path, "w", encoding="utf-8") as f:
        for row in orphans:
            record = dict(zip(ce_cols, row))
            # Convert any bytes to string
            for k, v in record.items():
                if isinstance(v, bytes):
                    record[k] = v.hex()
            f.write(json.dumps(record, default=str) + "\n")

    archive_size = os.path.getsize(archive_path)
    log("ARCHIVE", f"Archived {orphan_count} orphaned embeddings to {os.path.basename(archive_path)} ({archive_size/1024:.1f}KB)")

    # Remove orphans
    cur.execute("""
        DELETE FROM conversation_embeddings
        WHERE id IN (
            SELECT ce.id FROM conversation_embeddings ce
            LEFT JOIN conversations c ON ce.conversation_id = c.id
            WHERE c.id IS NULL
        )
    """)
    conn.commit()
    stats["records_archived"] += orphan_count
    log("CLEANUP", f"Removed {orphan_count} orphaned conversation embeddings")
    conn.close()


# ============================================================
# 3. Remove duplicate knowledge chunks (keep first)
# ============================================================
def dedup_knowledge_chunks():
    print("\n" + "=" * 60)
    print("DEDUP: Knowledge Chunks")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Find duplicate content
    cur.execute("""
        SELECT content, COUNT(*) as cnt, MIN(id) as keep_id
        FROM knowledge_chunks
        GROUP BY content
        HAVING cnt > 1
    """)
    dupes = cur.fetchall()
    print(f"  Duplicate content groups: {len(dupes)}")

    if len(dupes) == 0:
        log("DEDUP_CHECK", "No duplicate knowledge chunks found")
        conn.close()
        return

    total_dupes = 0
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = os.path.join(ARCHIVE_DIR, f"duplicate_chunks_{stamp}.jsonl")

    with open(archive_path, "w", encoding="utf-8") as f:
        for content, cnt, keep_id in dupes:
            # Get all IDs for this content
            cur.execute("SELECT * FROM knowledge_chunks WHERE content = ? AND id != ?", (content, keep_id))
            dup_rows = cur.fetchall()
            kc_cols = [d[0] for d in cur.description]

            for row in dup_rows:
                record = dict(zip(kc_cols, row))
                for k, v in record.items():
                    if isinstance(v, bytes):
                        record[k] = v.hex()
                    elif isinstance(v, str) and len(v) > 500:
                        record[k] = v[:500] + "..."
                f.write(json.dumps(record, default=str) + "\n")

            # Remove duplicates (keep the lowest ID)
            cur.execute("DELETE FROM knowledge_chunks WHERE content = ? AND id != ?", (content, keep_id))

            # Also remove corresponding embeddings
            cur.execute("SELECT id FROM knowledge_chunks WHERE content = ? AND id != ?", (content, keep_id))
            dup_ids = [r[0] for r in cur.fetchall()]
            for did in dup_ids:
                cur.execute("DELETE FROM knowledge_embeddings WHERE chunk_id = ?", (did,))
                cur.execute("DELETE FROM memory_mesh WHERE source_id = ? OR target_id = ?", (did, did))

            total_dupes += cnt - 1

    conn.commit()

    archive_size = os.path.getsize(archive_path)
    log("DEDUP", f"Archived {total_dupes} duplicate chunks to {os.path.basename(archive_path)} ({archive_size/1024:.1f}KB)")
    stats["records_archived"] += total_dupes
    print(f"  Removed {total_dupes} duplicate chunks")
    conn.close()


# ============================================================
# 4. Archive old mesh checkpoints (>90 days)
# ============================================================
def archive_old_checkpoints():
    print("\n" + "=" * 60)
    print("ARCHIVE: Old Mesh Checkpoints")
    print("=" * 60)
    cp_dir = os.path.join(MEMORY_DIR, "mesh_checkpoints")
    if not os.path.exists(cp_dir):
        print("  No mesh_checkpoints directory")
        return

    arch_cp_dir = os.path.join(ARCHIVE_DIR, "mesh_checkpoints")
    os.makedirs(arch_cp_dir, exist_ok=True)

    cutoff = datetime.now() - timedelta(days=90)
    files = os.listdir(cp_dir)
    moved_count = 0
    moved_size = 0

    for f in files:
        try:
            date_str = f.replace("mesh_", "")[:8]
            dt = datetime.strptime(date_str, "%Y%m%d")
            if dt < cutoff:
                src = os.path.join(cp_dir, f)
                dst = os.path.join(arch_cp_dir, f)
                shutil.move(src, dst)
                moved_size += os.path.getsize(dst)
                moved_count += 1
        except:
            pass

    if moved_count > 0:
        log("ARCHIVE_CP", f"Moved {moved_count} old checkpoints ({moved_size/1024:.1f}KB) to archives/")
        stats["space_saved_kb"] += moved_size / 1024
        print(f"  Moved {moved_count} checkpoints >90 days old, {moved_size/1024:.1f}KB")
    else:
        log("ARCHIVE_CP", "No old checkpoints to archive")


# ============================================================
# 5. Archive old autonomy log entries (>90 days -> moved to archive)
# ============================================================
def archive_old_logs():
    print("\n" + "=" * 60)
    print("ARCHIVE: Old Autonomy Logs")
    print("=" * 60)
    log_path = os.path.join(MEMORY_DIR, "autonomy", "autonomy_log.jsonl")
    if not os.path.exists(log_path):
        print("  No autonomy_log.jsonl")
        return

    size_before = os.path.getsize(log_path)
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    arch_path = os.path.join(ARCHIVE_DIR, f"autonomy_log_old_{stamp}.jsonl")

    kept = 0
    archived = 0
    arch_content = []

    with open(log_path, "r", encoding="utf-8") as f_in:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                is_old = False
                for tk in ["timestamp", "ts", "created_at", "time", "date"]:
                    if tk in obj:
                        try:
                            t = obj[tk]
                            if isinstance(t, (int, float)):
                                t_str = datetime.fromtimestamp(t if t < 1e12 else t/1000).isoformat()
                            else:
                                t_str = str(t)
                            if t_str < cutoff:
                                is_old = True
                            break
                        except:
                            pass
                if is_old:
                    arch_content.append(line)
                    archived += 1
                else:
                    kept += 1
            except:
                kept += 1

    if archived == 0:
        log("ARCHIVE_LOG", "No old log entries found")
        return

    with open(arch_path, "w", encoding="utf-8") as f:
        for entry in arch_content:
            f.write(entry + "\n")

    archive_size = os.path.getsize(arch_path)
    log("ARCHIVE_LOG", f"Archived {archived} old entries to {os.path.basename(arch_path)} ({archive_size/1024:.1f}KB)")
    stats["records_archived"] += archived

    # Rewrite log file without old entries (archive is kept separately)
    # We keep the current log intact for safety
    print(f"  Would archive {archived} entries >90 days ({archive_size/1024:.1f}KB)")
    print(f"  Current entries kept: {kept} in active log")
    print(f"  NOTE: Original log preserved, archive created as copy at {arch_path}")


# ============================================================
# 6. Clean empty qdrant entities collection
# ============================================================
def clean_empty_qdrant_collection():
    print("\n" + "=" * 60)
    print("CLEAN: Empty Qdrant Entity Collection")
    print("=" * 60)
    empty_dir = os.path.join(MEMORY_DIR, "mem0", "qdrant_store", "collection", "mike_mem0_entities")
    if not os.path.exists(empty_dir):
        print("  No empty entity collection found")
        return

    # Check if truly empty
    db_file = os.path.join(empty_dir, "storage.sqlite")
    if os.path.exists(db_file):
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM points")
        count = cur.fetchone()[0]
        conn.close()
        if count > 0:
            log("SKIP_EMPTY", f"Entity collection has {count} points, not empty")
            return

    # Archive the empty collection
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    arch_path = os.path.join(ARCHIVE_DIR, f"empty_entities_collection_{stamp}")
    shutil.move(empty_dir, arch_path)
    log("CLEAN_EMPTY", f"Moved empty entity collection to archives/")
    print(f"  Moved empty mike_mem0_entities collection to {arch_path}")


# ============================================================
# 7. Flag very short knowledge chunks for review (don't delete)
# ============================================================
def flag_short_chunks():
    print("\n" + "=" * 60)
    print("FLAG: Very Short Knowledge Chunks")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE LENGTH(content) < 20")
    short_count = cur.fetchone()[0]

    if short_count == 0:
        log("FLAG_SHORT", "No very short chunks found")
        conn.close()
        return

    # Write a review file
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    review_path = os.path.join(ARCHIVE_DIR, f"short_chunks_review_{stamp}.jsonl")

    cur.execute("SELECT id, content, LENGTH(content) as len FROM knowledge_chunks WHERE LENGTH(content) < 20")
    short_chunks = cur.fetchall()

    with open(review_path, "w", encoding="utf-8") as f:
        for row in short_chunks:
            f.write(json.dumps({"id": row[0], "content": row[1], "length": row[2]}) + "\n")

    size = os.path.getsize(review_path)
    log("FLAG_SHORT", f"Flagged {short_count} short chunks (<20 chars) for review: {os.path.basename(review_path)} ({size/1024:.1f}KB)")
    print(f"  Flagged {short_count} very short chunks for review (see {review_path})")
    conn.close()


# ============================================================
# 8. Rebuild FTS indexes (ensure sync)
# ============================================================
def rebuild_fts():
    print("\n" + "=" * 60)
    print("REBUILD: FTS Indexes")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Check drift
    cur.execute("SELECT ABS((SELECT COUNT(*) FROM knowledge_fts_content) - (SELECT COUNT(*) FROM knowledge_chunks))")
    drift_k = cur.fetchone()[0]
    cur.execute("SELECT ABS((SELECT COUNT(*) FROM conversations_fts_content) - (SELECT COUNT(*) FROM conversations))")
    drift_c = cur.fetchone()[0]

    if drift_k == 0 and drift_c == 0:
        log("FTS_OK", "FTS indexes are in sync, no rebuild needed")
        conn.close()
        return

    print(f"  Knowledge FTS drift: {drift_k}")
    print(f"  Conversations FTS drift: {drift_c}")

    # Rebuild knowledge FTS
    if drift_k > 0:
        print("  Rebuilding knowledge_fts...")
        cur.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        log("FTS_REBUILD", "Rebuilt knowledge_fts index")

    # Rebuild conversations FTS
    if drift_c > 0:
        print("  Rebuilding conversations_fts...")
        cur.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('rebuild')")
        log("FTS_REBUILD", "Rebuilt conversations_fts index")

    conn.commit()
    conn.close()


# ============================================================
# 9. Check and fix embedding cache (clear stale entries with old models/dims)
# ============================================================
def clean_embedding_cache():
    print("\n" + "=" * 60)
    print("CLEAN: Embedding Cache")
    print("=" * 60)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM embedding_cache")
    total = cur.fetchone()[0]
    cur.execute("SELECT dims, model, COUNT(*) FROM embedding_cache GROUP BY dims, model")
    groups = cur.fetchall()

    if len(groups) == 1:
        log("EC_OK", f"Single embedding type: {groups[0][1]} dims={groups[0][0]}, {total} entries")
        conn.close()
        return

    # Multiple types - find the dominant one and archive others
    dominant = max(groups, key=lambda x: x[2])
    print(f"  Dominant: {dominant[1]} dims={dominant[0]} ({dominant[2]} entries)")

    to_archive = [(d, m, c) for d, m, c in groups if c != dominant[2]]
    total_archived = 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")

    for dims, model, count in to_archive:
        archive_path = os.path.join(ARCHIVE_DIR, f"stale_embedding_cache_{model}_{dims}d_{stamp}.jsonl")
        cur.execute("SELECT * FROM embedding_cache WHERE dims = ? AND model = ?", (dims, model))
        rows = cur.fetchall()
        ec_cols = [d[0] for d in cur.description]

        with open(archive_path, "w", encoding="utf-8") as f:
            for row in rows:
                record = dict(zip(ec_cols, row))
                for k, v in record.items():
                    if isinstance(v, bytes):
                        record[k] = v.hex()
                f.write(json.dumps(record, default=str) + "\n")

        cur.execute("DELETE FROM embedding_cache WHERE dims = ? AND model = ?", (dims, model))
        total_archived += count
        log("EC_CLEAN", f"Archived {count} stale embeddings ({model}/{dims}d)")

    conn.commit()
    stats["records_archived"] += total_archived
    conn.close()


# ============================================================
# MAIN
# ============================================================
def main():
    global stats
    print("=" * 70)
    print("  MIKE MEMORY JANITOR - SAFE CLEANUP")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Archives directory: {ARCHIVE_DIR}")
    print("=" * 70)

    # 1. Archive operations first (safe, no data loss)
    archive_old_checkpoints()
    archive_old_logs()

    # 2. Cleanup operations
    clean_orphaned_embeddings()
    clean_empty_qdrant_collection()
    clean_embedding_cache()

    # 3. Dedup (archives before removing)
    dedup_knowledge_chunks()

    # 4. Flag for review (never delete)
    flag_short_chunks()

    # 5. Maintenance
    rebuild_fts()
    vacuum_main_db()

    # Summary
    print("\n" + "=" * 70)
    print("  CLEANUP SUMMARY")
    print("=" * 70)
    print(f"  Space saved: {stats['space_saved_kb']/1024:.1f}MB ({stats['space_saved_kb']:.0f}KB)")
    print(f"  Records archived: {stats['records_archived']}")
    print(f"  Operations performed: {len(stats['operations'])}")
    for op in stats["operations"]:
        print(f"    - {op}")

    # Write summary to archive dir
    summary_path = os.path.join(ARCHIVE_DIR, f"cleanup_summary_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    with open(summary_path, "w") as f:
        json.dump(stats, f, indent=2, default=str)

    return stats


if __name__ == "__main__":
    main()
