"""Read-only views of snapshots and before/after diffs. Used by assertions and the report."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class DbView:
    """Read-only view of a snapshot database copy."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        self.conn.row_factory = sqlite3.Row

    def tables(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    def schema(self) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]

    def pragmas(self) -> dict[str, Any]:
        return {p: self.conn.execute(f"PRAGMA {p}").fetchone()[0] for p in ("user_version", "application_id")}

    def rows(self, table: str) -> dict[int, dict[str, Any]]:
        cur = self.conn.execute(f'SELECT rowid AS __rowid__, * FROM "{table}" ORDER BY rowid')
        return {r["__rowid__"]: {k: r[k] for k in r.keys() if k != "__rowid__"} for r in cur}

    def row(self, table: str, rowid: int) -> dict[str, Any] | None:
        return self.rows(table).get(rowid)

    def integrity(self) -> str:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0]

    def close(self) -> None:
        self.conn.close()


def db_diff(before: DbView, after: DbView) -> dict[str, Any]:
    out: dict[str, Any] = {"schema_changed": before.schema() != after.schema(), "rows": [], "changes": []}
    if out["schema_changed"]:
        b, a = set(before.schema()), set(after.schema())
        out["schema"] = {"removed": [x[1] for x in b - a], "added": [x[1] for x in a - b]}
    for table in sorted(set(before.tables()) | set(after.tables())):
        br = before.rows(table) if table in before.tables() else {}
        ar = after.rows(table) if table in after.tables() else {}
        for rid in sorted(set(br) | set(ar)):
            old, new = br.get(rid), ar.get(rid)
            if old == new:
                continue
            kind = "inserted" if old is None else "deleted" if new is None else "modified"
            cols = {c: [(old or {}).get(c), (new or {}).get(c)]
                    for c in sorted(set(old or {}) | set(new or {}))
                    if (old or {}).get(c) != (new or {}).get(c) or kind != "modified"}
            out["rows"].append({"table": table, "row_id": rid, "change": kind, "columns": cols,
                                "before": old, "after": new})
            for c, (_, nv) in cols.items():
                out["changes"].append({"table": table, "row_id": rid, "column": c, "new_value": nv})
    return out


def fs_diff(before: dict[str, dict], after: dict[str, dict]) -> dict[str, Any]:
    modified = {}
    for p in sorted(set(before) & set(after)):
        if before[p] != after[p]:
            modified[p] = sorted(k for k in set(before[p]) | set(after[p]) if before[p].get(k) != after[p].get(k))
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "modified": modified}


def files_diff(before_root: Path, after_root: Path, fs: dict[str, Any]) -> str:
    """Unified diff of the text files that changed between two workspace copies."""
    import difflib

    chunks = []
    for rel in [*fs["modified"], *fs["added"], *fs["removed"]]:
        a, b = Path(before_root, rel), Path(after_root, rel)
        if (a.exists() and not a.is_file()) or (b.exists() and not b.is_file()):
            continue
        try:
            old = a.read_text().splitlines(keepends=True) if a.exists() else []
            new = b.read_text().splitlines(keepends=True) if b.exists() else []
        except UnicodeDecodeError:
            chunks.append(f"Binary file {rel} changed\n")
            continue
        chunks += difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}")
    return "".join(chunks)
