# 002 — Grants and owners for roles missing on the restore target

_Date: 2026-10-25 · Status: proposed (needs user approval with the Step 1 plan)_

## Context
Roles are cluster-level and out of scope for backup. A backup with "owners & grants" on contains
`ALTER ... OWNER TO r`, `REVOKE ... FROM r` and `GRANT ... TO r`. Restoring onto another server where
`r` does not exist fails those statements with SQLSTATE `42704` ("role ... does not exist", verified
on PG 18.4 for GRANT, REVOKE, OWNER TO and CREATE POLICY ... TO). With "stop on error" or "single
transaction" the whole restore would fail on a cosmetic problem.

## Options
1. Treat as normal errors — simple, but cross-server restores in stop/single-transaction mode always fail.
2. Wrap each grant in a `DO` block that checks `pg_roles` — makes the file non-standard, hides the problem.
3. **Executor downgrades them to warnings** — chosen.

## Decision
- Statements starting with `GRANT`, `REVOKE`, or matching `ALTER ... OWNER TO`, that fail with
  SQLSTATE `42704`, are recorded in `result.warnings` (same shape as errors) and never stop the run,
  in any mode. Detection uses SQLSTATE + statement kind, not the message text (messages are localised).
- In single-transaction mode those statements run inside a savepoint so a failure does not abort the
  transaction.
- Objects whose owner is missing stay owned by the restoring user (reported in the warning).
- `CREATE POLICY ... TO missing_role` stays an **error**: a missing policy changes security behaviour.
- The restore result and UI show "N privilege statements skipped: role(s) X, Y not found".

## Consequences
- Cross-server restores succeed; the user sees exactly which roles to create and can re-run grants.
- The file stays plain SQL that `psql` could also run.
