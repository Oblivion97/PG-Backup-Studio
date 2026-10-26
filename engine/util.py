"""Pure helpers shared by the engine: errors, cancel check, SQL quoting, topological sort."""

from __future__ import annotations

import heapq
import math
import threading
from collections.abc import Callable, Hashable, Iterable, Mapping
from decimal import Decimal
from typing import Any, TypeVar

H = TypeVar("H", bound=Hashable)


class Cancelled(Exception):
    """Raised when a job's cancel event is set between objects or COPY chunks."""


class EngineError(Exception):
    """Base class for errors whose message is a clear sentence for the user."""


class BackupError(EngineError):
    """A backup could not be completed."""


class BackupBlockedError(BackupError):
    """The login cannot read everything the backup needs; `problems` lists each object (pre-check shape)."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        listed = " ".join(p["message"] for p in problems[:10])
        more = f" ({len(problems) - 10} more not shown.)" if len(problems) > 10 else ""
        super().__init__(f"The backup cannot start: {listed}{more} Exclude those schemas or set those tables "
                         f"to Schema only, or use a login with the missing rights.")


class LockTimeoutError(BackupError):
    """A table stayed locked by another session longer than the lock timeout; `relation` names it."""

    def __init__(self, message: str, relation: str | None = None):
        self.relation = relation
        super().__init__(message)


class RestoreError(EngineError):
    """A restore could not be completed (for example a truncated file)."""


class CycleError(EngineError):
    """Objects depend on each other in a circle; `nodes` lists the members left unsorted."""

    def __init__(self, nodes: list):
        self.nodes = nodes
        names = ", ".join(str(n) for n in nodes[:10])
        super().__init__(f"Circular dependency between objects: {names}")


def check_cancel(ev: threading.Event | None) -> None:
    """Raise `Cancelled` when the cancel event is set."""
    if ev is not None and ev.is_set():
        raise Cancelled("Cancelled by user")


def lit(v: Any) -> str:
    """Render a Python value as an SQL literal (standard_conforming_strings = on)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "'NaN'"
        if math.isinf(v):
            return "'Infinity'" if v > 0 else "'-Infinity'"
        return repr(v)
    if isinstance(v, Decimal):
        return str(v) if v.is_finite() else f"'{v}'"
    if isinstance(v, str):
        if "\x00" in v:
            raise ValueError("SQL string literals cannot contain NUL characters")
        return "'" + v.replace("'", "''") + "'"
    raise TypeError(f"cannot render {type(v).__name__} as an SQL literal")


def ident(name: str) -> str:
    """Quote an identifier: always double-quoted, embedded quotes doubled."""
    return '"' + name.replace('"', '""') + '"'


def qname(schema: str, name: str) -> str:
    """Schema-qualified, quoted name."""
    return f"{ident(schema)}.{ident(name)}"


def stmt(sql: str) -> str:
    """Normalise one statement: stripped, exactly one trailing `;`, then a newline."""
    body = sql.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    return body + ";\n"


def topo_sort(nodes: Iterable[H], deps: Mapping[H, Iterable[H]],
              key: Callable[[H], Any] = repr) -> list[H]:
    """Kahn's algorithm: dependencies first, ties broken by `key`.

    `deps[n]` lists what `n` depends on; dependencies outside `nodes` and self edges are
    ignored. Raises `CycleError` listing the nodes that could not be ordered.
    """
    order = list(dict.fromkeys(nodes))
    members = set(order)
    waiting = {n: 0 for n in order}
    users: dict[H, list[H]] = {n: [] for n in order}
    for n in order:
        for d in set(deps.get(n, ())):
            if d in members and d != n:
                waiting[n] += 1
                users[d].append(n)
    seq = 0
    heap = []
    for n in order:
        if waiting[n] == 0:
            heap.append((key(n), seq, n))
            seq += 1
    heapq.heapify(heap)
    result = []
    while heap:
        _, _, n = heapq.heappop(heap)
        result.append(n)
        for u in users[n]:
            waiting[u] -= 1
            if waiting[u] == 0:
                heapq.heappush(heap, (key(u), seq, u))
                seq += 1
    if len(result) != len(order):
        left = [n for n in order if waiting[n] > 0]
        raise CycleError(sorted(left, key=key))
    return result
