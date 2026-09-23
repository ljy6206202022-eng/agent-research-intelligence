"""Allowlisted bounded file snapshots. Never import/call production services."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat

from .paths import BoundaryError, Workspace

PROJECT_ROOT = Path(os.environ["RESEARCH_INTEL_PROJECT_ROOT"]).absolute() if os.environ.get("RESEARCH_INTEL_PROJECT_ROOT") else None
PROJECT_STATE = PROJECT_ROOT / "project-state.json" if PROJECT_ROOT else None


def read_project_file(path: Path) -> tuple[bytes, dict]:
    if PROJECT_ROOT is None or path is None:
        raise BoundaryError("Optional project root is not configured")
    path = Path(path).absolute()
    if path.resolve() != path:
        raise BoundaryError("Project read symlinks denied")
    if path != PROJECT_STATE and not path.is_relative_to(PROJECT_ROOT / "docs"):
        raise BoundaryError("File is outside the initial project-read allowlist")
    if path.suffix.lower() not in {".md", ".json", ".txt"}:
        raise BoundaryError("Unsupported project material type")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024:
            raise BoundaryError("Invalid/oversized project input")
        content = stream.read(2 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
        current = path.stat()
        if len(content) > 2 * 1024 * 1024 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or current.st_ino != before.st_ino:
            raise BoundaryError("Project input changed while reading")
    receipt = {"source": str(path), "sha256": hashlib.sha256(content).hexdigest(),
               "classification": "PRIVATE", "authority": "READ_ONLY_PROJECT_MATERIAL",
               "captured_at": datetime.now(timezone.utc).isoformat()}
    return content, receipt


def snapshot_project_file(workspace: Workspace, path: Path) -> dict:
    import json
    from uuid import uuid4
    from agent_research_intelligence.storage.database import Store
    from agent_research_intelligence.storage.models import ResearchOutput
    content, receipt = read_project_file(path)
    relative = f"data/private_context/{uuid4().hex}"
    with Store(workspace) as store:
        workspace.write(relative + ".content", content)
        workspace.write(relative + ".json", json.dumps(receipt, ensure_ascii=False).encode())
        store.append(ResearchOutput(kind="private_snapshot", classification="PRIVATE",
                                    text=json.dumps(receipt), source_refs=(relative,)))
    return {"snapshot": relative, "sha256": receipt["sha256"], "classification": "PRIVATE"}
