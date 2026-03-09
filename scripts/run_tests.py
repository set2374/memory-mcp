"""Quick test runner for Memory MCP Server."""
import subprocess
import sys
import os

os.chdir(os.path.join(os.path.dirname(__file__), ".."))

# First: quick smoke test of DB init
print("=== Smoke Test: DB Init ===")
from app.db import init_db
init_db()
print("DB init OK")

# Second: quick smoke test of memory_status
print("\n=== Smoke Test: memory_status ===")
from app.tools import memory_status
result = memory_status()
print(f"Status: {result['status']}")
print(f"Version: {result['version']}")
print(f"Total memories: {result['counts']['total_memories']}")
print("memory_status OK")

# Third: quick smoke test of memory_read_codex
print("\n=== Smoke Test: memory_read_codex ===")
from app.tools import memory_read_codex
result = memory_read_codex()
if "error" in result:
    print(f"Codex cache: {result.get('error', 'unknown')}")
else:
    print(f"Codex events: {result['total_codex_events']}")
print("memory_read_codex OK")

print("\n=== All smoke tests passed ===")
