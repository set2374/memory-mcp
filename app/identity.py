"""Project identity resolution per ALP Shared Memory Spec §7.

5-level precedence:
1. Environment override (CODEX_MEMORY_PROJECT_ID)
2. .codex/memory/project.toml
3. Git remote-derived ID
4. Git folder-derived ID
5. Folder-name fallback
"""

import os
import re
import subprocess
from pathlib import Path

from loguru import logger


def resolve_project_id(project_path: str | None) -> str | None:
    """Resolve a project path to a stable project_id.

    Returns None for global scope (no project_path).
    """
    if not project_path:
        return None

    path = Path(project_path)

    # 1. Environment override
    env_id = os.environ.get("CODEX_MEMORY_PROJECT_ID")
    if env_id:
        logger.debug(f"Project ID from env: {env_id}")
        return _sanitize_id(env_id)

    # 2. .codex/memory/project.toml
    toml_id = _read_project_toml(path)
    if toml_id:
        logger.debug(f"Project ID from project.toml: {toml_id}")
        return _sanitize_id(toml_id)

    # 3. Git remote-derived ID
    git_remote = _git_remote_id(path)
    if git_remote:
        logger.debug(f"Project ID from git remote: {git_remote}")
        return git_remote

    # 4. Git folder-derived ID
    git_folder = _git_folder_id(path)
    if git_folder:
        logger.debug(f"Project ID from git folder: {git_folder}")
        return git_folder

    # 5. Folder-name fallback
    fallback = _folder_fallback(path)
    logger.debug(f"Project ID from folder fallback: {fallback}")
    return fallback


def _sanitize_id(raw: str) -> str:
    """Sanitize to a safe directory-name slug."""
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown"


def _read_project_toml(path: Path) -> str | None:
    """Read project_id from .codex/memory/project.toml."""
    toml_path = path / ".codex" / "memory" / "project.toml"
    if not toml_path.exists():
        return None
    try:
        text = toml_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r'\s*project_id\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def _git_remote_id(path: Path) -> str | None:
    """Derive project ID from git remote URL."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return _sanitize_id(f"{m.group(1)}-{m.group(2)}")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _git_folder_id(path: Path) -> str | None:
    """Derive project ID from git repo root folder name."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return _sanitize_id(Path(result.stdout.strip()).name)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _folder_fallback(path: Path) -> str:
    """Fallback: use the resolved folder name."""
    return _sanitize_id(path.resolve().name)
