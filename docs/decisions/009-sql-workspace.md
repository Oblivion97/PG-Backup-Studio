# 009 — Step 8: SQL workspace (DataGrip-style console)

_Date: 2026-10-28 · Status: **approved by the user 2026-10-28** (plan "Step 8: SQL workspace"). Design: ARCHITECTURE §14._

1. **Full access.** Statements run as typed, in auto-commit by default. There is no read-only switch (user's choice).
   Manual mode is optional: the tab stays in one transaction until Commit or Rollback.
2. **Built-in editor, no libraries.** A textarea over a highlighted `<pre>`, hand-written suggestions. It works offline and
   adds no dependency (same reason as decision 008 §1).
3. **Separate files.** `static/index.html` was at its 150 KB budget, so the workspace lives in `static/console.js`,
   `static/grid.js` (and `explorer.js` in 8b). They are served at `/ws/<name>` from a fixed allowlist, same origin, no CDN.
4. **One server-side session per query tab** (`engine/console.py`). Manual transactions and Cancel need a connection that
   stays open between requests. At most 16 sessions; idle ones close after 30 min (an open transaction is rolled back);
   all close when the app stops. Dropping or renaming a database closes its sessions first.
5. **Row cap.** 1000 rows by default, up to 10,000. Row queries (SELECT / WITH / VALUES / TABLE) go through a server-side
   cursor, so a huge result never loads more than the cap. If DECLARE refuses the statement (SELECT INTO, data-modifying
   WITH), it runs normally; DECLARE fails before anything runs, so nothing happens twice.
6. **Values as PostgreSQL text** (what psql shows): exact numerics, `\x` bytea, arrays as `{...}`. They can be edited and
   sent back with a cast to the column type (8c).
7. **Errors in manual mode undo only the failing statement** (each statement runs in a savepoint), so the transaction stays
   usable. In auto-commit mode statements before the error stay committed; the rest are not run.
8. **Query history and open tabs** are kept in the browser (localStorage), only as a per-browser convenience.
9. **Built in four parts** (8a console, 8b explorer and table viewer, 8c grid tools and editing, 8d EXPLAIN and formatter),
   each tested and clicked through before the next.
