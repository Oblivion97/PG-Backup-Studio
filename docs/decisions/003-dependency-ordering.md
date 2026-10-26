# 003 — Object ordering: fixed phases plus pg_depend-driven deferral

_Date: 2026-10-25 · Status: proposed (needs user approval with the Step 1 plan)_

## Context
The spec fixes 16 phases. Fixed phases alone break when an object needs something from a later phase:
a function taking a table row type (`app.customer_label(app.customers)`), a `BEGIN ATOMIC` function
whose body references tables, a CHECK, index or column DEFAULT that calls such a function, a domain
check calling a function. A full pg_dump-style global sort would discard the spec's phases (data
between DDL and constraints), which are needed for fast loads.

## Decision
1. Every object gets a key and a default phase (spec order). `late` = phase 13.
2. Edges come from one `pg_depend` query (`deptype IN ('n','a')`), with references resolved:
   row type (`pg_type.typrelid`) → relation; array/multirange → element/range type; view `_RETURN`
   rule → view; `pg_attrdef` → column default; domain `pg_constraint` → domain check; objects owned
   by extensions or in catalogs we do not dump → ignored. Self edges, sequence OWNED BY (`'a'` from
   sequence to table) and identity (`'i'`) edges are dropped. `pg_inherits` adds parent edges.
3. Walk objects in topological order. If an object depends on an object with a later effective
   phase, it is deferred to `late` when deferrable (functions, composites, domain checks — emitted as
   `ALTER DOMAIN ADD CONSTRAINT`, column defaults — `ALTER TABLE ... ALTER COLUMN SET DEFAULT`, CHECK
   constraints, indexes). Otherwise the backup records a warning naming the object.
4. Functions with `prosqlbody` (PG 14+), aggregates, views and matviews are `late` by default.
5. Every phase is emitted with `util.topo_sort`, ties by `(schema, name)` so files are deterministic.
   A cycle raises `CycleError` listing its members (should not happen for catalog-valid objects).
6. Table data order (phase 8) uses a separate FK graph (partitions mapped to their root); it matters
   only for data-only restores because full restores add constraints after data.

## Consequences
- The fixture orders correctly (verified edges on PG 18.4); exotic cases degrade to a visible warning
  instead of a silent restore failure.
- `build_plan` is pure and unit-testable with a fake catalog.
