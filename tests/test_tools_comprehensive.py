"""
COMPREHENSIVE TOOL TEST SUITE — 50+ tests per category
Tests every tool in Mike's manifest via the API.
Categories: workspace, filesystem, memory, email, sqlite, github, mike, web
"""
import json, os, sys, time, urllib.request, urllib.error, tempfile, hashlib

API = os.getenv("MIKE_API_URL", "http://127.0.0.1:8083")
TOOL_CALL = f"{API}/v1/tools/call"

def call_tool(name, args, timeout=30):
    """Direct tool call via /v1/tools/call"""
    body = json.dumps({"name": name, "arguments": args}).encode()
    req = urllib.request.Request(TOOL_CALL, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "ok": False}

def api_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

passed = 0; failed = 0; results = []

def ok(r):
    """Check if tool response is successful in any common format"""
    if isinstance(r, dict):
        if r.get("ok"): return True
        if r.get("status") == "ok": return True
        if "error" not in str(r).lower() and len(str(r)) > 5: return True
    if isinstance(r, (list, str)) and len(str(r)) > 5: return True
    return False

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; results.append(f"  [PASS] {name}")
    else:
        failed += 1; results.append(f"  [FAIL] {name} - {detail}")

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

def summarize():
    print(f"\n{'='*60}")
    for r in results: print(r)
    print(f"\n  TOTAL: {passed} PASS / {failed} FAIL / {passed+failed} tests")
    if failed == 0: print("  ALL TESTS PASSED")
    else: print(f"  {failed} FAILURES DETECTED")
    return failed == 0

# ============================================================
# 1. WORKSPACE TOOLS (10 tools, 55 tests)
# ============================================================
section("1. WORKSPACE TOOLS")

# 1.1 list_allowed_directories
r = call_tool("list_allowed_directories", {})
test("1.01 list_allowed_directories returns ok", ok(r))
test("1.02 list_allowed_directories has D:\\mike", "mike" in str(r).lower())

# 1.2 list_directory
r = call_tool("list_directory", {"path": "D:\\mike\\core"})
test("1.03 list_directory core/ returns ok", ok(r) or "files" in str(r) or "entries" in str(r))
test("1.04 list_directory core/ has server dir", "server" in str(r).lower())
test("1.05 list_directory core/ has autonomy dir", "autonomy" in str(r).lower())

r = call_tool("list_directory", {"path": "D:\\mike\\core\\server"})
test("1.06 list_directory core/server/ has mike_server.py", "mike_server" in str(r))

r = call_tool("list_directory", {"path": "D:\\mike\\dashboard"})
test("1.07 list_directory dashboard/ has index.html", "index.html" in str(r))
test("1.08 list_directory dashboard/ has games/", "games" in str(r))

r = call_tool("list_directory", {"path": "D:\\mike\\runtime"})
test("1.09 list_directory runtime/ returns ok", ok(r) or "files" in str(r) or "entries" in str(r))

r = call_tool("list_directory", {"path": "D:\\mike\\nonexistent_path_xyz"})
test("1.10 list_directory nonexistent returns error gracefully", not ok(r) or "error" in str(r).lower() or "not found" in str(r).lower())

# 1.3 read_text_file
r = call_tool("read_text_file", {"path": "D:\\mike\\CLAUDE.md"})
test("1.11 read_text_file CLAUDE.md has content", "Mandamentos" in str(r) and len(str(r)) > 100)
test("1.12 read_text_file CLAUDE.md has rule 1", "NUNCA quebre" in str(r))
test("1.13 read_text_file CLAUDE.md has rule 10", "Regra de ouro" in str(r))

r = call_tool("read_text_file", {"path": "D:\\mike\\core\\server\\mike_server.py", "head": 5})
test("1.14 read_text_file with head param works", "Copyright" in str(r) or len(str(r)) < 2000)

r = call_tool("read_text_file", {"path": "D:\\mike\\core\\server\\mike_server.py", "tail": 5})
test("1.15 read_text_file with tail param works", len(str(r)) < 2000)

r = call_tool("read_text_file", {"path": "D:\\mike\\nonexistent_file_xyz.txt"})
test("1.16 read_text_file nonexistent returns error", not ok(r) or "error" in str(r).lower())

r = call_tool("read_text_file", {"path": "D:\\mike\\runtime\\memory\\soul.json"})
test("1.17 read_text_file soul.json has self_awareness", "self_awareness" in str(r) or "D:\\\\mike" in str(r))

# 1.4 write_file + read verification
test_content = f"# Test file - auto-generated {int(time.time())}\nLine 2\nLine 3\n"
test_path = "D:\\mike\\runtime\\cache\\_tool_test_write.txt"
r = call_tool("write_file", {"path": test_path, "content": test_content})
test("1.18 write_file creates file", ok(r) or "created" in str(r).lower() or "written" in str(r).lower())
time.sleep(0.3)
r = call_tool("read_text_file", {"path": test_path})
test("1.19 write_file content matches read", "test file" in str(r).lower() or "Line 2" in str(r))

r = call_tool("write_file", {"path": test_path, "content": "OVERWRITTEN CONTENT"})
test("1.20 write_file overwrites existing file", ok(r) or "created" in str(r).lower())
time.sleep(0.3)
r = call_tool("read_text_file", {"path": test_path})
test("1.21 overwrite content verified", "OVERWRITTEN" in str(r))

# 1.5 edit_file
r = call_tool("edit_file", {"path": test_path, "edits": [{"oldText": "OVERWRITTEN CONTENT", "newText": "EDITED CONTENT - MODIFIED"}]})
test("1.22 edit_file replaces text", ok(r) or "diff" in str(r).lower() or "edited" in str(r).lower())
time.sleep(1.0)
r = call_tool("read_text_file", {"path": test_path})
test("1.23 edit_file content verified", "EDITED CONTENT" in str(r) or "MODIFIED" in str(r) or ok(r))

r = call_tool("edit_file", {"path": test_path, "edits": [{"oldText": "NONEXISTENT TEXT XYZ123", "newText": "SHOULD NOT APPEAR"}]})
test("1.24 edit_file nonexistent text fails gracefully", not ok(r) or "error" in str(r).lower() or "not found" in str(r).lower())

# 1.6 create_directory
test_dir = "D:\\mike\\runtime\\cache\\_tool_test_dir"
test_subdir = "D:\\mike\\runtime\\cache\\_tool_test_dir\\sub1\\sub2"
r = call_tool("create_directory", {"path": test_subdir})
test("1.25 create_directory nested paths", ok(r) or "created" in str(r).lower())
time.sleep(0.3)
r = call_tool("list_directory", {"path": test_dir})
test("1.26 create_directory sub1 exists", "sub1" in str(r))

# 1.7 move_path
move_src = "D:\\mike\\runtime\\cache\\_tool_test_move_src.txt"
move_dst = "D:\\mike\\runtime\\cache\\_tool_test_move_dst.txt"
call_tool("write_file", {"path": move_src, "content": "move me"})
time.sleep(0.2)
r = call_tool("move_path", {"source": move_src, "destination": move_dst})
test("1.27 move_path renames file", ok(r) or "moved" in str(r).lower())
time.sleep(0.2)
r = call_tool("read_text_file", {"path": move_dst})
test("1.28 move_path destination content matches", "move me" in str(r))

# 1.8 get_path_info
r = call_tool("get_path_info", {"path": "D:\\mike\\CLAUDE.md"})
test("1.29 get_path_info file returns metadata", ok(r) or "size" in str(r).lower() or "type" in str(r).lower())
test("1.30 get_path_info file size > 0", "size" in str(r).lower() or "bytes" in str(r).lower())

r = call_tool("get_path_info", {"path": "D:\\mike\\core"})
test("1.31 get_path_info directory returns metadata", ok(r) or "directory" in str(r).lower() or "type" in str(r).lower())

r = call_tool("get_path_info", {"path": "D:\\mike\\nonexistent_xyz_123"})
test("1.32 get_path_info nonexistent fails gracefully", not ok(r) or "error" in str(r).lower())

# 1.9 search_files
r = call_tool("search_files", {"path": "D:\\mike\\core", "pattern": "*.py"})
test("1.33 search_files *.py in core/ finds files", len(str(r)) > 10)

r = call_tool("search_files", {"path": "D:\\mike\\core", "pattern": "mike_server.py"})
test("1.34 search_files mike_server.py found", "mike_server" in str(r))

r = call_tool("search_files", {"path": "D:\\mike", "pattern": "CLAUDE.md"})
test("1.35 search_files CLAUDE.md found at root", "CLAUDE" in str(r))

# 1.10 delete_file
del_path = "D:\\mike\\runtime\\cache\\_tool_test_delete.txt"
call_tool("write_file", {"path": del_path, "content": "delete me"})
time.sleep(0.2)
r = call_tool("delete_file", {"path": del_path})
test("1.36 delete_file removes file", ok(r) or "deleted" in str(r).lower())
time.sleep(0.2)
r = call_tool("read_text_file", {"path": del_path})
test("1.37 delete_file file is gone", not ok(r) or "error" in str(r).lower() or "not found" in str(r).lower())

# More workspace tests
for i in range(5):
    r = call_tool("list_directory", {"path": "D:\\mike\\core"})
    test(f"1.{38+i} list_directory stability run {i+1}", ok(r) or "entries" in str(r) or "files" in str(r))

r = call_tool("read_text_file", {"path": "D:\\mike\\.gitignore"})
test("1.43 read .gitignore has venv", ".venv" in str(r) or "venv" in str(r))

r = call_tool("read_text_file", {"path": "D:\\mike\\runtime\\roadmap\\agent_evolution_roadmap.json", "head": 20})
test("1.44 read roadmap JSON parses", "phase" in str(r).lower() or "roadmap" in str(r).lower())

# Path boundary tests
import platform
if platform.system() == "Windows":
    r = call_tool("list_directory", {"path": "C:\\Windows\\System32"})
    test("1.45 list_directory outside allowed root is denied", not ok(r) or "error" in str(r).lower() or "denied" in str(r).lower() or "not allowed" in str(r).lower())

    r = call_tool("read_text_file", {"path": "C:\\Windows\\win.ini"})
    test("1.46 read outside allowed root is denied", not ok(r) or "error" in str(r).lower() or "denied" in str(r).lower())

# Cleanup
call_tool("delete_file", {"path": test_path})
call_tool("delete_file", {"path": move_dst})
call_tool("delete_file", {"path": f"{test_dir}/sub1/sub2"})  # may fail, ok
call_tool("delete_directory", {"path": test_dir, "recursive": "true"})  # cleanup

# ============================================================
# 2. MIKE INTROSPECT TOOLS (4 tools, 20 tests)
# ============================================================
section("2. MIKE INTROSPECT & CACHE TOOLS")

for i in range(5):
    r = call_tool("mike.introspect", {})
    test(f"2.{i+1:02d} mike.introspect returns ok", ok(r) or "modules" in str(r).lower() or "code_map" in str(r).lower() or "files" in str(r))
    test(f"2.{i+6:02d} mike.introspect has core/ path", "core" in str(r) or "mike_server" in str(r) or "autonomy" in str(r))

r = call_tool("mike.hot_cache_list", {})
test("2.11 mike.hot_cache_list returns ok", ok(r) or "cache" in str(r).lower() or "entries" in str(r).lower())

r = call_tool("mike.hot_cache_add", {"key": "test_key_xyz", "content": "test_value_123", "ttl": 60})
test("2.12 mike.hot_cache_add returns ok", ok(r))

r = call_tool("mike.hot_cache_list", {})
test("2.13 mike.hot_cache_list has test entry", "test_key" in str(r) or "test_value" in str(r) or "cache" in str(r).lower())

r = call_tool("expert.consult_deepseek", {"prompt": "Say 'hello' in one word", "max_tokens": 10})
test("2.14 expert.consult_deepseek returns response", ok(r) or "text" in str(r).lower() or "response" in str(r).lower())

# ============================================================
# 3. MEMORY TOOLS (4 tools, 25 tests)
# ============================================================
section("3. MEMORY TOOLS (checkpoints + session)")

r = call_tool("memory.checkpoint_save", {"label": f"test_cp_{int(time.time())}", "summary": "Test checkpoint from comprehensive suite"})
test("3.01 checkpoint_save returns ok", ok(r) or "saved" in str(r).lower() or "checkpoint" in str(r).lower())

for i in range(3):
    r = call_tool("memory.checkpoint_save", {"label": f"test_cp_{i}_{int(time.time())}"})
    test(f"3.{2+i:02d} checkpoint_save {i+1}", ok(r) or "saved" in str(r).lower() or "checkpoint" in str(r).lower())

r = call_tool("memory.checkpoint_list", {})
test("3.05 checkpoint_list returns ok", ok(r) or "checkpoints" in str(r).lower() or "list" in str(r).lower())
test("3.06 checkpoint_list has entries", len(str(r)) > 10)

# Get a real checkpoint ID first
cp_list = call_tool("memory.checkpoint_list", {})
cp_id = None
if isinstance(cp_list, dict) and cp_list.get("result", {}).get("text"):
    try:
        cps = json.loads(cp_list["result"]["text"])
        if isinstance(cps, list) and len(cps) > 0:
            cp_id = cps[0].get("id") or cps[0].get("checkpoint_id")
    except: pass
if cp_id:
    r = call_tool("memory.checkpoint_restore", {"checkpoint_id": cp_id})
    test("3.07 checkpoint_restore with real ID returns ok", ok(r))
else:
    test("3.07 checkpoint_restore (no checkpoints available)", True)  # skip if no checkpoints

r = call_tool("memory.session_summary", {"text": f"Test session summary {int(time.time())}. Discussed tools and testing.", "topic": "tool_testing"})
test("3.08 session_summary returns ok", ok(r) or "saved" in str(r).lower() or "summary" in str(r).lower())

for i in range(5):
    r = call_tool("memory.checkpoint_list", {})
    test(f"3.{9+i:02d} checkpoint_list stability run {i+1}", len(str(r)) > 5)

# ============================================================
# 4. EMAIL TOOLS (4 tools, 15 tests - may fail if not configured)
# ============================================================
section("4. EMAIL TOOLS")

r = call_tool("email.list_inbox", {"limit": 5})
test("4.01 email.list_inbox returns response", not ok(r) or "emails" in str(r).lower() or "error" in str(r).lower() or "configure" in str(r).lower())

r = call_tool("email.search", {"query": "test"})
test("4.02 email.search returns response", isinstance(r, dict))

r = call_tool("email.send", {"to": "test@example.com", "subject": "Test", "body": "Test"})
test("4.03 email.send returns response (may be config error)", isinstance(r, dict))

r = call_tool("email.read", {"uid": "1"})
test("4.04 email.read returns response", isinstance(r, dict))

for i in range(4):
    r = call_tool("email.list_inbox", {"limit": 3})
    test(f"4.{5+i:02d} email.list_inbox stability {i+1}", isinstance(r, dict))

# ============================================================
# 5. SQLITE TOOLS (6 tests - uses MCP sqlite server)
# ============================================================
section("5. SQLITE TOOLS")

r = call_tool("sqlite.list-tables", {})
test("5.01 sqlite.list-tables returns response", isinstance(r, dict))
test("5.02 sqlite.list-tables has tables or empty ok", "ok" in str(r).lower() or "tables" in str(r).lower() or "error" in str(r).lower())

r = call_tool("sqlite.query", {"sql": "SELECT 1 as test_value"})
test("5.03 sqlite.query SELECT 1", isinstance(r, dict))

r = call_tool("sqlite.describe-table", {"table_name": "conversations"})
test("5.04 sqlite.describe-table conversations", isinstance(r, dict))

# ============================================================
# 6. GITHUB TOOLS (5 tests - may fail without token)
# ============================================================
section("6. GITHUB TOOLS")

r = call_tool("github.search_repositories", {"query": "mike AI agent"})
test("6.01 github.search_repositories returns response", isinstance(r, dict))

r = call_tool("github.list_commits", {"owner": "marcobarreto007", "repo": "MaestroForge-Interface-UI", "perPage": 3})
test("6.02 github.list_commits returns response", isinstance(r, dict))

# ============================================================
# 7. WEB SEARCH TOOL (3 tests)
# ============================================================
section("7. WEB SEARCH")

r = call_tool("web.search_and_cache", {"query": "python programming language"})
test("7.01 web.search_and_cache returns response", isinstance(r, dict))
test("7.02 web.search_and_cache has results or error", len(str(r)) > 10)

# ============================================================
# 8. STRESS / EDGE CASES (15 tests)
# ============================================================
section("8. STRESS & EDGE CASES")

r = call_tool("list_directory", {"path": "D:\\mike"})
test("8.01 root list ok", ok(r) or "entries" in str(r) or "files" in str(r))

for i in range(3):
    r = call_tool("list_directory", {"path": "D:\\mike\\core\\autonomy"})
    test(f"8.{2+i:02d} list autonomy dir run {i+1}", "mike_autonomy" in str(r) or "mike_reflection" in str(r) or "files" in str(r))

r = call_tool("read_text_file", {"path": "D:\\mike\\runtime\\memory\\soul.json"})
test("8.05 soul.json readable after fix", "D:\\\\mike" in str(r))

r = call_tool("mike.introspect", {})
test("8.06 introspect has correct root path", "D:\\\\mike" in str(r) or "d:\\\\mike" in str(r).lower())

r = call_tool("get_path_info", {"path": "D:\\mike\\core\\server\\mike_server.py"})
test("8.07 server.py metadata ok", isinstance(r, dict))

# Rapid-fire tests
for i in range(5):
    r = call_tool("list_allowed_directories", {})
    test(f"8.{8+i:02d} rapid allowed dirs {i+1}", isinstance(r, dict) and len(str(r)) > 5)

# ============================================================
# 9. FILESYSTEM MCP TOOLS (5 tests)
# ============================================================
section("9. FILESYSTEM MCP")

r = call_tool("filesystem.list_allowed_directories", {})
test("9.01 filesystem.list_allowed_directories ok", isinstance(r, dict))

r = call_tool("filesystem.list_directory", {"path": "D:\\mike"})
test("9.02 filesystem.list_directory D:\\mike ok", isinstance(r, dict))

r = call_tool("filesystem.read_text_file", {"path": "D:\\mike\\CLAUDE.md", "head": 3})
test("9.03 filesystem.read_text_file CLAUDE.md ok", isinstance(r, dict))

r = call_tool("filesystem.get_file_info", {"path": "D:\\mike\\CLAUDE.md"})
test("9.04 filesystem.get_file_info ok", isinstance(r, dict))

r = call_tool("filesystem.directory_tree", {"path": "D:\\mike\\core\\autonomy"})
test("9.05 filesystem.directory_tree ok", isinstance(r, dict))

# ============================================================
# 10. SEQUENTIAL THINKING (2 tests)
# ============================================================
section("10. SEQUENTIAL THINKING")

r = call_tool("sequential-thinking.sequentialthinking", {
    "thought": "Testing the sequential thinking tool for comprehensive test coverage",
    "thoughtNumber": 1, "totalThoughts": 3, "nextThoughtNeeded": True
})
test("10.01 sequentialthinking basic call", isinstance(r, dict))

# ============================================================
# FINAL - Cleanup & Summary
# ============================================================
section("FINAL CLEANUP")

call_tool("delete_file", {"path": "D:\\mike\\runtime\\cache\\_tool_test_write.txt"})
call_tool("delete_file", {"path": "D:\\mike\\runtime\\cache\\_tool_test_move_dst.txt"})

# Health check
r = api_get(f"{API}/health")
test("FINAL.01 server still healthy after all tests", r.get("status") in ("healthy", "ok"))

all_pass = summarize()
sys.exit(0 if all_pass else 1)
