"""Analyze all JSON memory files in the MIKE project."""
import os, json
from datetime import datetime

MEMORY_DIR = os.path.join(r"C:\Users\Admin\Desktop\mike", "runtime", "memory")

json_files = [
    os.path.join(MEMORY_DIR, "soul.json"),
    os.path.join(MEMORY_DIR, "skills_catalog.json"),
    os.path.join(MEMORY_DIR, "magic_tokens.json"),
    os.path.join(MEMORY_DIR, "missions", "missions.json"),
    os.path.join(MEMORY_DIR, "curriculum", "curriculum_goals.json"),
    os.path.join(MEMORY_DIR, "learner_patterns.json"),
    os.path.join(MEMORY_DIR, "autonomy", "routines.json"),
    os.path.join(MEMORY_DIR, "autonomy", "task_board.json"),
    os.path.join(MEMORY_DIR, "autonomy", "email_tracking.json"),
    os.path.join(MEMORY_DIR, "skills_lib", "skill_library.json"),
]

jsonl_files = [
    os.path.join(MEMORY_DIR, "autonomy", "autonomy_log.jsonl"),
    os.path.join(MEMORY_DIR, "guard", "guard_audit.jsonl"),
]

def make_short(fp):
    return fp.replace(MEMORY_DIR + "\\", "")

print("=" * 60)
print("JSON FILES")
print("=" * 60)

for fp in json_files:
    if not os.path.exists(fp):
        print(f"MISSING: {fp}")
        continue
    size_kb = os.path.getsize(fp) / 1024
    mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    short = make_short(fp)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            key_count = len(data)
            oldest = None
            newest = None
            for k, v in data.items():
                if isinstance(v, dict):
                    for tk in ["timestamp", "created_at", "date", "ts", "updated_at", "created"]:
                        if tk in v:
                            try:
                                t = v[tk]
                                if isinstance(t, (int, float)):
                                    if t > 1e12:
                                        dt = datetime.fromtimestamp(t / 1000)
                                    else:
                                        dt = datetime.fromtimestamp(t) if t > 0 else None
                                elif isinstance(t, str):
                                    dt = datetime.fromisoformat(t.replace("Z", "+00:00").split("+")[0].split("[")[0])
                                else:
                                    dt = None
                                if dt:
                                    if oldest is None or dt < oldest:
                                        oldest = dt
                                    if newest is None or dt > newest:
                                        newest = dt
                            except:
                                pass
            age_info = ""
            if oldest and newest:
                age_info = f" | oldest={oldest.strftime('%Y-%m-%d')} newest={newest.strftime('%Y-%m-%d')}"
            print(f"[{short}] {size_kb:.1f}KB | mtime={mtime} | type=dict keys={key_count}{age_info}")
        elif isinstance(data, list):
            print(f"[{short}] {size_kb:.1f}KB | mtime={mtime} | type=list items={len(data)}")
        else:
            print(f"[{short}] {size_kb:.1f}KB | mtime={mtime} | type={type(data).__name__}")
    except json.JSONDecodeError as e:
        print(f"[{short}] {size_kb:.1f}KB | mtime={mtime} | CORRUPTED JSON: {e}")
    except Exception as e:
        print(f"ERROR {short}: {e}")

print()
print("=" * 60)
print("JSONL FILES")
print("=" * 60)

for fp in jsonl_files:
    if not os.path.exists(fp):
        print(f"MISSING: {fp}")
        continue
    size_kb = os.path.getsize(fp) / 1024
    mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    short = make_short(fp)
    line_count = 0
    empty_lines = 0
    oldest_ts = None
    newest_ts = None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    empty_lines += 1
                    continue
                line_count += 1
                try:
                    obj = json.loads(line)
                    for tk in ["timestamp", "ts", "created_at", "time", "date"]:
                        if tk in obj:
                            try:
                                t = obj[tk]
                                if isinstance(t, (int, float)):
                                    if t > 1e12:
                                        dt = datetime.fromtimestamp(t / 1000)
                                    else:
                                        dt = datetime.fromtimestamp(t) if t > 0 else None
                                elif isinstance(t, str):
                                    dt = datetime.fromisoformat(t.replace("Z", "+00:00").split("+")[0])
                                else:
                                    dt = None
                                if dt:
                                    if oldest_ts is None or dt < oldest_ts:
                                        oldest_ts = dt
                                    if newest_ts is None or dt > newest_ts:
                                        newest_ts = dt
                            except:
                                pass
                except:
                    pass
        age_info = ""
        if oldest_ts and newest_ts:
            age_info = f" | oldest={oldest_ts.strftime('%Y-%m-%d')} newest={newest_ts.strftime('%Y-%m-%d')}"
        print(f"[{short}] {size_kb:.1f}KB | mtime={mtime} | lines={line_count} empty={empty_lines}{age_info}")
    except Exception as e:
        print(f"ERROR {short}: {e}")
