from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCKED = {
    "project config sync tool": "sync_project",
    "project config upstream tool": "check_project_updates",
    "project upstream route": "/v1/projects/{project}/upstream",
    "project sync route": "/v1/projects/{project}/sync",
    "repo project config file": ".glimmung/project.yaml",
}

IGNORED_DIRS = {".git", ".venv", "__pycache__", "dist", "build"}
ALLOWED_PATHS = {"tests/test_removed_project_config_sync.py"}


def test_repo_backed_project_config_sync_surface_stays_removed() -> None:
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
        "repo-backed project-config sync is retired; edit the durable Glimmung "
        "project row via register_project instead:\n" + "\n".join(failures)
    )
