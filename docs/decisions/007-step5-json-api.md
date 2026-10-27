# 007 — Step 5: JSON API

_Date: 2026-10-27 · Status: **approved by the user 2026-10-27** (all seven recommendations). Design: ARCHITECTURE §12._

## 1. Protection against other websites (local-only app)
The app listens on 127.0.0.1 only, but any web page open in the same browser can still send requests to
`http://127.0.0.1:8000` (cross-site request forgery: a malicious page could POST "drop database"), and a
hostile DNS name can be pointed at 127.0.0.1 (DNS rebinding).
**Recommended:** every `/api/` request must (a) come with a `Host` of `127.0.0.1:<port>` or
`localhost:<port>`, and (b) carry the header `X-PGBS: 1` (our page always sends it; another site cannot add
a custom header without a CORS preflight, which we never allow). Bodies must be JSON. Failures → 403.
Alternative: no protection (simpler, but a drive-by page could drop databases).

## 2. Asking for a password that is not stored
**Recommended:** `POST /api/servers/<id>/connect {password}` checks the password by connecting and keeps
it **in memory only** until the app stops or the server is edited/deleted. Any call that needs a password
it does not have returns 401 with `code: "password_required"`; the UI shows a password box and retries. A
wrong password is dropped from memory and also gives 401. Alternative: the browser sends the password with
every request (it would sit in browser memory and every request body).

## 3. Status codes
**Recommended:** `{"error": "<sentence>", "code": "<code>"}` with 400 invalid · 401 password_required ·
403 protected / permission / forbidden · 404 not_found · 409 exists / in_use / conflict / busy · 500
unexpected (logged with traceback; the browser only gets "Unexpected error: <type>"). Successes: 200, 201
(created server / database), 202 (job started: `{"job_id": ...}`).

## 4. Slow calls run inside the request
**Recommended:** listing databases (sizes), the pre-check and DB actions run synchronously in the request
(Flask threaded; the pre-check already has a 60 s statement timeout). Only backup and restore are jobs.
Alternative: make the pre-check a job too (more moving parts for a few seconds of waiting).

## 5. Open folder
**Recommended:** only for a file recorded in a history entry (never a path sent by the browser). Windows:
Explorer opens the folder with the file selected; macOS `open -R`; Linux `xdg-open <folder>`. Missing file
→ open its folder if that exists, else 404.

## 6. Database names in URLs
Werkzeug decodes `%2F` before routing, so a database name containing `/` cannot be used in a URL path.
**Recommended:** accept this limitation (README): such a database is listed and can be backed up / restored
(names travel in JSON bodies there), but drop / rename / kill / profile need a name without `/`.
Alternative: send every database name in the body instead of the URL (departs from the spec's API shape).

## 7. "Test connection" result
**Recommended:** `POST /api/servers/test` answers 200 with `{"ok": false, "error": ...}` when the server
refuses (a failed test is a normal answer, not an API error); invalid form data is still 400.
