# 008 — Step 6: UI

_Date: 2026-10-27 · Status: **approved by the user 2026-10-27** (all eight recommendations; "make the UI enterprise grade"). Design: ARCHITECTURE §13._

## 1. One file, no libraries, works offline
**Recommended:** everything in `static/index.html`: inline CSS and plain JavaScript (ES2020, no build
step, no CDN). It works on a PC without internet and nothing third-party runs next to your database
passwords. Alternative: a small library such as Alpine.js or htmx from a CDN (less code, but needs
internet and adds an outside dependency).

## 2. Database names never become HTML
Database, schema, table and role names come from the server and can contain `<`, quotes, etc.
**Recommended:** the page builds elements with `textContent` / a small `el()` helper only; `innerHTML`
is used only for fixed markup written in the file. A QA test creates a database named
`pgbs_test_<img src=x onerror=...>` and checks nothing runs.

## 3. Where you are survives a page reload
**Recommended:** the address keeps the place: `#/s/<server id>/databases`, `#/s/<id>/backup/<db>`,
`#/s/<id>/restore`, `#/activity`, `#/history`. Reload or the browser Back button returns to the same
tab. Unsent form fields are not kept. Alternative: no URL state (reload always goes to the start).

## 4. Password prompt
When an API call answers 401 `password_required`, **recommended:** a small modal asks for the password
of that server, calls `/connect`, and repeats the original call automatically. Cancel leaves the
server "not connected". Nothing is saved; the app remembers it until it stops (decision 007 §2).

## 5. The pre-check and the Start button
Opening the backup panel loads schemas + saved profile, then runs the check. **Recommended:** changing
schemas, mode or a table option marks the check "out of date"; **Start backup** re-runs the check
first when it is out of date and only starts if nothing blocks. While a check is running, the button
shows "Checking…". Blocking problems are listed with a one-click "Exclude this schema / set table to
schema only" fix where possible. Alternative: re-run the check automatically on every change (more
server load while you are still choosing).

## 6. Large tables in the backup panel
**Recommended:** the per-table list shows large tables first and highlighted, then the rest; a filter
box and a "Large only" switch (on by default when there are more than 50 tables). Each row: name, size,
rows, data option (Full / Latest N / Schema only); for Latest N: ordering column (suggested ones
first) and N, plus the index advice from the check ("no index — will sort the whole table").

## 7. Confirmations
**Recommended:**
- Drop database and restore "drop and recreate": type the database name.
- Kill connections, cancel a job, delete a history entry: a plain confirm modal (no typing). Deleting
  the file too is a separate, unticked checkbox in that modal.
- Never the browser's `alert` / `confirm` dialogs; only in-page modals and toasts.

## 8. How the UI is tested
There is no browser test tool in the project. **Recommended:**
- Automated (pytest, no browser): the page is served; every `/api/...` path used in the JavaScript
  matches a Flask route with that method; every request sends `X-PGBS`; no `innerHTML` with a variable;
  no CDN; size budget (< 150 KB).
- Click-through by QA in your Chrome (Claude in Chrome extension) against PG 18 using a fixed checklist
  (§13.7), with screenshots of each flow in the QA report, plus the same checklist in the README so
  you can repeat it.
Alternative: add Playwright (real automated browser tests, but a ~150 MB browser download and a new
dependency).
