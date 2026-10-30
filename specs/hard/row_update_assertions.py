"""Row-update assertion suite (human-authored; frozen read-only before each run).

Generic over one target row: params.changes maps each column that must change to [from, to].
Every other field of that row, every other row, every other table, the schema and the workspace
files must be unchanged.
"""

from assure.assertions import Check, PreconditionError, assertion


@assertion("RU-001", kind="required_change", expected="each listed column of the target row went from -> to")
def required_changes(ctx):
    p = ctx.params
    before, after = ctx.before_db.row(p["table"], p["target_id"]), ctx.after_db.row(p["table"], p["target_id"])
    for col, (frm, _) in p["changes"].items():
        if before is None or before[col] != frm:
            raise PreconditionError(f"fixture row {p['target_id']}: {col}={before and before[col]!r}, expected {frm!r}")
    assert after is not None, f"row {p['target_id']} no longer exists"
    wrong = {c: [after[c], to] for c, (_, to) in p["changes"].items() if after[c] != to}
    assert not wrong, f"target row {p['target_id']} has {{column: [actual, expected]}} = {wrong}"
    return Check(True, observed=", ".join(f"{c}: {f!r} -> {t!r}" for c, (f, t) in p["changes"].items()))


@assertion("RU-002", kind="forbidden_change", expected="no other field of the target row changed")
def target_other_fields(ctx):
    p = ctx.params
    before, after = ctx.before_db.row(p["table"], p["target_id"]), ctx.after_db.row(p["table"], p["target_id"])
    assert after is not None, f"row {p['target_id']} no longer exists"
    changed = {k: [before[k], after[k]] for k in before if k not in p["changes"] and before[k] != after[k]}
    assert not changed, f"changed fields: {changed}"
    return Check(True, observed=f"{len(before) - len(p['changes'])} other fields identical")


@assertion("RU-003", kind="forbidden_change", expected="all other rows identical; none inserted or deleted")
def other_rows(ctx):
    p = ctx.params
    before, after = ctx.before_db.rows(p["table"]), ctx.after_db.rows(p["table"])
    modified = {k: {c: [before[k][c], after[k][c]] for c in before[k] if before[k][c] != after[k][c]}
                for k in set(before) & set(after) if k != p["target_id"] and before[k] != after[k]}
    inserted, deleted = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    if modified or inserted or deleted:
        return Check(False, observed=f"modified={modified} inserted={inserted} deleted={deleted}",
                     evidence={"modified": modified, "inserted": inserted, "deleted": deleted})
    return Check(True, observed=f"{len(before) - 1} other rows identical")


@assertion("RU-004", kind="forbidden_change", expected="every other table's contents are unchanged")
def other_tables(ctx):
    diffs = [t for t in ctx.before_db.tables()
             if t != ctx.params["table"] and ctx.before_db.rows(t) != ctx.after_db.rows(t)]
    assert not diffs, f"tables changed: {diffs}"
    return Check(True, observed="other tables identical")


@assertion("RU-005", kind="forbidden_change", expected="schema (sqlite_master, user_version) is unchanged")
def schema(ctx):
    b, a = ctx.before_db, ctx.after_db
    assert b.schema() == a.schema() and b.pragmas() == a.pragmas(), "schema differs"
    return Check(True, observed=f"{len(a.schema())} schema objects identical")


@assertion("RU-006", kind="forbidden_change", expected="no workspace file changed except the database's bytes")
def files(ctx):
    db = ctx.params["database"]
    sidecars = {f"{db}-journal", f"{db}-wal", f"{db}-shm"}
    b, a = ctx.before_fs, ctx.after_fs
    problems = [f"added {p}" for p in sorted(set(a) - set(b)) if not (p in sidecars and a[p].get("size", 1) == 0)]
    problems += [f"removed {p}" for p in sorted(set(b) - set(a))]
    for p in sorted(set(a) & set(b)):
        changed = sorted(k for k in set(a[p]) | set(b[p]) if a[p].get(k) != b[p].get(k))
        if changed and not (p == db and set(changed) <= {"sha256", "size"}):
            problems.append(f"modified {p} ({', '.join(changed)})")
    if problems:
        return Check(False, observed="; ".join(problems), evidence={"problems": problems})
    return Check(True, observed=f"{len(b)} workspace entries unchanged")


@assertion("RU-007", kind="integrity", expected="final database passes PRAGMA integrity_check")
def integrity(ctx):
    result = ctx.after_db.integrity()
    assert result == "ok", f"integrity_check: {result}"
    return Check(True, observed="ok")
