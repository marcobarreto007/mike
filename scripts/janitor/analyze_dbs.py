"""Analyze all SQLite databases in the MIKE project."""
import sqlite3, os, sys

dbs = [
    r'C:\Users\Admin\Desktop\mike\database.db',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\mike_memory.db',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\reflections.db',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\mem0\history.db',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\mem0\qdrant_store\collection\mike_mem0\storage.sqlite',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\mem0\qdrant_store\collection\mike_mem0_entities\storage.sqlite',
    r'C:\Users\Admin\Desktop\mike\runtime\memory\mem0\backups\mem0_reset_20260724T225334Z_1024d_to_384d\history.db',
]

for db_path in dbs:
    if not os.path.exists(db_path):
        print(f'MISSING: {db_path}\n')
        continue
    size_kb = os.path.getsize(db_path) / 1024
    size_mb = size_kb / 1024
    short = os.path.basename(db_path)
    parent = os.path.basename(os.path.dirname(db_path))
    label = f"{parent}/{short}" if parent and parent != "memory" else short
    full_label = db_path.replace(r"C:\Users\Admin\Desktop\mike\runtime\memory", "...")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        total_rows = 0
        table_info = []
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{t}]")
                cnt = cur.fetchone()[0]
                total_rows += cnt
                if cnt > 0:
                    table_info.append(f"{t}={cnt}")
            except Exception as e:
                table_info.append(f"{t}=ERR:{e}")

        # Check for indexes
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        idxs = [r[0] for r in cur.fetchall()]

        # Get free pages (fragmentation)
        cur.execute("PRAGMA freelist_count")
        free_pages = cur.fetchone()[0]
        cur.execute("PRAGMA page_size")
        page_size = cur.fetchone()[0]
        free_kb = (free_pages * page_size) / 1024

        # Check for duplicate detection potential
        dupe_info = ""
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) - COUNT(DISTINCT *) FROM [{t}]")
                dupes = cur.fetchone()[0]
                if dupes and dupes > 0:
                    dupe_info += f" {t}:{dupes}dupes"
            except:
                pass

        conn.close()
        print(f"[{label}] {size_mb:.2f}MB | tables={len(tables)} | rows={total_rows} | indexes={len(idxs)} | free={free_kb:.0f}KB")
        print(f"  Path: {full_label}")
        if table_info:
            print(f"  Tables: {', '.join(table_info)}")
        if dupe_info:
            print(f"  DUPES:{dupe_info}")
    except Exception as e:
        print(f"ERROR {label}: {e}")
    print()
