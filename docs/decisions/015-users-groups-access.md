# 015 — Simpler admin: Users, Groups and Access

_Date: 2026-11-22 · Status: **released in v0.14.0** (user built and pushed it 2026-11-22). Builds on 011 (Admin panel)._

1. **Four sub-tabs instead of two:** Users · Groups · Access · Sessions (Sessions unchanged). Users = roles with LOGIN,
   Groups = roles without; built-in `pg_` roles only appear as optional chips under "More options" of a user.
2. **Short forms.** New user: name, password (Generate / Show / Copy), groups as chips. New group: name, description,
   members as chips. Expiry, connection limit, attributes, nested groups and the note are under "More options".
   Group members are sent as `members` (`GRANT group TO member`, diffed on edit) next to the existing `member_of`.
   After creating a group, or a user in no group, the app offers to open Access for it.
3. **Access** (`engine/access.py`, `GET|POST /api/servers/<id>/admin/access[/preview]`): pick who (list on the left)
   and a database. Per schema one dropdown sets every table / view: No access, Read only (SELECT), Read & write
   (+ INSERT, UPDATE), Read, write & delete (+ DELETE), Full control (all seven), Custom (chips per privilege); "Mixed"
   when tables differ. Tables can be set one by one. Per schema also: run functions (EXECUTE ON ALL ROUTINES), create
   tables (CREATE on the schema), "also tables created later" (ALTER DEFAULT PRIVILEGES FOR ROLE <schema owner>). The
   database card sets CONNECT / CREATE / TEMPORARY. A "Set all tables to…" quick pick covers every schema at once.
4. **Wanted state, diffed on the server.** The UI sends what it wants; the server re-reads the direct grants and emits
   only the needed GRANT / REVOKE: per schema `… ON ALL TABLES IN SCHEMA` (revoke what any table has but is not wanted,
   grant what any lacks), then per-table differences. Added automatically: USAGE on a schema while anything in it is
   granted (revoked when nothing is left), CONNECT on the database when it is missing, USAGE, SELECT on sequences when
   INSERT is newly granted (serial columns). Tables the role owns are never revoked from it.
5. **Shown, not hidden:** what a role gets anyway through groups or PUBLIC (e.g. functions are runnable by PUBLIC by
   default), ownership, superuser (nothing limits it), a non-superuser saved login (PostgreSQL may refuse grants on
   objects it does not own). Over 5,000 tables: only the first 5,000 are listed one by one.
6. **Confirm in plain words**, SQL behind "Show SQL"; one transaction; pending changes sit in a sticky bar with
   Discard, and switching user, database or tab asks before throwing them away.
7. **Tests:** `tests/test_access.py` (statement building, members, and against PostgreSQL: apply, effective rights
   through a group with a real login and insert, idempotence, clean-up, API routes).
