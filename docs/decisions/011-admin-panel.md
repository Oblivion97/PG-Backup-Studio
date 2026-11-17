# 011 — Admin panel: sessions and roles & users

_Date: 2026-11-17 · Status: **built on branch `admin-panel`, waiting for the user's review** (user: "build standard …
first I will see it; if it works I will approve to merge and release"). Defaults chosen by the plan the user accepted._

1. **Per-server Admin tab** (`#/s/<id>/admin`) after Query, with two sub-tabs: **Sessions** and **Roles & users**.
   Sessions and roles belong to one PostgreSQL server, like Databases (Activity / History stay app-wide).
2. **Offer only what the saved login can do.** `GET …/admin/capabilities` reads superuser, CREATEROLE,
   pg_signal_backend and pg_read_all_stats. Per row the server also says `can_signal` (sessions) and `can_manage`
   (roles: superuser; or CREATEROLE on a non-superuser, non-`pg_` role, and from PostgreSQL 16 only with ADMIN OPTION
   on it). The UI greys out the rest with a reason; PostgreSQL stays the final judge and its refusals are shown.
3. **Sessions** from `pg_stat_activity` without the app's own asking connection. Stale = *idle in transaction* ≥ 5 min
   (holds locks, blocks VACUUM) or *idle* ≥ 30 min; both thresholds can be changed in the panel (per browser).
   Cancel query (`pg_cancel_backend`) or End session (`pg_terminate_backend`) for ticked rows, after a confirmation
   that lists them. Only client backends can be chosen; background processes are shown but never signalled; each pid
   is re-checked and reported (done / not allowed / already gone). "Select stale" only ticks; nothing ends without
   the confirmation. Auto refresh off / 5 s / 15 s, paused while a dialog is open.
4. **Roles & users**: list with attributes, member of (★ = admin option), members, connection limit, password expiry,
   sessions. Create user (LOGIN) or role, edit (rename, attributes, limit, expiry, password set / remove, memberships
   diffed into GRANT / REVOKE / REVOKE ADMIN OPTION FOR, comment), drop (type the name; the login the app uses cannot
   drop itself). Every change shows its SQL first (`…/admin/roles/preview`, password as `'***'`) and runs in one
   transaction. Built with `psycopg.sql` only.
5. **Passwords** are turned into a SCRAM-SHA-256 verifier in the app (`engine/admin.scram_verifier`: SASLprep, PBKDF2
   4096 iterations, random 16-byte salt — PostgreSQL's own format), so the plain password never reaches the server or
   its statement log. The app never stores or logs it. A Generate button makes a 20-character random one.
6. **Not in this version:** table / schema privileges (GRANT SELECT …), REASSIGN / DROP OWNED, per-database role
   settings, pg_hba.conf, server settings. A drop refused because of owned objects or privileges explains where.
7. **Code:** `engine/admin.py`, routes under `/api/servers/<id>/admin/…` in `app.py`, UI in `static/admin.js`
   (served at `/ws/admin.js`). Tests: `tests/test_admin.py` (unit + PostgreSQL: real login with the computed verifier,
   cancel / terminate a live session, non-admin login refused).
