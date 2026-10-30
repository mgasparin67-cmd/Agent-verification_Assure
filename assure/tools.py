"""Narrowly scoped structured tools. There is deliberately no shell and no network tool.

Each tool has a typed argument model, a `plan` step (derive the resources it would touch,
without side effects) and an `execute` step. The gateway runs policy between the two.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, ValidationError, field_validator

from .models import Resource, Scalar, Strict, digest
from .paths import PathError, resolve
from .policy import PlannedCall

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_READ_BYTES = 256 * 1024


class ToolInvalid(Exception):
    """Arguments are structurally valid JSON but cannot be executed (bad identifier, missing file…)."""


class RowLimitExceeded(Exception):
    def __init__(self, matched: int, limit: int):
        super().__init__(f"update matched {matched} rows; limit is {limit}; transaction rolled back")
        self.matched, self.limit = matched, limit


def _ident(name: str) -> str:
    if not IDENT.match(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return name


class Workspace:
    def __init__(self, root: Path, db_rel: str):
        self.root = root
        self.db_rel = db_rel
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.root / self.db_rel, isolation_level=None)
            self._conn.execute("PRAGMA trusted_schema=OFF")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def columns(self, table: str) -> list[str]:
        self.conn.set_authorizer(None)
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists or table.startswith("sqlite_"):
            raise ToolInvalid(f"unknown table '{table}'")
        return [r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')]


def _authorizer(table: str, write_cols: set[str]):
    """SQLite authorizer: the statement may only read/update the planned table and columns.

    Defense in depth on top of identifier validation; it also blocks side effects from triggers.
    """

    def auth(action, arg1, arg2, _db, _src):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 == table:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_UPDATE and arg1 == table and arg2 in write_cols:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return auth


def _matching_rows(ws: Workspace, table: str, where: dict[str, Any]) -> list[int] | None:
    """Read-only preview of the rowids a where-clause matches; None if it cannot be evaluated."""
    try:
        if any(c not in ws.columns(table) for c in where):
            return None
    except ToolInvalid:
        return None
    clause = " AND ".join(f'"{c}" = ?' for c in where)
    ws.conn.set_authorizer(_authorizer(table, set()))
    try:
        return [r[0] for r in ws.conn.execute(f'SELECT rowid FROM "{table}" WHERE {clause}', list(where.values()))]
    except sqlite3.Error:
        return None
    finally:
        ws.conn.set_authorizer(None)


# --------------------------------------------------------------------------- argument models


class DbQueryArgs(Strict):
    table: str
    where: dict[str, Scalar] = {}
    columns: list[str] | None = None
    limit: int = Field(100, ge=1, le=100)

    @field_validator("table")
    @classmethod
    def _t(cls, v):
        return _ident(v)

    @field_validator("where")
    @classmethod
    def _w(cls, v):
        return {_ident(k): x for k, x in v.items()}

    @field_validator("columns")
    @classmethod
    def _c(cls, v):
        return None if v is None else [_ident(c) for c in v]


class DbUpdateArgs(Strict):
    table: str
    set: dict[str, Scalar] = Field(min_length=1)
    where: dict[str, Scalar] = Field(min_length=1)  # no unbounded updates

    @field_validator("table")
    @classmethod
    def _t(cls, v):
        return _ident(v)

    @field_validator("set", "where")
    @classmethod
    def _keys(cls, v):
        return {_ident(k): x for k, x in v.items()}


class PathArgs(Strict):
    path: str = Field(min_length=1, max_length=512)


class ListArgs(Strict):
    path: str = Field(".", min_length=1, max_length=512)


class WriteArgs(Strict):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=64 * 1024)


# --------------------------------------------------------------------------- tools


class Tool:
    name: ClassVar[str]
    args_model: ClassVar[type[Strict]]
    description: ClassVar[str]

    def parse(self, raw: dict[str, Any]) -> Strict:
        return self.args_model.model_validate(raw)

    def plan(self, ws: Workspace, args: Any, raw: dict[str, Any]) -> PlannedCall:
        raise NotImplementedError

    def execute(self, ws: Workspace, args: Any, call: PlannedCall, max_rows: int) -> tuple[dict, dict]:
        raise NotImplementedError


class DbQuery(Tool):
    name = "db_query"
    args_model = DbQueryArgs
    description = ("Read rows: {table, where?: {col: value}, columns?: [..], limit?: <=100}. "
                   "The result says truncated: true when more rows match than were returned.")

    def plan(self, ws, a: DbQueryArgs, raw):
        cols = a.columns or ["*"]
        return PlannedCall(self.name, raw, digest(raw), table=a.table, where=dict(a.where),
                           resources=[Resource(kind="db_table", target=a.table, access="read", columns=cols)])

    def execute(self, ws, a: DbQueryArgs, call, max_rows):
        known = ws.columns(a.table)
        bad = [c for c in [*(a.columns or []), *a.where] if c not in known]
        if bad:
            raise ToolInvalid(f"unknown columns {bad} in '{a.table}'")
        sel = ", ".join(f'"{c}"' for c in (a.columns or known))
        clause = " AND ".join(f'"{c}" = ?' for c in a.where) or "1=1"
        ws.conn.set_authorizer(_authorizer(a.table, set()))
        try:
            cur = ws.conn.execute(f'SELECT rowid, {sel} FROM "{a.table}" WHERE {clause} LIMIT ?',
                                  [*a.where.values(), a.limit + 1])  # one extra row detects truncation
            names = [d[0] for d in cur.description][1:]
            fetched = cur.fetchall()
        finally:
            ws.conn.set_authorizer(None)
        truncated, fetched = len(fetched) > a.limit, fetched[:a.limit]
        call.rows = [r[0] for r in fetched]  # which rows the agent has seen, for read-before-write
        rows = [dict(zip(names, r[1:])) for r in fetched]
        return {"rows": rows, "truncated": truncated}, {"rows_returned": len(rows), "truncated": truncated}


class DbUpdate(Tool):
    name = "db_update"
    args_model = DbUpdateArgs
    description = "Update rows: {table, set: {col: value}, where: {col: value}} (where is required)"

    def plan(self, ws, a: DbUpdateArgs, raw):
        return PlannedCall(self.name, raw, digest(raw), table=a.table, where=dict(a.where),
                           set_columns=sorted(a.set), rows=_matching_rows(ws, a.table, a.where),
                           resources=[Resource(kind="db_table", target=a.table, access="write",
                                               columns=sorted(a.set))])

    def execute(self, ws, a: DbUpdateArgs, call, max_rows):
        known = ws.columns(a.table)
        bad = [c for c in [*a.set, *a.where] if c not in known]
        if bad:
            raise ToolInvalid(f"unknown columns {bad} in '{a.table}'")
        sets = ", ".join(f'"{c}" = ?' for c in a.set)
        clause = " AND ".join(f'"{c}" = ?' for c in a.where)
        ws.conn.set_authorizer(_authorizer(a.table, set(a.set)))
        try:
            ws.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = ws.conn.execute(f'UPDATE "{a.table}" SET {sets} WHERE {clause}',
                                      [*a.set.values(), *a.where.values()])
                if cur.rowcount > max_rows:
                    ws.conn.execute("ROLLBACK")
                    raise RowLimitExceeded(cur.rowcount, max_rows)
                ws.conn.execute("COMMIT")
            except sqlite3.Error:
                ws.conn.execute("ROLLBACK")
                raise
        finally:
            ws.conn.set_authorizer(None)
        return {"rows_affected": cur.rowcount}, {"rows_affected": cur.rowcount}


class FsRead(Tool):
    name = "fs_read"
    args_model = PathArgs
    description = "Read a text file: {path}"

    def plan(self, ws, a: PathArgs, raw):
        rp = resolve(ws.root, a.path)
        return PlannedCall(self.name, raw, digest(raw), paths=[(rp, "read")],
                           resources=[Resource(kind="file", target=rp.relative or a.path, access="read")])

    def execute(self, ws, a, call, max_rows):
        p = call.paths[0][0].absolute
        if not p.is_file():
            raise ToolInvalid(f"not a file: {a.path}")
        data = p.read_bytes()[:MAX_READ_BYTES]
        return {"content": data.decode("utf-8", errors="replace")}, {"bytes_read": len(data)}


class FsList(Tool):
    name = "fs_list"
    args_model = ListArgs
    description = "List a directory: {path?}"

    def plan(self, ws, a: ListArgs, raw):
        rp = resolve(ws.root, a.path)
        return PlannedCall(self.name, raw, digest(raw), paths=[(rp, "read")],
                           resources=[Resource(kind="file", target=rp.relative or a.path, access="read")])

    def execute(self, ws, a, call, max_rows):
        p = call.paths[0][0].absolute
        if not p.is_dir():
            raise ToolInvalid(f"not a directory: {a.path}")
        entries = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())
        return {"entries": entries}, {"entries": len(entries)}


class FsWrite(Tool):
    name = "fs_write"
    args_model = WriteArgs
    description = "Write a text file: {path, content}"

    def plan(self, ws, a: WriteArgs, raw):
        rp = resolve(ws.root, a.path)
        return PlannedCall(self.name, raw, digest(raw), paths=[(rp, "write")],
                           resources=[Resource(kind="file", target=rp.relative or a.path, access="write")])

    def execute(self, ws, a, call, max_rows):
        p = call.paths[0][0].absolute
        if not p.parent.is_dir():
            raise ToolInvalid(f"parent directory does not exist: {a.path}")
        p.write_text(a.content)
        return {"bytes_written": len(a.content.encode())}, {"bytes_written": len(a.content.encode())}


REGISTRY: dict[str, Tool] = {t.name: t for t in (DbQuery(), DbUpdate(), FsRead(), FsList(), FsWrite())}


def describe_tools(allowed: list[str]) -> list[dict[str, Any]]:
    return [{"name": n, "description": REGISTRY[n].description,
             "schema": REGISTRY[n].args_model.model_json_schema()} for n in allowed if n in REGISTRY]


__all__ = ["REGISTRY", "Workspace", "ToolInvalid", "RowLimitExceeded", "PathError", "ValidationError",
           "describe_tools"]
