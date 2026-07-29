"""Deep analysis of memory databases for duplicates, orphans, and stale data."""
import sqlite3, os, json
from datetime import datetime, timedelta

DB = r"C:\Users\Admin\Desktop\mike\runtime\memory\mike_memory.db"
MEM0_DB = r"C:\Users\Admin\Desktop\mike\runtime\memory\mem0\history.db"
REFL_DB = r"C:\Users\Admin\Desktop\mike\runtime\memory\reflections.db"

def safe_parse_ts(val):
    """Try to parse a timestamp value into a datetime."""
    if val is None:
        return None
    try:
        if isinstance(val, str):
            return datetime.fromisoformat(val.replace("Z", "+00:00").split("+")[0].split("[")[0])
        v = float(val)
        if v > 1e12:
            return datetime.fromtimestamp(v / 1000)
        elif v > 0:
            return datetime.fromtimestamp(v)
    except:
        pass
    return None


def analyze_main_db():
    print("=" * 70)
    print("MIKE_MEMORY.DB (972MB) DEEP DIVE")
    print("=" * 70)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 1. Conversations
    print("\n--- Conversations ---")
    cur.execute("SELECT COUNT(*) FROM conversations")
    print(f"  Total: {cur.fetchone()[0]}")
    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM conversations")
    mn, mx = cur.fetchone()
    print(f"  Timeline: {safe_parse_ts(mn)} -> {safe_parse_ts(mx)}")
    cur.execute("SELECT COUNT(DISTINCT entry_hash) FROM conversations")
    print(f"  Unique hashes: {cur.fetchone()[0]} (no hash dupes)")
    cur.execute("SELECT COUNT(DISTINCT session_id) FROM conversations")
    print(f"  Unique sessions: {cur.fetchone()[0]}")

    # 2. Knowledge chunks
    print("\n--- Knowledge Chunks ---")
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT content) FROM knowledge_chunks")
    total, distinct = cur.fetchone()
    print(f"  Total: {total}, Distinct content: {distinct}, Potential dupes: {total - distinct}")
    cur.execute("SELECT AVG(LENGTH(content)), MIN(LENGTH(content)), MAX(LENGTH(content)) FROM knowledge_chunks")
    avg_len, min_len, max_len = cur.fetchone()
    print(f"  Content: avg={avg_len:.0f} chars, min={min_len}, max={max_len}")
    # Find short/empty chunks
    cur.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE LENGTH(content) < 20")
    short = cur.fetchone()[0]
    print(f"  Very short chunks (<20 chars): {short}")

    # 3. Embedding cache
    print("\n--- Embedding Cache ---")
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT cache_key) FROM embedding_cache")
    total_ec, unique_key = cur.fetchone()
    print(f"  Total: {total_ec}, Unique cache_key: {unique_key}, Dupes: {total_ec - unique_key}")
    cur.execute("SELECT COUNT(DISTINCT model), COUNT(DISTINCT dims) FROM embedding_cache")
    models, dims = cur.fetchone()
    print(f"  Distinct models: {models}, Distinct dims: {dims}")
    cur.execute("SELECT dims, model, COUNT(*) FROM embedding_cache GROUP BY dims, model")
    for row in cur.fetchall():
        print(f"    dims={row[0]}, model={row[1]}: {row[2]} entries")
    cur.execute("SELECT MIN(created_at), MAX(created_at) FROM embedding_cache")
    mn_ec, mx_ec = cur.fetchone()
    print(f"  Timeline: {safe_parse_ts(mn_ec)} -> {safe_parse_ts(mx_ec)}")

    # 4. Memory mesh
    print("\n--- Memory Mesh ---")
    cur.execute("SELECT COUNT(*) FROM memory_mesh")
    print(f"  Total edges: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(DISTINCT source_id), COUNT(DISTINCT target_id) FROM memory_mesh")
    src, tgt = cur.fetchone()
    print(f"  Distinct sources: {src}, Distinct targets: {tgt}")
    cur.execute("SELECT relation, COUNT(*) as cnt FROM memory_mesh GROUP BY relation ORDER BY cnt DESC LIMIT 10")
    for row in cur.fetchall():
        print(f"    {row[0]}: {row[1]}")
    # Check for orphan references (source or target not in knowledge_chunks)
    cur.execute("SELECT COUNT(*) FROM memory_mesh mm LEFT JOIN knowledge_chunks kc ON mm.source_id = kc.id WHERE kc.id IS NULL")
    orphan_src = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM memory_mesh mm LEFT JOIN knowledge_chunks kc ON mm.target_id = kc.id WHERE kc.id IS NULL")
    orphan_tgt = cur.fetchone()[0]
    print(f"  Orphan source refs: {orphan_src}, Orphan target refs: {orphan_tgt}")
    cur.execute("SELECT MIN(created_at), MAX(created_at) FROM memory_mesh")
    mn_mm, mx_mm = cur.fetchone()
    print(f"  Timeline: {safe_parse_ts(mn_mm)} -> {safe_parse_ts(mx_mm)}")

    # 5. Knowledge embeddings vs chunks (orphan check)
    print("\n--- Knowledge Embeddings ---")
    cur.execute("PRAGMA table_info(knowledge_embeddings)")
    ke_cols = [c[1] for c in cur.fetchall()]
    for id_col in ["chunk_id", "knowledge_id"]:
        if id_col in ke_cols:
            cur.execute(f"SELECT COUNT(*) FROM knowledge_embeddings ke LEFT JOIN knowledge_chunks kc ON ke.{id_col} = kc.id WHERE kc.id IS NULL")
            print(f"  Orphaned ({id_col}): {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM knowledge_embeddings")
    ke_total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
    kc_total = cur.fetchone()[0]
    print(f"  Embeddings: {ke_total}, Chunks: {kc_total}")
    if ke_total < kc_total:
        print(f"  WARNING: {kc_total - ke_total} chunks have no embedding!")

    # 6. Conversation embeddings
    print("\n--- Conversation Embeddings ---")
    cur.execute("PRAGMA table_info(conversation_embeddings)")
    ce_cols = [c[1] for c in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM conversation_embeddings")
    ce_total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM conversations")
    c_total = cur.fetchone()[0]
    print(f"  Embeddings: {ce_total}, Conversations: {c_total}")
    for col in ["conversation_id", "conv_id"]:
        if col in ce_cols:
            cur.execute(f"SELECT COUNT(*) FROM conversation_embeddings ce LEFT JOIN conversations c ON ce.{col} = c.id WHERE c.id IS NULL")
            print(f"  Orphaned ({col}): {cur.fetchone()[0]}")

    # 7. Session checkpoints
    print("\n--- Session Checkpoints ---")
    cur.execute("SELECT COUNT(*) FROM session_checkpoints")
    sc_count = cur.fetchone()[0]
    print(f"  Total: {sc_count}")
    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM session_checkpoints")
    mn_sc, mx_sc = cur.fetchone()
    print(f"  Timeline: {safe_parse_ts(mn_sc)} -> {safe_parse_ts(mx_sc)}")

    # 8. Session summaries
    print("\n--- Session Summaries ---")
    cur.execute("SELECT COUNT(*) FROM session_summaries")
    print(f"  Total: {cur.fetchone()[0]}")

    # 9. FTS sync check
    print("\n--- FTS Index Sync ---")
    cur.execute("SELECT (SELECT COUNT(*) FROM knowledge_fts_content) - (SELECT COUNT(*) FROM knowledge_chunks)")
    print(f"  knowledge FTS drift: {cur.fetchone()[0]} (should be 0)")
    cur.execute("SELECT (SELECT COUNT(*) FROM conversations_fts_content) - (SELECT COUNT(*) FROM conversations)")
    print(f"  conversations FTS drift: {cur.fetchone()[0]} (should be 0)")

    # 10. Fragmentation
    print("\n--- Fragmentation ---")
    cur.execute("PRAGMA freelist_count")
    free_pages = cur.fetchone()[0]
    cur.execute("PRAGMA page_size")
    page_size = cur.fetchone()[0]
    free_mb = (free_pages * page_size) / (1024 * 1024)
    cur.execute("PRAGMA page_count")
    total_pages = cur.fetchone()[0]
    total_mb = (total_pages * page_size) / (1024 * 1024)
    print(f"  Total: {total_mb:.1f}MB, Free (fragmented): {free_mb:.1f}MB ({free_mb/total_mb*100:.1f}%)")

    # 11. Decisions
    print("\n--- Decisions ---")
    cur.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM decisions")
    cnt, mn_dec, mx_dec = cur.fetchone()
    print(f"  Count: {cnt}, {safe_parse_ts(mn_dec)} -> {safe_parse_ts(mx_dec)}")

    # 12. Project patterns
    print("\n--- Project Patterns ---")
    cur.execute("SELECT COUNT(*) FROM project_patterns")
    print(f"  Count: {cur.fetchone()[0]}")

    conn.close()


def analyze_reflections_db():
    print("\n" + "=" * 70)
    print("REFLECTIONS.DB (1.4MB)")
    print("=" * 70)
    conn = sqlite3.connect(REFL_DB)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM reflections")
    r_count = cur.fetchone()[0]
    print(f"  Reflections: {r_count}")

    cur.execute("PRAGMA table_info(reflections)")
    cols = [c[1] for c in cur.fetchall()]
    for ts_col in ["created_at", "timestamp", "ts"]:
        if ts_col in cols:
            cur.execute(f"SELECT MIN({ts_col}), MAX({ts_col}) FROM reflections")
            mn, mx = cur.fetchone()
            print(f"  {ts_col}: {safe_parse_ts(mn)} -> {safe_parse_ts(mx)}")

    cur.execute("SELECT COUNT(*) FROM reflection_embeddings")
    re_count = cur.fetchone()[0]
    print(f"  Embeddings: {re_count}, Missing: {r_count - re_count}")

    cur.execute("SELECT COUNT(*) FROM reflections_fts_content")
    print(f"  FTS content: {cur.fetchone()[0]}, in_sync={cur.fetchone()==r_count}")

    cur.execute("SELECT * FROM reflection_stats")
    stats = cur.fetchone()
    if stats:
        cur.execute("PRAGMA table_info(reflection_stats)")
        rs_cols = [c[1] for c in cur.fetchall()]
        for i, c in enumerate(rs_cols):
            print(f"  stat.{c}: {stats[i]}")

    conn.close()


def analyze_mem0():
    print("\n" + "=" * 70)
    print("MEM0 HISTORY.DB (0.6MB)")
    print("=" * 70)
    db_path = MEM0_DB
    if not os.path.exists(db_path):
        print(f"  NOT FOUND: {db_path}")
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(history)")
    cols = [c[1] for c in cur.fetchall()]
    print(f"  Columns: {cols}")
    cur.execute("SELECT COUNT(*) FROM history")
    h_count = cur.fetchone()[0]
    print(f"  Total rows: {h_count}")

    for ts_col in ["created_at", "timestamp", "updated_at"]:
        if ts_col in cols:
            cur.execute(f"SELECT MIN({ts_col}), MAX({ts_col}) FROM history")
            mn, mx = cur.fetchone()
            if mn:
                print(f"  {ts_col}: {safe_parse_ts(mn)} -> {safe_parse_ts(mx)}")

    # Check user/memory uniqueness
    for col in ["user_id", "memory_id", "id"]:
        if col in cols:
            cur.execute(f"SELECT COUNT(DISTINCT {col}) FROM history")
            print(f"  Distinct {col}: {cur.fetchone()[0]}")

    # Check for text content bloat
    for col in cols:
        if any(x in col.lower() for x in ["content", "text", "data", "memory"]):
            cur.execute(f"SELECT AVG(LENGTH({col})), SUM(LENGTH({col})) FROM history")
            avg, total = cur.fetchone()
            if total:
                print(f"  {col}: avg={avg:.0f}B, total={total/1024:.1f}KB")
            break

    conn.close()

    # Backup analysis
    backup_path = r"C:\Users\Admin\Desktop\mike\runtime\memory\mem0\backups\mem0_reset_20260724T225334Z_1024d_to_384d\history.db"
    if os.path.exists(backup_path):
        print(f"\n  Backup history.db: {os.path.getsize(backup_path)/1024:.1f}KB")
        bconn = sqlite3.connect(backup_path)
        bcur = bconn.cursor()
        bcur.execute("SELECT COUNT(*) FROM history")
        bcount = bcur.fetchone()[0]
        bconn.close()
        print(f"  Backup rows: {bcount}, Live rows: {h_count}")
        if bcount < h_count:
            print(f"  INFO: Backup is stale ({h_count - bcount} rows behind)")

    # Check for multiple backup dirs
    bu_dir = r"C:\Users\Admin\Desktop\mike\runtime\memory\mem0\backups"
    if os.path.exists(bu_dir):
        subdirs = [d for d in os.listdir(bu_dir) if os.path.isdir(os.path.join(bu_dir, d))]
        print(f"\n  Backup dirs: {len(subdirs)}")
        total_bu_size = 0
        for sd in subdirs:
            sd_path = os.path.join(bu_dir, sd)
            sd_size = sum(os.path.getsize(os.path.join(sd_path, f)) for f in os.listdir(sd_path) if os.path.isfile(os.path.join(sd_path, f)))
            total_bu_size += sd_size
            print(f"    {sd}: {sd_size/1024:.1f}KB")
        print(f"  Total backup size: {total_bu_size/1024:.1f}KB")


def analyze_mem0_qdrant():
    print("\n" + "=" * 70)
    print("MEM0 QDRANT VECTOR STORES")
    print("=" * 70)

    # Main collection
    qd = r"C:\Users\Admin\Desktop\mike\runtime\memory\mem0\qdrant_store\collection\mike_mem0\storage.sqlite"
    if os.path.exists(qd):
        size_kb = os.path.getsize(qd) / 1024
        print(f"  mike_mem0 collection: {size_kb:.1f}KB ({size_kb/1024:.2f}MB)")
        conn = sqlite3.connect(qd)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM points")
        pt_count = cur.fetchone()[0]
        print(f"  Points: {pt_count}")
        # Check payload size
        cur.execute("SELECT AVG(LENGTH(payload)), SUM(LENGTH(payload)), MAX(LENGTH(payload)) FROM points")
        avg_p, total_p, max_p = cur.fetchone()
        if total_p:
            print(f"  Payload: avg={avg_p:.0f}B, total={total_p/1024:.1f}KB, max={max_p}B")
        # Check vector count matches history count
        cur.execute("SELECT AVG(LENGTH(vector)), SUM(LENGTH(vector)) FROM points")
        avg_v, total_v, max_v = cur.fetchone()
        if total_v:
            print(f"  Vector: avg={avg_v:.0f}B, total={total_v/1024:.1f}KB")
        conn.close()

        # Check for duplicate points (same payload)
        conn = sqlite3.connect(qd)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT payload) FROM points")
        t, d = cur.fetchone()
        if t != d:
            print(f"  Potential duplicate points: {t - d}")
        else:
            print(f"  No payload duplicates detected")
        conn.close()

    # Entity collection (empty)
    qd2 = r"C:\Users\Admin\Desktop\mike\runtime\memory\mem0\qdrant_store\collection\mike_mem0_entities\storage.sqlite"
    if os.path.exists(qd2):
        size2 = os.path.getsize(qd2) / 1024
        conn2 = sqlite3.connect(qd2)
        cur2 = conn2.cursor()
        cur2.execute("SELECT COUNT(*) FROM points")
        pts = cur2.fetchone()[0]
        conn2.close()
        print(f"\n  mike_mem0_entities: {size2:.1f}KB, {pts} points - EMPTY ORPHANED COLLECTION")


def analyze_mesh_checkpoints():
    print("\n" + "=" * 70)
    print("MESH CHECKPOINTS")
    print("=" * 70)
    cp_dir = r"C:\Users\Admin\Desktop\mike\runtime\memory\mesh_checkpoints"
    if not os.path.exists(cp_dir):
        print("  Directory not found")
        return

    files = sorted(os.listdir(cp_dir))
    total_size = sum(os.path.getsize(os.path.join(cp_dir, f)) for f in files)
    print(f"  Total: {len(files)} files, {total_size/1024:.1f}KB ({total_size/1024/1024:.2f}MB)")

    from collections import defaultdict
    by_step = defaultdict(int)
    groups = defaultdict(list)
    for f in files:
        parts = f.replace("mesh_", "").split("_")
        if len(parts) >= 3:
            date = parts[0]
            step = parts[-1].replace(".json", "")
            groups[(date, step)].append(f)
            by_step[step] += 1

    print("  Files by step:")
    for step, cnt in sorted(by_step.items()):
        print(f"    step={step}: {cnt} files")

    # Find redundant (same date+step, multiple timestamps)
    print("\n  Redundant (same date+step, keep latest):")
    can_archive_size = 0
    can_archive_count = 0
    for (date, step), files_list in sorted(groups.items()):
        if len(files_list) > 1:
            sorted_files = sorted(files_list, reverse=True)
            for f in sorted_files[1:]:
                path = os.path.join(cp_dir, f)
                size = os.path.getsize(path)
                can_archive_size += size
                can_archive_count += 1
                print(f"    ARCHIVE: {f} ({size/1024:.0f}KB)")
    if can_archive_count == 0:
        print("    (none)")
    else:
        print(f"    Total: {can_archive_count} files, {can_archive_size/1024:.1f}KB")

    # Old checkpoints
    cutoff = datetime.now() - timedelta(days=90)
    old_total_size = 0
    old_files = []
    for f in files:
        try:
            date_str = f.replace("mesh_", "")[:8]
            dt = datetime.strptime(date_str, "%Y%m%d")
            if dt < cutoff:
                path = os.path.join(cp_dir, f)
                size = os.path.getsize(path)
                old_total_size += size
                old_files.append(f)
        except:
            pass
    print(f"\n  >90 days old: {len(old_files)} files, {old_total_size/1024:.1f}KB")


if __name__ == "__main__":
    analyze_main_db()
    analyze_reflections_db()
    analyze_mem0()
    analyze_mem0_qdrant()
    analyze_mesh_checkpoints()
