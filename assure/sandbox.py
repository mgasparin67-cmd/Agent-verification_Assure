"""Sandbox manager: per-run workspace from a versioned fixture, frozen spec, snapshots, teardown.

Run directory layout (all outside the agent's reach in docker mode):
    runs/<run_id>/sandbox/workspace/   agent-visible state, written only by the gateway
    runs/<run_id>/verifier/            frozen task/policy/assertions, mode 0444
    runs/<run_id>/evidence/            logs, artifacts, before/after snapshots, report
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def fs_manifest(root: Path) -> dict[str, dict[str, Any]]:
    """Every entry under root: files by content hash, dirs and symlinks by mode/target. No following."""
    out: dict[str, dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            mode = oct(stat.S_IMODE(st.st_mode))
            if stat.S_ISLNK(st.st_mode):
                out[rel] = {"type": "symlink", "target": os.readlink(p), "mode": mode}
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = {"type": "dir", "mode": mode}
            elif stat.S_ISREG(st.st_mode):
                out[rel] = {"type": "file", "sha256": sha256_file(p), "size": st.st_size, "mode": mode}
            else:
                out[rel] = {"type": "other", "mode": mode}
    return out


def backup_db(src: Path, dst: Path) -> None:
    """Consistent copy via the SQLite backup API (read-only open of the source)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


@dataclass
class Fixture:
    path: Path
    fixture_id: str
    version: str
    database: str
    seed_sql: str
    known_secrets: list[str]
    snapshot_files: bool = False  # keep before/after copies of workspace files (code tasks)

    @classmethod
    def load(cls, path: Path) -> "Fixture":
        meta = yaml.safe_load((path / "fixture.yaml").read_text())
        return cls(path=path, fixture_id=meta["fixture_id"], version=meta["version"],
                   database=meta["database"], seed_sql=meta["seed_sql"],
                   known_secrets=meta.get("known_secrets", []),
                   snapshot_files=bool(meta.get("snapshot_files", False)))


@dataclass
class RunDirs:
    root: Path
    workspace: Path
    verifier: Path
    evidence: Path


class SandboxManager:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir

    def create(self, run_id: str, fixture: Fixture) -> RunDirs:
        root = self.runs_dir / run_id
        if root.exists():
            raise FileExistsError(f"run directory already exists: {root}")
        dirs = RunDirs(root, root / "sandbox" / "workspace", root / "verifier", root / "evidence")
        shutil.copytree(fixture.path / "files", dirs.workspace)
        db = dirs.workspace / fixture.database
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.executescript((fixture.path / fixture.seed_sql).read_text())
            conn.commit()
        finally:
            conn.close()
        # Deterministic DB bytes/mode regardless of umask.
        os.chmod(db, 0o644)
        dirs.verifier.mkdir(parents=True)
        dirs.evidence.mkdir(parents=True)
        return dirs

    @staticmethod
    def freeze(dirs: RunDirs, files: dict[str, Path]) -> dict[str, str]:
        """Copy human-authored spec files into verifier/, make them read-only, return digests."""
        digests = {}
        for name, src in files.items():
            dst = dirs.verifier / name
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o444)
            digests[name] = sha256_file(dst)
        os.chmod(dirs.verifier, 0o555)
        return digests

    @staticmethod
    def verify_frozen(dirs: RunDirs, digests: dict[str, str]) -> list[str]:
        problems = []
        for name, expected in digests.items():
            p = dirs.verifier / name
            if not p.exists():
                problems.append(f"{name}: missing")
            elif sha256_file(p) != expected:
                problems.append(f"{name}: digest changed")
        return problems

    @staticmethod
    def snapshot(dirs: RunDirs, fixture: Fixture, label: str) -> dict[str, Any]:
        db_copy = dirs.evidence / f"{label}.db"
        backup_db(dirs.workspace / fixture.database, db_copy)
        os.chmod(db_copy, 0o444)
        manifest = fs_manifest(dirs.workspace)
        (dirs.evidence / f"{label}_fs.yaml").write_text(yaml.safe_dump(manifest, sort_keys=True))
        out = {"db": db_copy, "fs": manifest}
        if fixture.snapshot_files:
            db = Path(fixture.database)
            files = dirs.evidence / f"{label}_files"
            shutil.copytree(dirs.workspace, files, symlinks=True,
                            ignore=lambda d, names: [n for n in names if Path(d, n) == dirs.workspace / db
                                                     or n.startswith(db.name + "-")])
            out["files"] = files
        return out

    @staticmethod
    def destroy(dirs: RunDirs) -> None:
        shutil.rmtree(dirs.root / "sandbox", ignore_errors=True)
