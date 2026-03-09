"""Run pytest for Memory MCP Server."""
import subprocess
import sys
import os

os.chdir(os.path.join(os.path.dirname(__file__), ".."))

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "--no-header", "-x"],
    capture_output=False,
    timeout=60,
)
sys.exit(result.returncode)
