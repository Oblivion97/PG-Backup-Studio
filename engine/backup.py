"""Backup: rebuild DDL from the catalogs and stream table data into one .sql / .sql.gz file.

`build_plan` is pure: it turns a `Catalog` into ordered `PlanItem`s (fixed phases from the
spec plus pg_depend-driven deferral, see docs/decisions/003), applying per-table data options
(full / latest N / schema only) and the NOT VALID rule for foreign keys (decision 004).
`run_backup` checks the login's read rights, opens one REPEATABLE READ READ ONLY transaction,
takes ACCESS SHARE locks on every table under `lock_timeout` before the snapshot, reads the
catalog, writes the plan to `<file>.part` and renames it on success; the part file is removed
on failure or cancel. Nothing is written to the source database.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime

import psycopg
from psycopg import errors, sql

from . import catalog
from .catalog import Catalog
from .db import cancel_on_event, connect, require_supported
from .precheck import (TableOption, check_permissions, display_ident, options_by_root, parse_table_options,
                       read_set, scaled_estimate)
from .util import (BackupBlockedError, BackupError, Cancelled, CycleError, EngineError, LockTimeoutError,
                   check_cancel, ident, lit, qname, stmt, topo_sort)

log = logging.getLogger(__name__)

LATE = 13
WRITE_BATCH_BYTES = 256 * 1024
PROGRESS_INTERVAL = 0.25
MODES = ("full", "schema", "data")
MAX_LOCK_TIMEOUT_S = 3600
MIN_SPEED_LIMIT_MB_S = 0.1
_FK_ALREADY_RELAXED = re.compile(r"\sNOT (?:VALID|ENFORCED)$")

HEADER_SETTINGS = (
    "SET statement_timeout = 0;\n"
    "SET lock_timeout = 0;\n"
    "SET idle_in_transaction_session_timeout = 0;\n"
    "SET client_encoding = 'UTF8';\n"
    "SET standard_conforming_strings = on;\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n"
    "SET check_function_bodies = false;\n"
    "SET xmloption = content;\n"
    "SET client_min_messages = warning;\n"
    "SET row_security = off;\n"
)

SESSION_SETTINGS = (
    "SET search_path = pg_catalog",
    "SET client_encoding = 'UTF8'",
    "SET statement_timeout = 0",
    "SET idle_in_transaction_session_timeout = 0",
    "SET row_security = off",
    "SET DateStyle = ISO",
    "SET IntervalStyle = postgres",
    "SET extra_float_digits = 3",
)

_POLICY_CMDS = {"*": "ALL", "r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}
_TRIGGER_STATE = {"D": "DISABLE TRIGGER", "R": "ENABLE REPLICA TRIGGER", "A": "ENABLE ALWAYS TRIGGER"}
_RULE_STATE = {"D": "DISABLE RULE", "R": "ENABLE REPLICA RULE", "A": "ENABLE ALWAYS RULE"}
_RESTORE_TRIGGER = {"O": "ENABLE TRIGGER", "R": "ENABLE REPLICA TRIGGER", "A": "ENABLE ALWAYS TRIGGER"}
_REL_KEYWORD = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE"}
# Every privilege of an object kind (acldefault for the owner); MAINTAIN is added on PG 17+.
_ALL_PRIVILEGES = {
    "TABLE": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}),
    "SEQUENCE": frozenset({"SELECT", "UPDATE", "USAGE"}),
    "SCHEMA": frozenset({"USAGE", "CREATE"}),
    "FUNCTION": frozenset({"EXECUTE"}),
    "PROCEDURE": frozenset({"EXECUTE"}),
    "TYPE": frozenset({"USAGE"}),
    "DOMAIN": frozenset({"USAGE"}),
}
_SIMPLE_OPTION = re.compile(r"[\w.\-]+")
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class BackupOptions:
    """What to back up and where. `schemas=None` means every user schema.

    `lock_timeout_s`: seconds to wait for a table lock (0 = wait forever). `speed_limit_mb_s`:
    MiB/s of COPY data (None = no limit). `tables`: TableOption dicts (only non-full ones matter).
    """
    mode: str = "full"
    schemas: list[str] | None = None
    privileges: bool = True
    gzip: bool = False
    out_dir: str = "~/pg_backups"
    filename: str | None = None
    lock_timeout_s: float = 10
    speed_limit_mb_s: float | None = None
    tables: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None) -> "BackupOptions":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def to_dict(self) -> dict:
        """Plain JSON-ready dict; only non-full table options are kept."""
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["schemas"] = list(self.schemas) if self.schemas is not None else None
        out["tables"] = [o.to_dict() for o in self.table_options() if o.data != "full"]
        return out

    def to_profile(self, saved_at: datetime | None = None) -> dict:
        """Entry for `backup_profiles.json` (ARCHITECTURE 9.8): the options without the file name."""
        options = self.to_dict()
        options.pop("filename")
        return {"saved_at": (saved_at or datetime.now().astimezone()).isoformat(timespec="seconds"),
                "options": options}

    def table_options(self) -> list[TableOption]:
        return parse_table_options(self.tables)

    @classmethod
    def from_value(cls, value: "BackupOptions | dict | None") -> "BackupOptions":
        opts = value if isinstance(value, BackupOptions) else cls.from_dict(value)
        opts.validate()
        return opts

    def validate(self) -> None:
        """Refuse an unknown mode, an empty schema selection (None means every schema), out-of-range
        safety options and invalid or repeated table options."""
        if self.mode not in MODES:
            raise BackupError(f"Unknown backup mode {self.mode!r}; use full, schema or data.")
        if self.schemas is not None and not self.schemas:
            raise BackupError("No schemas selected; choose at least one schema to back up.")
        if not _number_between(self.lock_timeout_s, 0, MAX_LOCK_TIMEOUT_S):
            raise BackupError(f"The lock timeout must be between 0 and {MAX_LOCK_TIMEOUT_S} seconds "
                              f"(0 waits for ever).")
        if self.speed_limit_mb_s is not None and not _number_between(self.speed_limit_mb_s, MIN_SPEED_LIMIT_MB_S,
                                                                     float("inf")):
            raise BackupError(f"The speed limit must be at least {MIN_SPEED_LIMIT_MB_S} MB/s, or empty for no limit.")
        if not isinstance(self.tables, (list, tuple)):
            raise BackupError("Table options must be a list.")
        self.table_options()


def _number_between(value, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


@dataclass
class CopySpec:
    """One table's data: COPY target plus optional statements around it (data-only trigger guard).

    `source` is the server-side COPY query when it is not a plain `COPY table` ("latest N").
    """
    schema: str
    name: str
    qname: str
    columns: list[str]
    est_rows: int = 0
    size: int = 0
    before: str = ""
    after: str = ""
    source: sql.Composable | None = None
    option: TableOption | None = None


@dataclass
class PartialInfo:
    """What makes a backup partial: per-table options in effect and the foreign keys relaxed for it."""
    partial: bool = False
    tables: list[dict] = field(default_factory=list)
    not_valid_fks: list[str] = field(default_factory=list)
    skipped_fks: list[dict] = field(default_factory=list)


@dataclass
class PlanItem:
    phase: int
    label_kind: str
    label_name: str
    sql: str | None = None
    copy: CopySpec | None = None


@dataclass
class _Node:
    key: tuple
    phase: int
    sort: tuple
    kind: str
    name: str
    render: Callable[[], str]
    deferrable: bool = False
    deps: set = field(default_factory=set)


def _one_line(text: str) -> str:
    """Collapse whitespace (including newlines in quoted names) so a comment stays on one line."""
    return " ".join(str(text).split())


def _options_clause(options: list[str] | None, prefix: str = "") -> list[str]:
    """reloptions entries as `name=value`, quoting values that are not simple words."""
    out = []
    for opt in options or []:
        name, _, value = opt.partition("=")
        out.append(f"{prefix}{name}={value if _SIMPLE_OPTION.fullmatch(value) else lit(value)}")
    return out


class _PlanBuilder:
    """Turns a Catalog into ordered PlanItems for one backup mode."""

    def __init__(self, cat: Catalog, opts: BackupOptions):
        self.cat = cat
        self.opts = opts
        self.warnings: list[str] = []
        self.nodes: dict[tuple, _Node] = {}
        self.eff: dict[tuple, int] = {}
        self.selected = {s["name"] for s in cat.schemas}
        self.comments = {(c["cat"], c["oid"], c["subid"]): c["description"] for c in cat.comments}
        self.acls = {(a["cat"], a["oid"], a["subid"]): a["entries"] for a in cat.acls}
        self.tables = {t["oid"]: t for t in cat.tables}
        self.columns = defaultdict(list)
        for c in cat.columns:
            self.columns[c["relid"]].append(c)
        self.parents = defaultdict(list)
        for i in cat.inherits:
            self.parents[i["relid"]].append(i)
        self.identity_seq = {(s["identity_of"], s["identity_attnum"]): s
                             for s in cat.sequences if s["identity_of"]}
        self.triggers = defaultdict(list)
        for g in cat.triggers:
            self.triggers[g["relid"]].append(g)
        self.alias = {(a["cat"], a["oid"]): (a["target_cat"], a["target_oid"]) for a in cat.aliases}
        self.options: dict[int, TableOption] = {}
        self.fk_action: dict[int, str] = {}
        self.info = PartialInfo()

    # ---------- shared pieces: owner, grants, comments ----------

    def _owner(self, keyword: str, name: str, owner: str) -> str:
        return stmt(f"ALTER {keyword} {name} OWNER TO {ident(owner)}") if self.opts.privileges else ""

    def _acl(self, key: tuple, keyword: str, name: str, owner: str) -> str:
        entries = self.acls.get(key)
        if entries is None or not self.opts.privileges:
            return ""
        out = [stmt(f"REVOKE ALL ON {keyword} {name} FROM PUBLIC"),
               stmt(f"REVOKE ALL ON {keyword} {name} FROM {ident(owner)}")]
        everything = self._all_privileges(keyword)
        for (grantee, grantable), privs in self._group_grants(entries):
            target = "PUBLIC" if grantee is None else ident(grantee)
            listed = "ALL" if set(privs) == everything else ", ".join(privs)
            out.append(stmt(f"GRANT {listed} ON {keyword} {name} TO {target}"
                            + (" WITH GRANT OPTION" if grantable else "")))
        return "".join(out)

    def _all_privileges(self, keyword: str) -> frozenset:
        """Full privilege set of an object kind on the source server.

        A grantee holding all of them gets `GRANT ALL`, so a PG 17+ backup (MAINTAIN) still
        restores owner grants on PG 12-16 (decision 004, Option A).
        """
        privs = _ALL_PRIVILEGES.get(keyword, frozenset())
        return privs | {"MAINTAIN"} if keyword == "TABLE" and self.cat.version_num >= 170000 else privs

    def _column_acls(self, relid: int, table: str) -> str:
        if not self.opts.privileges:
            return ""
        out = []
        for col in self.columns.get(relid, []):
            entries = self.acls.get(("pg_class", relid, col["num"]))
            for (grantee, grantable), privs in self._group_grants(entries or []):
                target = "PUBLIC" if grantee is None else ident(grantee)
                cols = ", ".join(f"{p} ({ident(col['name'])})" for p in privs)
                out.append(stmt(f"GRANT {cols} ON TABLE {table} TO {target}"
                                + (" WITH GRANT OPTION" if grantable else "")))
        return "".join(out)

    @staticmethod
    def _group_grants(entries: list[dict]) -> list[tuple[tuple, list[str]]]:
        groups: dict[tuple, list[str]] = {}
        for e in entries:
            groups.setdefault((e["grantee"], e["grantable"]), []).append(e["priv"])
        return list(groups.items())

    def _comment(self, key: tuple, target: str) -> str:
        text = self.comments.get(key)
        return stmt(f"COMMENT ON {target} IS {lit(text)}") if text is not None else ""

    def _column_comments(self, relid: int, rel: str) -> str:
        return "".join(self._comment(("pg_class", relid, c["num"]), f"COLUMN {rel}.{ident(c['name'])}")
                       for c in self.columns.get(relid, []))

    # ---------- nodes ----------

    def _add(self, key: tuple, phase: int, sort: tuple, kind: str, name: str,
             render: Callable[[], str], deferrable: bool = False) -> None:
        self.nodes[key] = _Node(key, phase, sort, kind, name, render, deferrable)

    def add_nodes(self) -> None:
        for s in self.cat.schemas:
            self._add(("pg_namespace", s["oid"]), 1, (0, s["name"]), "SCHEMA", ident(s["name"]),
                      lambda s=s: self._schema_sql(s))
        for e in self.cat.extensions:
            self._add(("pg_extension", e["oid"]), 2, (0, e["name"]), "EXTENSION", ident(e["name"]),
                      lambda e=e: self._extension_sql(e))
        self._add_types()
        for s in self.cat.sequences:
            if not s["identity_of"]:
                self._add(("pg_class", s["oid"]), 4, (0, s["schema"], s["name"]), "SEQUENCE", s["qname"],
                          lambda s=s: self._sequence_sql(s))
                if s["owned_by"]:
                    self._add(("owned_by", s["oid"]), 7, (0, s["schema"], s["name"]), "SEQUENCE OWNED BY",
                              s["qname"], lambda s=s: stmt(f"ALTER SEQUENCE {s['qname']} OWNED BY {s['owned_by']}"))
        for f in self.cat.functions:
            kind = "PROCEDURE" if f["kind"] == "p" else "FUNCTION"
            self._add(("pg_proc", f["oid"]), LATE if f["sqlbody"] else 5, (0, f["schema"], f["name"], f["sig"]),
                      kind, f["sig"], lambda f=f, kind=kind: self._function_sql(f, kind), deferrable=True)
        self._add_aggregates()
        self._add_tables()
        self._add_constraints()
        self._add_indexes()
        for v in self.cat.views:
            self._add(("pg_class", v["oid"]), LATE, (0, v["schema"], v["name"]),
                      "VIEW" if v["kind"] == "v" else "MATERIALIZED VIEW", v["qname"], lambda v=v: self._view_sql(v))
        self._add_triggers_rules_policies()

    def _add_types(self) -> None:
        order = {"e": 0, "d": 1, "r": 2, "c": 3}
        for t in self.cat.types:
            kind = "DOMAIN" if t["kind"] == "d" else "TYPE"
            self._add(("pg_type", t["oid"]), 3, (order[t["kind"]], t["schema"], t["name"], ""), kind, t["qname"],
                      lambda t=t: self._type_sql(t), deferrable=t["kind"] == "c")
        domains = {t["oid"]: t for t in self.cat.types if t["kind"] == "d"}
        for k in self.cat.constraints:
            if k["typid"] in domains:
                d = domains[k["typid"]]
                self._add(("pg_constraint", k["oid"]), 3, (1, d["schema"], d["name"], k["name"]),
                          "DOMAIN CONSTRAINT", f"{k['name']} ON {k['domain_qname']}",
                          lambda k=k: stmt(f"ALTER DOMAIN {k['domain_qname']} ADD CONSTRAINT {ident(k['name'])} "
                                           f"{k['def']}")
                          + self._comment(("pg_constraint", k["oid"], 0),
                                          f"CONSTRAINT {ident(k['name'])} ON DOMAIN {k['domain_qname']}"),
                          deferrable=True)

    def _add_aggregates(self) -> None:
        for a in self.cat.aggregates:
            if a["aggkind"] != "n" or a["moving"]:
                what = "ordered-set/hypothetical-set" if a["aggkind"] != "n" else "moving-aggregate"
                self.warnings.append(f"Aggregate {a['sig']} skipped: {what} aggregates are not supported.")
                continue
            self._add(("pg_proc", a["oid"]), LATE, (0, a["schema"], a["name"], a["sig"]), "AGGREGATE", a["sig"],
                      lambda a=a: self._aggregate_sql(a))

    def _add_tables(self) -> None:
        for t in self.cat.tables:
            self._add(("pg_class", t["oid"]), 6, (0, t["schema"], t["name"]), "TABLE", t["qname"],
                      lambda t=t: self._table_sql(t))
            for c in self.columns.get(t["oid"], []):
                if c["def_oid"] is None:
                    continue
                if c["generated"]:
                    self.alias[("pg_attrdef", c["def_oid"])] = ("pg_class", t["oid"])
                    continue
                self._add(("pg_attrdef", c["def_oid"]), 6, (1, t["schema"], t["name"], c["name"]), "DEFAULT",
                          f"{t['qname']}.{ident(c['name'])}", lambda t=t, c=c: self._default_sql(t, c),
                          deferrable=True)
            if t["rls"] or t["force_rls"]:
                self._add(("rls", t["oid"]), 15, (2, t["schema"], t["name"], ""), "ROW SECURITY", t["qname"],
                          lambda t=t: self._rls_sql(t))

    def _add_constraints(self) -> None:
        for k in self.cat.constraints:
            if k["relid"] not in self.tables:
                continue
            fk = k["kind"] == "f"
            kind = "FK CONSTRAINT" if fk else "CHECK CONSTRAINT" if k["kind"] == "c" else "CONSTRAINT"
            t = self.tables[k["relid"]]
            self._add(("pg_constraint", k["oid"]), 12 if fk else 10, (0, t["schema"], t["name"], k["name"]), kind,
                      f"{k['name']} ON {k['table_qname']}", lambda k=k: self._constraint_sql(k),
                      deferrable=k["kind"] in ("c", "f"))

    def _add_indexes(self) -> None:
        for i in self.cat.indexes:
            phase = 14 if i["table_kind"] == "m" else 11
            self._add(("pg_class", i["oid"]), phase, (0, i["schema"], i["name"]), "INDEX",
                      qname(i["schema"], i["name"]), lambda i=i: self._index_sql(i), deferrable=True)
        for s in self.cat.statistics:
            self._add(("pg_statistic_ext", s["oid"]), 11, (1, s["schema"], s["name"]), "STATISTICS", s["qname"],
                      lambda s=s: stmt(s["def"]) + self._owner("STATISTICS", s["qname"], s["owner"])
                      + self._comment(("pg_statistic_ext", s["oid"], 0), f"STATISTICS {s['qname']}"),
                      deferrable=True)

    def _add_triggers_rules_policies(self) -> None:
        for g in self.cat.triggers:
            if not g["is_clone"]:
                self._add(("pg_trigger", g["oid"]), 15, (0, g["schema"], g["table_qname"], g["name"]), "TRIGGER",
                          f"{g['name']} ON {g['table_qname']}", lambda g=g: self._trigger_sql(g))
        for r in self.cat.rules:
            self._add(("pg_rewrite", r["oid"]), 15, (1, r["schema"], r["table_qname"], r["name"]), "RULE",
                      f"{r['name']} ON {r['table_qname']}", lambda r=r: self._rule_sql(r))
        for p in self.cat.policies:
            self._add(("pg_policy", p["oid"]), 15, (3, p["schema"], p["table_qname"], p["name"]), "POLICY",
                      f"{p['name']} ON {p['table_qname']}", lambda p=p: self._policy_sql(p))

    # ---------- dependencies and phases ----------

    def _resolve(self, cat: str, oid: int) -> tuple | None:
        key = (cat, oid)
        for _ in range(4):
            nxt = self.alias.get(key)
            if nxt is None or nxt == key:
                break
            key = nxt
        return key if key in self.nodes else None

    def add_dependencies(self) -> None:
        for i in self.cat.inherits:
            src, ref = ("pg_class", i["relid"]), ("pg_class", i["parent"])
            if src in self.nodes and ref in self.nodes:
                self.nodes[src].deps.add(ref)
        for d in self.cat.dependencies:
            src = self._resolve(d["cat"], d["objid"])
            if src is None:
                continue
            ref = self._resolve(d["refcat"], d["refobjid"])
            if ref is None:
                self._note_outside_ref(src, d)
                continue
            if ref == src or (d["deptype"] == "a" and self.nodes[src].kind == "SEQUENCE"):
                continue
            self.nodes[src].deps.add(ref)

    def _note_outside_ref(self, src: tuple, d: dict) -> None:
        schema = d["ref_schema"]
        if (schema and schema not in self.selected and not schema.startswith("pg_")
                and schema != "information_schema" and not d["ref_in_extension"]):
            msg = (f"{self.nodes[src].kind} {self.nodes[src].name} depends on {d['ref_identity']} in schema "
                   f"{schema}, which is not in the backup.")
            if msg not in self.warnings:
                self.warnings.append(msg)

    def assign_phases(self, collect_warnings: bool) -> None:
        """Effective phase per node: deferrable objects needing something later move to LATE."""
        order = topo_sort(self.nodes, {k: n.deps for k, n in self.nodes.items()},
                          key=lambda k: (self.nodes[k].phase, self.nodes[k].sort))
        for k in order:
            n = self.nodes[k]
            eff = n.phase
            later = max((self.eff[d] for d in n.deps if d in self.eff), default=0)
            if later > eff:
                if n.deferrable and later <= LATE:
                    eff = LATE
                elif collect_warnings:
                    self.warnings.append(f"{n.kind} {n.name} depends on an object created later; "
                                         f"it may not restore.")
            self.eff[k] = eff

    def deferred(self, key: tuple) -> bool:
        return key in self.eff and self.eff[key] != self.nodes[key].phase

    def node_items(self) -> dict[int, list[PlanItem]]:
        by_phase = defaultdict(list)
        for k, phase in self.eff.items():
            by_phase[phase].append(k)
        deps = {k: n.deps for k, n in self.nodes.items()}
        items = defaultdict(list)
        for phase, keys in by_phase.items():
            sort = ((lambda k: self.nodes[k].sort[1:] + self.nodes[k].sort[:1]) if phase == LATE
                    else (lambda k: self.nodes[k].sort))
            for k in topo_sort(keys, deps, key=sort):
                n = self.nodes[k]
                text = n.render()
                if text:
                    items[phase].append(PlanItem(phase, n.kind, n.name, text))
        return items

    # ---------- per-table data options (Step 2) ----------

    def resolve_options(self) -> None:
        """Apply the non-full table options (never in schema mode) and relax FKs for them.

        Options naming no root table of the backup are ignored with a warning; a "latest N"
        option whose ordering column is gone is refused.
        """
        if self.opts.mode == "schema":
            return
        wanted = self.opts.table_options()
        rels = [{"oid": t["oid"], "schema": t["schema"], "name": t["name"], "kind": t["kind"],
                 "root_oid": self._root(t["oid"])} for t in self.cat.tables]
        matched = options_by_root(rels, wanted)
        for o in wanted:
            if o not in matched.values():
                self.warnings.append(f"Data option for {o.name} ignored: the table is not in the backup "
                                     f"(missing, in a schema that is not selected, or a partition).")
        self.options = {oid: o for oid, o in matched.items() if o.data != "full"}
        for oid, o in sorted(self.options.items(), key=lambda item: item[1].key):
            self.info.tables.append(self._partial_entry(self.tables[oid], o))
        self.info.partial = bool(self.options)
        if self.info.partial and self.opts.mode == "data":
            self.warnings.append("This data-only backup is partial: restoring it into a schema with valid foreign "
                                 "keys fails for rows whose parent rows were left out.")
        if self.opts.mode == "full":
            self._relax_foreign_keys()

    def _partial_entry(self, t: dict, o: TableOption) -> dict:
        if o.data == "latest" and not any(c["name"] == o.order_by for c in self.columns.get(t["oid"], [])):
            raise BackupError(f"Table {t['qname']}: the ordering column {display_ident(o.order_by)} for "
                              f"\"latest N rows\" no longer exists; choose another column.")
        latest = o.data == "latest"
        return {"table": t["qname"], "data": o.data, "order_by": o.order_by if latest else None,
                "limit": o.limit if latest else None}

    def _data_option(self, oid: int) -> str:
        opt = self.options.get(self._root(oid))
        return opt.data if opt else "full"

    def _relax_foreign_keys(self) -> None:
        """Decision 004: an FK becomes NOT VALID when the table it references is not Full and its own
        table has data; on PG 12-17 such an FK of a partitioned table is left out instead."""
        for k in self.cat.constraints:
            if k["kind"] != "f" or k["relid"] not in self.tables:
                continue
            if self._data_option(k["frelid"]) == "full" or self._data_option(k["relid"]) == "schema":
                continue
            if _FK_ALREADY_RELAXED.search(k["def"].rstrip()):
                continue
            label = f"{display_ident(k['name'])} ON {k['table_qname']}"
            if self.tables[k["relid"]]["kind"] == "p" and self.cat.version_num < 180000:
                self.fk_action[k["oid"]] = "skip"
                statement = stmt(f"ALTER TABLE {k['table_qname']} ADD CONSTRAINT {ident(k['name'])} {k['def']}")
                self.info.skipped_fks.append({"constraint": k["name"], "table": k["table_qname"],
                                              "sql": statement.rstrip("\n")})
                self.warnings.append(f"Foreign key {label} was left out: before PostgreSQL 18 it cannot be added "
                                     f"as NOT VALID to a partitioned table. The file header has the statement "
                                     f"to run after the restore.")
            else:
                self.fk_action[k["oid"]] = "not_valid"
                self.info.not_valid_fks.append(label)

    # ---------- table data, sequence values, matview refresh ----------

    def _root(self, oid: int) -> int:
        seen = set()
        while self.tables.get(oid, {}).get("part_parent") and oid not in seen:
            seen.add(oid)
            oid = self.tables[oid]["part_parent"]
        return oid

    def data_items(self) -> list[PlanItem]:
        leaves = sorted((t for t in self.cat.tables if t["kind"] == "r"), key=lambda t: (t["schema"], t["name"]))
        groups = defaultdict(list)
        for t in leaves:
            groups[self._root(t["oid"])].append(t)
        deps = defaultdict(set)
        for k in self.cat.constraints:
            if k["kind"] == "f" and k["relid"] in self.tables:
                src, ref = self._root(k["relid"]), self._root(k["frelid"])
                if src != ref:
                    deps[src].add(ref)
        names = {oid: (self.tables[oid]["schema"], self.tables[oid]["name"]) for oid in groups}
        try:
            order = topo_sort(list(groups), deps, key=lambda o: names[o])
        except CycleError:
            self.warnings.append("Foreign keys between tables form a cycle; table data is written in name order, "
                                 "so a data-only restore may hit foreign key errors.")
            order = sorted(groups, key=lambda o: names[o])
        items = []
        for root in order:
            opt = self.options.get(root)
            if opt is None:
                items += [self._copy_item(t) for t in groups[root]]
            elif opt.data == "latest":
                items.append(self._latest_item(self.tables[root], opt, groups[root]))
        return items

    def _copy_item(self, t: dict) -> PlanItem:
        cols = [c["name"] for c in self.columns.get(t["oid"], []) if not c["generated"]]
        spec = CopySpec(t["schema"], t["name"], t["qname"], cols, max(int(t["reltuples"] or 0), 0),
                        int(t["size"] or 0))
        self._trigger_guard(spec, [t])
        return PlanItem(8, "TABLE DATA", t["qname"], None, spec)

    def _latest_item(self, root: dict, opt: TableOption, leaves: list[dict]) -> PlanItem:
        """Newest `limit` rows of a root table; a partitioned root is read (and restored) as a whole.

        `NULLS LAST` is left out when the column cannot hold NULLs: same rows, and a plain
        index on the column can serve the query.
        """
        columns = self.columns.get(root["oid"], [])
        cols = [c["name"] for c in columns if not c["generated"]]
        order_col = next(c for c in columns if c["name"] == opt.order_by)
        null_safe = order_col["notnull"] and order_col.get("nn_validated") is not False
        est = sum(max(int(t["reltuples"] or 0), 0) for t in leaves)
        unknown = any((t["reltuples"] or 0) < 0 for t in leaves)
        size, rows = scaled_estimate(sum(int(t["size"] or 0) for t in leaves), est, unknown, opt.limit)
        source = sql.SQL("COPY (SELECT {} FROM {}{} ORDER BY {} DESC{} LIMIT {}) TO STDOUT").format(
            sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL("ONLY " if root["kind"] == "r" else ""),
            sql.Identifier(root["schema"], root["name"]), sql.Identifier(opt.order_by),
            sql.SQL("" if null_safe else " NULLS LAST"), sql.Literal(opt.limit))
        spec = CopySpec(root["schema"], root["name"], root["qname"], cols, rows, size, source=source, option=opt)
        self._trigger_guard(spec, [root] + [t for t in leaves if t["oid"] != root["oid"]])
        return PlanItem(8, "TABLE DATA", root["qname"], None, spec)

    def _trigger_guard(self, spec: CopySpec, tables: list[dict]) -> None:
        """Data-only mode: disable user triggers around the COPY, then restore each trigger's exact state.

        With several tables (a partitioned root and its partitions) disabled triggers are
        disabled again explicitly, in case enabling a parent's trigger recursed to a partition.
        """
        if self.opts.mode != "data":
            return
        for t in tables:
            triggers = self.triggers.get(t["oid"])
            if not triggers:
                continue
            spec.before += stmt(f"ALTER TABLE {t['qname']} DISABLE TRIGGER USER")
            for g in triggers:
                if g["enabled"] in _RESTORE_TRIGGER:
                    spec.after += stmt(f"ALTER TABLE {t['qname']} {_RESTORE_TRIGGER[g['enabled']]} {ident(g['name'])}")
                elif len(tables) > 1:
                    spec.after += stmt(f"ALTER TABLE {t['qname']} DISABLE TRIGGER {ident(g['name'])}")

    def setval_items(self) -> list[PlanItem]:
        items = []
        for s in sorted(self.cat.sequences, key=lambda s: (s["schema"], s["name"])):
            if s["oid"] not in self.cat.sequence_values:
                self.warnings.append(f"Sequence {s['qname']}: its current value was not saved (the login cannot "
                                     f"read it and its table is schema only).")
                continue
            last, called = self.cat.sequence_values[s["oid"]]
            items.append(PlanItem(9, "SEQUENCE SET", s["qname"],
                                  stmt(f"SELECT pg_catalog.setval({lit(s['qname'])}, {lit(last)}, {lit(called)})")))
        return items

    def refresh_items(self) -> list[PlanItem]:
        mviews = [("pg_class", v["oid"]) for v in self.cat.views if v["kind"] == "m" and v["populated"]]
        deps = {k: n.deps for k, n in self.nodes.items()}
        return [PlanItem(LATE, "MATERIALIZED VIEW DATA", self.nodes[k].name,
                         stmt(f"REFRESH MATERIALIZED VIEW {self.nodes[k].name}"))
                for k in topo_sort(mviews, deps, key=lambda k: self.nodes[k].sort)]

    # ---------- DDL per object kind ----------

    def _schema_sql(self, s: dict) -> str:
        name = ident(s["name"])
        return (stmt(f"CREATE SCHEMA IF NOT EXISTS {name}") + self._owner("SCHEMA", name, s["owner"])
                + self._acl(("pg_namespace", s["oid"], 0), "SCHEMA", name, s["owner"])
                + self._comment(("pg_namespace", s["oid"], 0), f"SCHEMA {name}"))

    def _extension_sql(self, e: dict) -> str:
        out = "" if e["schema"] in self.selected else stmt(f"CREATE SCHEMA IF NOT EXISTS {ident(e['schema'])}")
        return (out + stmt(f"CREATE EXTENSION IF NOT EXISTS {ident(e['name'])} WITH SCHEMA {ident(e['schema'])}")
                + self._comment(("pg_extension", e["oid"], 0), f"EXTENSION {ident(e['name'])}"))

    def _type_sql(self, t: dict) -> str:
        q, kind = t["qname"], t["kind"]
        if kind == "e":
            ddl = f"CREATE TYPE {q} AS ENUM ({', '.join(lit(x) for x in t['labels'])})"
        elif kind == "d":
            ddl = f"CREATE DOMAIN {q} AS {t['base_type']}"
            ddl += f" COLLATE {t['collation']}" if t["collation"] else ""
            ddl += f" DEFAULT {t['default_expr']}" if t["default_expr"] is not None else ""
            ddl += " NOT NULL" if t["notnull"] else ""
        elif kind == "r":
            ddl = f"CREATE TYPE {q} AS RANGE (\n    subtype = {t['subtype']}"
            for opt in ("subtype_opclass", "collation", "canonical", "subtype_diff"):
                ddl += f",\n    {opt} = {t[opt]}" if t[opt] else ""
            ddl += f",\n    multirange_type_name = {t['multirange']}" if t["multirange"] else ""
            ddl += "\n)"
        else:
            attrs = ",\n".join(f"    {ident(a['name'])} {a['type']}" + (f" COLLATE {a['collation']}"
                                                                         if a["collation"] else "")
                               for a in t["attributes"] or [])
            ddl = f"CREATE TYPE {q} AS (\n{attrs}\n)"
        keyword = "DOMAIN" if kind == "d" else "TYPE"
        out = stmt(ddl) + self._owner(keyword, q, t["owner"]) + self._acl(("pg_type", t["oid"], 0), keyword, q,
                                                                         t["owner"])
        out += self._comment(("pg_type", t["oid"], 0), f"{keyword} {q}")
        if kind == "c":
            out += "".join(self._comment(("pg_class", t["typrelid"], n + 1), f"COLUMN {q}.{ident(a['name'])}")
                           for n, a in enumerate(t["attributes"] or []))
        return out

    @staticmethod
    def _sequence_options(s: dict) -> str:
        return (f"START WITH {s['start']} INCREMENT BY {s['increment']} MINVALUE {s['min']} MAXVALUE {s['max']}"
                f" CACHE {s['cache']}{' CYCLE' if s['cycle'] else ' NO CYCLE'}")

    def _sequence_sql(self, s: dict) -> str:
        unlogged = "UNLOGGED " if s["persistence"] == "u" and self.cat.version_num >= 150000 else ""
        q = s["qname"]
        return (stmt(f"CREATE {unlogged}SEQUENCE {q} AS {s['type']} {self._sequence_options(s)}")
                + self._owner("SEQUENCE", q, s["owner"]) + self._acl(("pg_class", s["oid"], 0), "SEQUENCE", q, s["owner"])
                + self._comment(("pg_class", s["oid"], 0), f"SEQUENCE {q}"))

    def _function_sql(self, f: dict, kind: str) -> str:
        return (stmt(f["def"]) + self._owner(kind, f["sig"], f["owner"])
                + self._acl(("pg_proc", f["oid"], 0), kind, f["sig"], f["owner"])
                + self._comment(("pg_proc", f["oid"], 0), f"{kind} {f['sig']}"))

    def _aggregate_sql(self, a: dict) -> str:
        opts = [f"SFUNC = {a['sfunc']}", f"STYPE = {a['stype']}"]
        if a["sspace"]:
            opts.append(f"SSPACE = {a['sspace']}")
        if a["finalfunc"]:
            opts.append(f"FINALFUNC = {a['finalfunc']}")
            if a["finalextra"]:
                opts.append("FINALFUNC_EXTRA")
            if a["finalmodify"] in ("s", "w"):
                opts.append(f"FINALFUNC_MODIFY = {'SHAREABLE' if a['finalmodify'] == 's' else 'READ_WRITE'}")
        for opt in ("combinefunc", "serialfunc", "deserialfunc"):
            if a[opt]:
                opts.append(f"{opt.upper()} = {a[opt]}")
        if a["initcond"] is not None:
            opts.append(f"INITCOND = {lit(a['initcond'])}")
        if a["sortop"]:
            opts.append(f"SORTOP = {a['sortop']}")
        if a["parallel"] in ("s", "r"):
            opts.append(f"PARALLEL = {'SAFE' if a['parallel'] == 's' else 'RESTRICTED'}")
        ddl = f"CREATE AGGREGATE {a['qname']}({a['args']}) (\n    " + ",\n    ".join(opts) + "\n)"
        return (stmt(ddl) + self._owner("AGGREGATE", a["sig"], a["owner"])
                + self._acl(("pg_proc", a["oid"], 0), "FUNCTION", a["sig"], a["owner"])
                + self._comment(("pg_proc", a["oid"], 0), f"AGGREGATE {a['sig']}"))

    def _inline_default(self, t: dict, c: dict) -> bool:
        return (c["def_oid"] is not None and not c["generated"] and c["islocal"] and not t["is_partition"]
                and not self.deferred(("pg_attrdef", c["def_oid"])))

    def _column_sql(self, t: dict, c: dict) -> str:
        out = f"{ident(c['name'])} {c['type']}"
        if c["collation"]:
            out += f" COLLATE {c['collation']}"
        if c["identity"]:
            seq = self.identity_seq.get((t["oid"], c["num"]))
            how = "ALWAYS" if c["identity"] == "a" else "BY DEFAULT"
            out += f" GENERATED {how} AS IDENTITY"
            if seq:
                out += f" (SEQUENCE NAME {seq['qname']} {self._sequence_options(seq)})"
        elif c["generated"]:
            out += f" GENERATED ALWAYS AS ({c['def_expr']}) {'VIRTUAL' if c['generated'] == 'v' else 'STORED'}"
        elif self._inline_default(t, c):
            out += f" DEFAULT {c['def_expr']}"
        if c["notnull"]:
            if c["nn_name"] and c["nn_name"] != f"{t['name']}_{c['name']}_not_null":
                out += f" CONSTRAINT {ident(c['nn_name'])}"
            out += " NOT NULL" + (" NO INHERIT" if c["nn_noinherit"] else "")
        return out

    def _table_sql(self, t: dict) -> str:
        q = t["qname"]
        unlogged = "UNLOGGED " if t["persistence"] == "u" else ""
        if t["is_partition"]:
            ddl = f"CREATE {unlogged}TABLE {q} PARTITION OF {t['part_parent_qname']}\n{t['partbound']}"
        else:
            cols = [c for c in self.columns.get(t["oid"], []) if c["islocal"]]
            body = ",\n".join("    " + self._column_sql(t, c) for c in cols)
            ddl = f"CREATE {unlogged}TABLE {q} (\n{body}\n)"
            if self.parents.get(t["oid"]):
                ddl += f"\nINHERITS ({', '.join(p['parent_qname'] for p in self.parents[t['oid']])})"
        if t["partkey"]:
            ddl += f"\nPARTITION BY {t['partkey']}"
        if t["am"]:
            ddl += f"\nUSING {ident(t['am'])}"
        options = _options_clause(t["options"]) + _options_clause(t["toast_options"], "toast.")
        if options:
            ddl += f"\nWITH ({', '.join(options)})"
        out = [stmt(ddl)]
        if t["replident"] in ("f", "n"):
            out.append(stmt(f"ALTER TABLE {q} REPLICA IDENTITY {'FULL' if t['replident'] == 'f' else 'NOTHING'}"))
        out += [self._owner("TABLE", q, t["owner"]), self._acl(("pg_class", t["oid"], 0), "TABLE", q, t["owner"]),
                self._column_acls(t["oid"], q), self._comment(("pg_class", t["oid"], 0), f"TABLE {q}"),
                self._column_comments(t["oid"], q)]
        for c in self.columns.get(t["oid"], []):
            seq = self.identity_seq.get((t["oid"], c["num"])) if c["identity"] else None
            if seq:
                out.append(self._acl(("pg_class", seq["oid"], 0), "SEQUENCE", seq["qname"], seq["owner"]))
                out.append(self._comment(("pg_class", seq["oid"], 0), f"SEQUENCE {seq['qname']}"))
        return "".join(out)

    def _default_sql(self, t: dict, c: dict) -> str:
        if self._inline_default(t, c):
            return ""
        only = "ONLY " if not self.deferred(("pg_attrdef", c["def_oid"])) else ""
        return stmt(f"ALTER TABLE {only}{t['qname']} ALTER COLUMN {ident(c['name'])} SET DEFAULT {c['def_expr']}")

    def _cluster_replident(self, item: dict, table: str, keyword: str) -> str:
        if not (item["clustered"] or item["replident"]):
            return ""
        index = ident(item["index_name"] if "index_name" in item else item["name"])
        out = stmt(f"ALTER {keyword} {table} CLUSTER ON {index}") if item["clustered"] else ""
        if item["replident"]:
            out += stmt(f"ALTER TABLE {table} REPLICA IDENTITY USING INDEX {index}")
        return out

    def _constraint_sql(self, k: dict) -> str:
        action = self.fk_action.get(k["oid"])
        if action == "skip":
            return ""
        table = k["table_qname"]
        definition = k["def"] + (" NOT VALID" if action == "not_valid" else "")
        return (stmt(f"ALTER TABLE {table} ADD CONSTRAINT {ident(k['name'])} {definition}")
                + self._cluster_replident(k, table, "TABLE")
                + self._comment(("pg_constraint", k["oid"], 0), f"CONSTRAINT {ident(k['name'])} ON {table}"))

    def _index_sql(self, i: dict) -> str:
        keyword = "MATERIALIZED VIEW" if i["table_kind"] == "m" else "TABLE"
        return (stmt(i["def"].replace(" ON ONLY ", " ON ", 1))
                + self._cluster_replident(i, i["table_qname"], keyword)
                + self._comment(("pg_class", i["oid"], 0), f"INDEX {qname(i['schema'], i['name'])}"))

    def _view_sql(self, v: dict) -> str:
        q, matview = v["qname"], v["kind"] == "m"
        body = v["def"].strip().rstrip(";").rstrip()
        options = _options_clause(v["options"])
        with_clause = f" WITH ({', '.join(options)})" if options else ""
        if matview:
            using = f" USING {ident(v['am'])}" if v["am"] else ""
            data = "DATA" if v["populated"] and self.opts.mode == "full" else "NO DATA"
            ddl = f"CREATE MATERIALIZED VIEW {q}{using}{with_clause} AS\n{body}\nWITH {data}"
        else:
            ddl = f"CREATE VIEW {q}{with_clause} AS\n{body}"
        keyword = _REL_KEYWORD[v["kind"]]
        return (stmt(ddl) + self._owner(keyword, q, v["owner"])
                + self._acl(("pg_class", v["oid"], 0), "TABLE", q, v["owner"]) + self._column_acls(v["oid"], q)
                + self._comment(("pg_class", v["oid"], 0), f"{keyword} {q}") + self._column_comments(v["oid"], q))

    def _trigger_sql(self, g: dict) -> str:
        out = stmt(g["def"])
        if g["enabled"] in _TRIGGER_STATE:
            out += stmt(f"ALTER TABLE {g['table_qname']} {_TRIGGER_STATE[g['enabled']]} {ident(g['name'])}")
        return out + self._comment(("pg_trigger", g["oid"], 0), f"TRIGGER {ident(g['name'])} ON {g['table_qname']}")

    def _rule_sql(self, r: dict) -> str:
        out = stmt(r["def"])
        if r["enabled"] in _RULE_STATE:
            out += stmt(f"ALTER TABLE {r['table_qname']} {_RULE_STATE[r['enabled']]} {ident(r['name'])}")
        return out + self._comment(("pg_rewrite", r["oid"], 0), f"RULE {ident(r['name'])} ON {r['table_qname']}")

    def _rls_sql(self, t: dict) -> str:
        out = stmt(f"ALTER TABLE {t['qname']} ENABLE ROW LEVEL SECURITY") if t["rls"] else ""
        return out + (stmt(f"ALTER TABLE {t['qname']} FORCE ROW LEVEL SECURITY") if t["force_rls"] else "")

    def _policy_sql(self, p: dict) -> str:
        ddl = f"CREATE POLICY {ident(p['name'])} ON {p['table_qname']}"
        ddl += "" if p["permissive"] else " AS RESTRICTIVE"
        ddl += f" FOR {_POLICY_CMDS[p['cmd']]} TO {', '.join(p['roles'])}"
        ddl += f" USING ({p['qual']})" if p["qual"] is not None else ""
        ddl += f" WITH CHECK ({p['withcheck']})" if p["withcheck"] is not None else ""
        return stmt(ddl) + self._comment(("pg_policy", p["oid"], 0), f"POLICY {ident(p['name'])} ON {p['table_qname']}")


def build_plan(cat: Catalog, opts: BackupOptions | dict | None, warnings: list[str] | None = None,
               info: PartialInfo | None = None) -> list[PlanItem]:
    """Ordered plan for one backup mode; ordering/skip notes are appended to `warnings` and the
    partial-backup details (options in effect, relaxed FKs) are copied into `info`."""
    opts = BackupOptions.from_value(opts)
    b = _PlanBuilder(cat, opts)
    b.add_nodes()
    b.add_dependencies()
    b.assign_phases(collect_warnings=opts.mode != "data")
    if opts.mode == "data":
        b.warnings.clear()  # DDL ordering notes do not apply when no DDL is written
    b.resolve_options()
    phases: dict[int, list[PlanItem]] = defaultdict(list)
    if opts.mode != "data":
        phases.update(b.node_items())
    if opts.mode != "schema":
        phases[8] = b.data_items()
        phases[9] = b.setval_items()
    if opts.mode == "data":
        phases[LATE] = b.refresh_items()
    if warnings is not None:
        warnings.extend(b.warnings)
    if info is not None:
        vars(info).update(vars(b.info))
    return [item for phase in sorted(phases) for item in phases[phase]]


# ---------------------------------------------------------------- run_backup

class _Output:
    """Binary output file, optionally gzip-compressed; `size` is the on-disk byte count."""

    def __init__(self, path: str, compress: bool):
        self.raw = open(path, "wb")
        self.stream = (gzip.GzipFile(filename="", mode="wb", fileobj=self.raw, compresslevel=6)
                       if compress else self.raw)

    def write(self, data: bytes | bytearray | str) -> None:
        self.stream.write(data.encode("utf-8") if isinstance(data, str) else data)

    @property
    def size(self) -> int:
        return self.raw.tell()

    def close(self) -> None:
        try:
            if self.stream is not self.raw:
                self.stream.close()
        finally:
            self.raw.close()


class _Reporter:
    """Calls the progress callback safely and throttles high-frequency events."""

    def __init__(self, progress: Callable[[str, dict], None] | None):
        self.progress = progress
        self.last = 0.0

    def emit(self, event: str, data: dict) -> None:
        if self.progress is not None:
            try:
                self.progress(event, data)
            except Exception:  # noqa: BLE001 - a broken observer must not stop the backup
                log.exception("progress callback failed")

    def throttled(self, event: str, data: dict) -> None:
        now = time.monotonic()
        if now - self.last >= PROGRESS_INTERVAL:
            self.last = now
            self.emit(event, data)


def _reserve(path: str) -> bool:
    """Create `<path>.part` exclusively; False when it (or the final file) already exists.

    Exclusive create makes the name unique even when several backups start in the same second.
    """
    if os.path.exists(path):
        return False
    try:
        with open(path + ".part", "xb"):
            return True
    except FileExistsError:
        return False


def _target_path(opts: BackupOptions, dbname: str, partial: bool = False) -> str:
    """Choose the output file name and reserve its `.part` file (the caller removes it on failure)."""
    out_dir = os.path.abspath(os.path.expanduser(opts.out_dir))
    os.makedirs(out_dir, exist_ok=True)
    ext = ".sql.gz" if opts.gzip else ".sql"
    if opts.filename:
        path = os.path.join(out_dir, os.path.basename(opts.filename))
        if not _reserve(path):
            raise BackupError(f"The file {path} already exists (or another backup is writing it); "
                              f"choose another file name.")
        return path
    base = f"{_UNSAFE_FILENAME.sub('_', dbname)}_{datetime.now():%Y%m%d_%H%M%S}" + ("_partial" if partial else "")
    path, n = os.path.join(out_dir, base + ext), 2
    while not _reserve(path):
        path, n = os.path.join(out_dir, f"{base}_{n}{ext}"), n + 1
    return path


def _partial_lines(info: PartialInfo) -> list[str]:
    """Header block of a partial backup: each table's option and the relaxed foreign keys."""
    if not info.partial:
        return []
    lines = ["-- PARTIAL BACKUP: some tables do not contain all rows."]
    for t in info.tables:
        what = (f"latest {t['limit']} rows by {display_ident(t['order_by'])} (newest first)"
                if t["data"] == "latest" else "schema only (no data)")
        lines.append(f"--   {t['table']}: {what}")
    if info.not_valid_fks:
        lines.append("-- Foreign keys written as NOT VALID (check later: ALTER TABLE ... VALIDATE CONSTRAINT ...):")
        lines += [f"--   {name}" for name in info.not_valid_fks]
    if info.skipped_fks:
        lines.append("-- Foreign keys left out (before PostgreSQL 18 a partitioned table cannot get a NOT VALID "
                     "foreign key); add them after the restore once the data allows it:")
        lines += [f"--   {fk['sql']}" for fk in info.skipped_fks]
    return lines + ["--"]


def _header(dbname: str, conn: psycopg.Connection, opts: BackupOptions, schemas: list[str],
            warnings: list[str], info: PartialInfo | None = None) -> str:
    info_ = conn.info
    lines = ["--", "-- PG Backup Studio backup", f"-- Database: {dbname}",
             f"-- Server version: {info_.parameter_status('server_version')} ({info_.server_version})",
             f"-- Host: {info_.host}:{info_.port}", f"-- Created: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
             f"-- Mode: {opts.mode}", f"-- Schemas: {', '.join(ident(s) for s in schemas) or '(none)'}",
             f"-- Owners and grants: {'yes' if opts.privileges else 'no'}", "--"]
    not_included = [w for w in warnings if w.startswith("NOT INCLUDED")]
    others = [w for w in warnings if not w.startswith("NOT INCLUDED")]
    if not_included:
        lines += ["-- NOT INCLUDED (object kinds PG Backup Studio does not back up):"]
        lines += [f"--   {w.split(': ', 1)[1]}" for w in not_included] + ["--"]
    if others:
        lines += ["-- Warnings:"] + [f"--   {w}" for w in others] + ["--"]
    lines += _partial_lines(info or PartialInfo())
    return "\n".join(re.sub(r"[\r\n]+", " ", x) for x in lines) + "\n\n" + HEADER_SETTINGS


class _Throttle:
    """Keeps each table's COPY stream at or under `limit_mb_s` MiB/s by sleeping between chunks.

    Counts uncompressed COPY bytes (the load on the source); a Cancel ends a sleep at once.
    """

    def __init__(self, limit_mb_s: float | None, cancel: threading.Event | None):
        self.rate = limit_mb_s * 2 ** 20 if limit_mb_s else None
        self.cancel = cancel
        self.start: float | None = None

    def new_table(self) -> None:
        self.start = None

    def pace(self, copied: int) -> None:
        """Called after each flushed chunk with the table's bytes so far; the first call starts the clock."""
        if not self.rate:
            return
        now = time.monotonic()
        if self.start is None:
            self.start = now
        due = self.start + copied / self.rate
        while now < due:
            wait = min(due - now, 0.25)
            if self.cancel is not None:
                if self.cancel.wait(wait):
                    raise Cancelled("Cancelled by user")
            else:
                time.sleep(wait)
            now = time.monotonic()


class _BackupRun:
    """One backup: snapshot connection, locks, plan, output file and counters."""

    def __init__(self, server: dict, dbname: str, opts: BackupOptions, progress, cancel):
        self.server, self.dbname, self.opts = server, dbname, opts
        self.rep = _Reporter(progress)
        self.cancel = cancel
        self.throttle = _Throttle(opts.speed_limit_mb_s, cancel)
        self.locked: set[int] = set()
        self.rows = 0
        self.data_done = 0

    def run(self) -> dict:
        started = time.monotonic()
        conn = connect(self.server, self.dbname)
        part = None
        try:
            with cancel_on_event(conn, self.cancel):
                version = require_supported(conn)
                self.rep.emit("phase", {"name": "catalog"})
                skipped = self._lock_snapshot(conn)
                cat, plan, warnings, info = self._plan(conn, version, skipped)
                path = _target_path(self.opts, self.dbname, info.partial)
                part = path + ".part"
                self.rep.emit("file", {"path": path, "part": part})
                self._write(conn, part, plan, self._header(conn, cat, warnings, info))
            check_cancel(self.cancel)
            conn.commit()
            os.replace(part, path)
            part = None
        except Cancelled:
            raise
        except Exception as e:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled("Cancelled by user") from e
            if isinstance(e, EngineError):
                raise
            log.exception("backup failed")
            if isinstance(e, psycopg.Error):
                raise BackupError(f"Backup of {self.dbname} failed: {_message(e)}") from e
            if isinstance(e, OSError):
                raise BackupError(f"Could not write the backup file: {e.strerror or e}") from e
            raise
        finally:
            conn.close()
            if part and os.path.exists(part):
                os.remove(part)
        tables = sum(1 for i in plan if i.copy)
        self.rep.emit("phase", {"name": "done"})
        return {"file": path, "size": os.path.getsize(path), "mode": self.opts.mode,
                "schemas": [s["name"] for s in cat.schemas], "objects": len(plan) - tables, "tables": tables,
                "rows": self.rows, "duration_s": round(time.monotonic() - started, 3),
                "server_version": version, "warnings": warnings, "partial": info.partial,
                "partial_tables": info.tables, "not_valid_fks": info.not_valid_fks, "skipped_fks": info.skipped_fks,
                "lock_timeout_s": self.opts.lock_timeout_s, "speed_limit_mb_s": self.opts.speed_limit_mb_s}

    def _header(self, conn, cat: Catalog, warnings: list[str], info: PartialInfo) -> str:
        return _header(self.dbname, conn, self.opts, [s["name"] for s in cat.schemas], warnings, info)

    @staticmethod
    def _start_snapshot(conn: psycopg.Connection) -> None:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        for setting in SESSION_SETTINGS:
            conn.execute(setting)

    def _lock_timeout_sql(self) -> str:
        ms = 0 if not self.opts.lock_timeout_s else max(1, round(self.opts.lock_timeout_s * 1000))
        return f"SET lock_timeout = '{ms}ms'"

    def _lock_snapshot(self, conn: psycopg.Connection) -> set[int]:
        """Check read rights, open the snapshot transaction and lock every table before the snapshot.

        `SET` and `LOCK` take no snapshot, so the locks are held before the first query sees any
        data. A table dropped in between restarts the sequence once. Returns the oids of
        sequences whose value is not read: all of them in schema mode, else see `precheck.read_set`.
        """
        retries = 1
        while True:
            relations, reads = self._check_access(conn)
            self._start_snapshot(conn)
            conn.execute(self._lock_timeout_sql())
            try:
                self._lock_tables(conn, [r for r in relations if r["kind"] in ("r", "p") and r["can_lock"]])
                if self.opts.mode == "schema":
                    return {r["oid"] for r in relations if r["kind"] == "S"}
                return {s["oid"] for s in reads["skipped_sequences"]}
            except (errors.UndefinedTable, errors.InvalidSchemaName) as e:
                conn.rollback()
                if not retries:
                    raise BackupError("Tables or schemas were dropped or renamed while the backup was starting; "
                                      "try again.") from e
                retries -= 1

    def _check_access(self, conn: psycopg.Connection) -> tuple[list[dict], dict]:
        """Short read-only transaction (rolled back): what the backup reads and whether the login may."""
        conn.read_only = True
        for setting in SESSION_SETTINGS + (self._lock_timeout_sql(),):
            conn.execute(setting)
        relations = catalog.list_relations(conn, self.opts.schemas)
        reads = read_set(relations, self.opts.mode, self.opts.table_options())
        problems = check_permissions(conn, reads)
        conn.rollback()
        if problems:
            raise BackupBlockedError(problems)
        return relations, reads

    def _lock_tables(self, conn: psycopg.Connection, relations: list[dict]) -> None:
        self.locked = set()
        for r in relations:
            check_cancel(self.cancel)
            self._lock_one(conn, r["oid"], r["schema"], r["name"], r["qname"])

    def _lock_one(self, conn: psycopg.Connection, oid: int, schema: str, name: str, qname_: str) -> None:
        try:
            conn.execute(sql.SQL("LOCK TABLE ONLY {} IN ACCESS SHARE MODE").format(sql.Identifier(schema, name)))
        except errors.LockNotAvailable as e:
            raise LockTimeoutError(
                f"Table {qname_} is locked by another session (for example ALTER TABLE, TRUNCATE, VACUUM FULL or "
                f"REINDEX); gave up after {self.opts.lock_timeout_s:g} s. Nothing was written. Try again later or "
                f"raise the lock timeout.", qname_) from e
        except errors.OutOfMemory as e:
            raise BackupError("Too many tables to lock in one transaction: raise max_locks_per_transaction on the "
                              "server, or back up fewer schemas.") from e
        self.locked.add(oid)

    def _lock_new_tables(self, conn: psycopg.Connection, cat: Catalog) -> None:
        """Lock tables created between the access check and the snapshot (the login may SELECT them)."""
        new = [t for t in cat.tables if t["oid"] not in self.locked]
        if not new:
            return
        rows = conn.execute("SELECT o FROM unnest(%s::oid[]) o WHERE has_table_privilege(o, 'SELECT')",
                            ([t["oid"] for t in new],)).fetchall()
        readable = {r[0] for r in rows}
        for t in new:
            if t["oid"] in readable:
                self._lock_one(conn, t["oid"], t["schema"], t["name"], t["qname"])

    def _plan(self, conn, version: int, skipped: set[int]) -> tuple[Catalog, list[PlanItem], list[str], PartialInfo]:
        step = {"name": "schemas"}

        def on_step(name: str) -> None:
            step["name"] = name
            check_cancel(self.cancel)

        try:
            cat = catalog.load_all(conn, self.opts.schemas, version, on_step=on_step, skip_sequence_values=skipped)
            self._lock_new_tables(conn, cat)
        except errors.LockNotAvailable as e:
            raise LockTimeoutError(
                f"An object is locked by another session (while reading {step['name']}); gave up after "
                f"{self.opts.lock_timeout_s:g} s. Nothing was written. Try again later or raise the lock "
                f"timeout.") from e
        except psycopg.Error as e:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled("Cancelled by user") from e
            log.exception("catalog read failed")
            raise BackupError(f"Reading the catalog of {self.dbname} failed: {_message(e)}") from e
        warnings = list(cat.unsupported)
        info = PartialInfo()
        plan = build_plan(cat, self.opts, warnings, info)
        for w in warnings:
            self.rep.emit("warning", {"level": "warning", "msg": w})
        copies = [i.copy for i in plan if i.copy]
        self.rep.emit("totals", {"objects": len(plan) - len(copies), "tables": len(copies),
                                 "est_rows": sum(c.est_rows for c in copies),
                                 "data_bytes": sum(c.size for c in copies)})
        return cat, plan, warnings, info

    def _write(self, conn, part: str, plan: list[PlanItem], header: str) -> None:
        out = _Output(part, self.opts.gzip)
        try:
            out.write(header)
            self.rep.emit("phase", {"name": "write"})
            done = 0
            for item in plan:
                check_cancel(self.cancel)
                name = _one_line(item.label_name)
                out.write(f"\n-- Type: {item.label_kind}; Name: {name}\n")
                if item.copy:
                    self._copy(conn, out, item.copy)
                else:
                    out.write(item.sql)
                    done += 1
                    self.rep.emit("object", {"kind": item.label_kind, "name": name, "objects_done": done,
                                             "bytes_written": out.size})
        finally:
            out.close()

    @staticmethod
    def _copy_query(spec: CopySpec) -> sql.Composable:
        if spec.source is not None:
            return spec.source
        table = sql.Identifier(spec.schema, spec.name)
        if not spec.columns:
            return sql.SQL("COPY {} TO STDOUT").format(table)
        return sql.SQL("COPY {} ({}) TO STDOUT").format(table, sql.SQL(", ").join(map(sql.Identifier, spec.columns)))

    def _copy(self, conn: psycopg.Connection, out: _Output, spec: CopySpec) -> None:
        self.rep.emit("table_start", {"name": spec.qname, "est_rows": spec.est_rows, "size": spec.size})
        cols = ", ".join(ident(c) for c in spec.columns)
        target = f"{spec.qname} ({cols})" if cols else spec.qname
        out.write(spec.before + f"COPY {target} FROM stdin;\n")
        rows = copied = 0
        buf = bytearray()
        self.throttle.new_table()
        try:
            with conn.cursor() as cur, cur.copy(self._copy_query(spec)) as cp:
                for chunk in cp:
                    buf += chunk
                    if len(buf) >= WRITE_BATCH_BYTES:
                        rows, copied = self._flush(out, buf, spec, rows, copied)
                if buf:
                    rows, copied = self._flush(out, buf, spec, rows, copied)
        except psycopg.Error as e:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled("Cancelled by user") from e
            log.exception("COPY failed")
            raise BackupError(f"Table {spec.qname}: {_message(e)}") from e
        out.write("\\.\n" + spec.after)
        self.rows += rows
        self.rep.emit("table_done", {"name": spec.qname, "rows": rows, "bytes_written": out.size})

    def _flush(self, out: _Output, buf: bytearray, spec: CopySpec, rows: int, copied: int) -> tuple[int, int]:
        out.write(buf)
        rows += buf.count(b"\n")
        copied += len(buf)
        buf.clear()
        check_cancel(self.cancel)
        self.throttle.pace(copied)
        self.rep.throttled("table_progress", {"name": spec.qname, "rows": rows, "bytes_copied": copied,
                                              "bytes_written": out.size})
        return rows, copied


def _message(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    return (diag.message_primary if diag else None) or " ".join(str(e).split()) or type(e).__name__


def run_backup(server: dict, dbname: str, options: BackupOptions | dict | None = None,
               progress: Callable[[str, dict], None] | None = None,
               cancel: threading.Event | None = None) -> dict:
    """Back up one database into a single file; returns the result dict (ARCHITECTURE 8.1, 9.6).

    Raises `Cancelled` when `cancel` is set (also during a lock wait), `BackupBlockedError` when
    the login cannot read everything, `LockTimeoutError` when a table stays locked, and
    `BackupError`/`EngineError` with a clear message on other failures; the `.part` file is
    removed in every case.
    """
    return _BackupRun(server, dbname, BackupOptions.from_value(options), progress, cancel).run()
