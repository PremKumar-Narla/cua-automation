"""
Artifact persistence + versioning (brief §3.2).

Files on disk, not a DB (the brief explicitly anti-rewards scaling infra).
Old versions stay replayable; a re-record bumps `version`.
"""
from __future__ import annotations

import json
from pathlib import Path

from cua.artifact.schema import Capability

ARTIFACT_DIR = Path("artifacts")


def save(cap: Capability, root: Path = ARTIFACT_DIR) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{cap.capability_id}.v{cap.version}.json"
    path.write_text(cap.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    return path


def load(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))
