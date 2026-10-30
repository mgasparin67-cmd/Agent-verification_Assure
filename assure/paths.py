"""Path confinement and glob matching for the filesystem tools.

Confinement does not rely on string patterns alone: the requested path is joined to the sandbox
root, symlinks are resolved with realpath, and the result must stay under the resolved root.
Glob allowlists are then matched against the normalised relative path.

Limitation: there is a time-of-check/time-of-use window between resolution and open(). In the MVP
only the gateway writes to the workspace, so nothing can swap a symlink in between.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ResolvedPath:
    raw: str
    absolute: Path
    relative: str | None  # None when outside the root
    inside_root: bool


class PathError(ValueError):
    pass


def resolve(root: Path, raw: str) -> ResolvedPath:
    if not isinstance(raw, str) or raw == "":
        raise PathError("path must be a non-empty string")
    if "\x00" in raw:
        raise PathError("path contains NUL byte")
    root_real = Path(os.path.realpath(root))
    candidate = Path(raw) if os.path.isabs(raw) else root_real / raw
    absolute = Path(os.path.realpath(candidate))
    try:
        rel = absolute.relative_to(root_real).as_posix()
        return ResolvedPath(raw, absolute, "." if rel == "" else rel, True)
    except ValueError:
        return ResolvedPath(raw, absolute, None, False)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(path))


def matches_any(path: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if glob_match(path, p):
            return p
    return None
