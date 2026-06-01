from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCKED = {
    "repo workflow manifest path": ".glimmung/workflows",
    "workflow sync check tool": "check_workflow_updates",
    "workflow sync apply tool": "sync_workflow",
    "workflow upstream route": "/v1/projects/{project}/workflows/{workflow}/upstream",
    "workflow sync route": "/v1/projects/{project}/workflows/{workflow}/sync",
}

IGNORED_DIRS = {".git", ".venv", "__pycache__", "dist", "build"}
ALLOWED_PATHS = {"tests/test_removed_workflow_sync.py"}


def test_repo_backed_workflow_sync_surface_stays_removed() -> None:
    failures: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if rel in ALLOWED_PATHS or not path.is_file():
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        for label, needle in BLOCKED.items():
            if needle in content:
                failures.append(f"{rel}: {label} ({needle})")

    assert not failures, (
        "repo-backed workflow sync is retired; use durable Glimmung workflow "
        "registration instead:\n" + "\n".join(failures)
    )
