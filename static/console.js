/* PG Backup Studio: SQL workspace (Step 8, decision 009). Editor, suggestions, query tabs, transactions.
 *
 * Loaded before the page's inline script; uses its helpers (el, icon, api, toast, modal, menu, fmt, state, store,
 * server, go, loadDatabases, renderHeader, loadingView, errorCard, stale) only inside functions, so load order of
 * the definitions does not matter. Every global here starts with `ws` or `SQL_` so nothing clashes with the page.
 * Text is always set through text nodes (decision 008 §2).
 */
'use strict';

/* ======================================================================== styles */

const WS_CSS = `
:root { --tk-k: #7c3aed; --tk-s: #15803d; --tk-n: #b45309; --tk-q: #0369a1; --tk-f: #be185d; --tk-c: #64748b; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --tk-k: #c4b5fd; --tk-s: #86efac; --tk-n: #fbbf24; --tk-q: #7dd3fc; --tk-f: #f9a8d4; --tk-c: #8b95a5; } }
:root[data-theme="dark"] { --tk-k: #c4b5fd; --tk-s: #86efac; --tk-n: #fbbf24; --tk-q: #7dd3fc; --tk-f: #f9a8d4; --tk-c: #8b95a5; }
.content.ws { padding: 0; overflow: hidden; }
.content.ws .content-inner { max-width: none; height: 100%; gap: 0; }
.wsp { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--surface); }
.ws-body { flex: 1; display: flex; min-height: 0; }
.ws-center { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.ws-bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.ws-bar .input { height: 30px; }
.ws-bar select.input { width: auto; }
.ws-tabs { display: flex; align-items: flex-end; gap: 2px; padding: 6px 12px 0; border-bottom: 1px solid var(--border); background: var(--surface-2); overflow-x: auto; flex: none; }
.ws-tab { display: flex; align-items: center; gap: 6px; padding: 5px 6px 5px 10px; border: 1px solid var(--border); border-bottom: 0; border-radius: 6px 6px 0 0;
  background: var(--surface-3); color: var(--fg-2); cursor: pointer; font-size: 12.5px; white-space: nowrap; max-width: 220px; user-select: none; }
.ws-tab span { overflow: hidden; text-overflow: ellipsis; }
.ws-tab.on { background: var(--surface); color: var(--fg); font-weight: 600; margin-bottom: -1px; padding-bottom: 6px; }
.ws-tab .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warn); flex: none; }
.ws-tab button { border: 0; background: none; color: var(--fg-3); cursor: pointer; padding: 0 2px; display: flex; border-radius: 3px; }
.ws-tab button:hover { background: var(--border); color: var(--fg); }
.ws-tab button .icon { width: 13px; height: 13px; }
.ws-add { border: 0; background: none; color: var(--fg-2); cursor: pointer; padding: 5px 8px; display: flex; }
.ed { position: relative; display: flex; height: var(--ed-h, 260px); min-height: 80px; font-family: var(--mono); font-size: 13px; line-height: 20px; flex: none; }
.ed-gutter { width: 50px; flex: none; overflow: hidden; text-align: right; padding: 8px 8px 8px 0; color: var(--fg-3); background: var(--surface-2);
  border-right: 1px solid var(--border); user-select: none; box-sizing: border-box; position: relative; }
.ed-gutter div.err { color: var(--danger); font-weight: 700; }
.ed-main { position: relative; flex: 1; min-width: 0; overflow: hidden; }
.ed-hl, .ed-ta { position: absolute; inset: 0; margin: 0; padding: 8px 12px; font: inherit; line-height: inherit; white-space: pre; tab-size: 4;
  box-sizing: border-box; border: 0; letter-spacing: normal; word-spacing: normal; }
.ed-hl { overflow: hidden; color: var(--fg); pointer-events: none; }
.ed-ta { resize: none; background: transparent; color: transparent; caret-color: var(--fg); outline: none; overflow: auto; }
.ed-ta::selection { background: color-mix(in srgb, var(--accent) 28%, transparent); color: transparent; }
.ed-ta::placeholder { color: var(--fg-3); }
.ed-err { position: absolute; left: 0; right: 0; height: 20px; background: var(--danger-soft); pointer-events: none; }
.ed-hl .k { color: var(--tk-k); font-weight: 600; } .ed-hl .s { color: var(--tk-s); } .ed-hl .n { color: var(--tk-n); }
.ed-hl .q { color: var(--tk-q); } .ed-hl .c { color: var(--tk-c); font-style: italic; } .ed-hl .p { color: var(--tk-f); }
.ws-split-h { height: 6px; cursor: row-resize; background: var(--surface-2); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); flex: none; }
.ws-split-h:hover { background: var(--accent-soft); }
.ac { position: fixed; z-index: 70; min-width: 300px; max-width: 560px; max-height: 270px; overflow-y: auto; background: var(--surface);
  border: 1px solid var(--border-strong); border-radius: 6px; box-shadow: var(--shadow-lg); font-family: var(--mono); font-size: 12.5px; padding: 3px; }
.ac .it { display: flex; gap: 8px; align-items: center; padding: 3px 8px; border-radius: 4px; cursor: pointer; white-space: nowrap; }
.ac .it.on { background: var(--accent-soft); }
.ac .kd { width: 18px; height: 16px; line-height: 16px; text-align: center; font-size: 10px; font-weight: 700; border-radius: 3px; flex: none; color: #fff; background: var(--fg-3); }
.ac .kd.t { background: #2563eb; } .ac .kd.v { background: #0891b2; } .ac .kd.c { background: #16a34a; } .ac .kd.f { background: #c026d3; }
.ac .kd.k { background: #7c3aed; } .ac .kd.s { background: #64748b; } .ac .kd.n { background: #ea580c; }
.ac .nm { flex: 1; overflow: hidden; text-overflow: ellipsis; color: var(--fg); }
.ac .nm b { color: var(--accent); }
.ac .ds { color: var(--fg-3); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; max-width: 240px; }
.ac .ft { padding: 4px 8px 2px; color: var(--fg-3); font-family: inherit; font-size: 11px; border-top: 1px solid var(--border); margin-top: 3px; }
.ws-res { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.ws-rtabs { display: flex; gap: 4px; align-items: center; padding: 5px 12px; border-bottom: 1px solid var(--border); background: var(--surface-2); flex-wrap: wrap; flex: none; }
.ws-rtab { padding: 2px 10px; border-radius: 12px; cursor: pointer; font-size: 12.5px; color: var(--fg-2); border: 1px solid transparent; background: none; }
.ws-rtab.on { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 30%, transparent); }
.ws-rtab.err { color: var(--danger); }
.ws-status { display: flex; gap: 14px; padding: 4px 12px; border-top: 1px solid var(--border); font-size: 12px; color: var(--fg-3); background: var(--surface-2); flex: none; flex-wrap: wrap; }
.ws-status .grow { flex: 1; }
.ws-msgs { overflow: auto; padding: 8px 14px; font-family: var(--mono); font-size: 12.5px; flex: 1; }
.ws-msg { padding: 7px 0; border-bottom: 1px dashed var(--border); }
.ws-msg .st { color: var(--fg); font-weight: 600; }
.ws-msg .er { color: var(--danger); white-space: pre-wrap; }
.ws-msg .nt { color: var(--fg-2); }
.ws-msg pre { margin: 4px 0 0; color: var(--fg-3); white-space: pre-wrap; max-height: 80px; overflow: hidden; }
.ws-msg a { cursor: pointer; }
.ws-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--fg-3); font-size: 13px; text-align: center; padding: 20px; }
.ws-tx { font-size: 11.5px; }
.ws-hist { max-height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.ws-hist .h { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; cursor: pointer; }
.ws-hist .h:hover { border-color: var(--accent); }
.ws-hist pre { margin: 0; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; max-height: 64px; overflow: hidden; }
`;
(function wsStyle() {
  const s = document.createElement('style');
  s.textContent = WS_CSS;
  document.head.appendChild(s);
})();

/* ======================================================================== SQL tokenizer */

const SQL_KEYWORDS = new Set(('ABORT ADD ALL ALTER ANALYZE AND ANY ARRAY AS ASC ASYMMETRIC BEGIN BETWEEN BIGINT BOOLEAN BOTH BY CALL CASCADE CASE CAST CHECK ' +
  'CLUSTER COLLATE COLUMN COMMENT COMMIT CONCURRENTLY CONFLICT CONSTRAINT COPY CREATE CROSS CURRENT_DATE CURRENT_TIMESTAMP CURRENT_USER CURSOR ' +
  'DATABASE DEALLOCATE DECLARE DEFAULT DEFERRABLE DELETE DESC DISTINCT DO DOMAIN DROP ELSE END EXCEPT EXECUTE EXISTS EXPLAIN EXTENSION FALSE FETCH ' +
  'FILTER FIRST FOR FOREIGN FROM FULL FUNCTION GRANT GROUP HAVING IF ILIKE IN INDEX INHERITS INNER INSERT INTERSECT INTERVAL INTO IS ISNULL JOIN KEY ' +
  'LANGUAGE LAST LATERAL LEADING LEFT LIKE LIMIT LOCK MATERIALIZED NATURAL NOT NOTHING NOTNULL NULL NULLS OF OFFSET ON ONLY OR ORDER OUTER OVER OWNER ' +
  'PARTITION POLICY PRIMARY PROCEDURE PUBLICATION RECURSIVE REFERENCES REFRESH REINDEX RELEASE RENAME REPLACE RESET RESTRICT RETURNING RETURNS REVOKE ' +
  'RIGHT ROLE ROLLBACK ROW ROWS SAVEPOINT SCHEMA SELECT SEQUENCE SESSION_USER SET SHOW SIMILAR SOME START SYMMETRIC TABLE TABLESAMPLE TEMP TEMPORARY ' +
  'THEN TO TRAILING TRANSACTION TRIGGER TRUE TRUNCATE TYPE UNION UNIQUE UNLOGGED UPDATE USING VACUUM VALUES VARIADIC VERBOSE VIEW WHEN WHERE WINDOW ' +
  'WITH WITHIN WITHOUT ' +
  'INTEGER INT SMALLINT TEXT VARCHAR CHAR NUMERIC DECIMAL REAL DOUBLE PRECISION DATE TIME TIMESTAMP TIMESTAMPTZ JSON JSONB UUID BYTEA SERIAL BIGSERIAL ' +
  'GENERATED ALWAYS IDENTITY STORED VIRTUAL ZONE ASSERT RAISE NOTICE EXCEPTION PERFORM RETURN DECLARE LOOP WHILE ELSIF ANALYSE BUFFERS COSTS FORMAT ' +
  'TIMING SUMMARY SETTINGS WAL').split(' '));

/** Tokens of `text`: {t: kind, a: start, b: end}. Kinds: w (word), q ("ident"), s (string), n (number), c (comment),
 * p ($1 parameter), o (operator / punctuation, one char), ' ' (whitespace). Unterminated strings run to the end. */
function sqlTokens(text) {
  const out = [];
  const n = text.length;
  let i = 0;
  while (i < n) {
    const ch = text[i];
    const a = i;
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      while (i < n && /\s/.test(text[i])) i++;
      out.push({ t: ' ', a, b: i });
    } else if (ch === '-' && text[i + 1] === '-') {
      while (i < n && text[i] !== '\n') i++;
      out.push({ t: 'c', a, b: i });
    } else if (ch === '/' && text[i + 1] === '*') {
      let depth = 0;
      while (i < n) {
        if (text[i] === '/' && text[i + 1] === '*') { depth++; i += 2; } else if (text[i] === '*' && text[i + 1] === '/') { depth--; i += 2; if (!depth) break; } else i++;
      }
      out.push({ t: 'c', a, b: Math.min(i, n) });
    } else if (ch === "'" || ((ch === 'E' || ch === 'e') && text[i + 1] === "'")) {
      const esc = ch !== "'";
      i += esc ? 2 : 1;
      while (i < n) {
        if (esc && text[i] === '\\') { i += 2; continue; }
        if (text[i] === "'") { if (text[i + 1] === "'") { i += 2; continue; } i++; break; }
        i++;
      }
      out.push({ t: 's', a, b: Math.min(i, n) });
    } else if (ch === '"') {
      i++;
      while (i < n) { if (text[i] === '"') { if (text[i + 1] === '"') { i += 2; continue; } i++; break; } i++; }
      out.push({ t: 'q', a, b: Math.min(i, n) });
    } else if (ch === '$') {
      const m = /^\$([A-Za-z_][A-Za-z_0-9]*)?\$/.exec(text.slice(i, i + 80));
      if (m) {
        const tag = m[0];
        const end = text.indexOf(tag, i + tag.length);
        i = end < 0 ? n : end + tag.length;
        out.push({ t: 's', a, b: i });
      } else if (/[0-9]/.test(text[i + 1] || '')) {
        i++;
        while (i < n && /[0-9]/.test(text[i])) i++;
        out.push({ t: 'p', a, b: i });
      } else { i++; out.push({ t: 'o', a, b: i }); }
    } else if (/[0-9]/.test(ch) || (ch === '.' && /[0-9]/.test(text[i + 1] || ''))) {
      while (i < n && /[0-9._eE]/.test(text[i])) { if ((text[i] === 'e' || text[i] === 'E') && /[+-]/.test(text[i + 1] || '')) i++; i++; }
      out.push({ t: 'n', a, b: i });
    } else if (/[A-Za-z_\u0080-￿]/.test(ch)) {
      while (i < n && /[A-Za-z0-9_$\u0080-￿]/.test(text[i])) i++;
      out.push({ t: 'w', a, b: i });
    } else {
      i++;
      out.push({ t: 'o', a, b: i });
    }
  }
  return out;
}

/** Statements as [start, end) ranges split at top-level semicolons (semicolon included). */
function sqlStatements(text, tokens) {
  const ranges = [];
  let start = 0;
  for (const tk of tokens || sqlTokens(text)) {
    if (tk.t === 'o' && text[tk.a] === ';') { ranges.push([start, tk.b]); start = tk.b; }
  }
  if (start < text.length) ranges.push([start, text.length]);
  return ranges.filter(([a, b]) => sqlTokens(text.slice(a, b)).some((t) => t.t !== ' ' && t.t !== 'c' && !(t.t === 'o' && text[a + t.a] === ';')));
}

/** The statement the caret is in (or right after, on the same line); null when there is none. */
function sqlStatementAt(text, pos) {
  const ranges = sqlStatements(text);
  for (const [a, b] of ranges) if (pos >= a && pos <= b) {
    // caret after the semicolon on a later line belongs to the next statement
    if (pos === b && text[b - 1] === ';' && text.slice(b - 1, pos).includes('\n')) continue;
    return [a, b];
  }
  // caret in blank space after the last statement: the previous one
  const before = ranges.filter(([, b]) => b <= pos);
  return before.length ? before[before.length - 1] : (ranges[0] || null);
}

function sqlLineOf(text, pos) { let n = 1; for (let i = 0; i < pos; i++) if (text.charCodeAt(i) === 10) n++; return n; }

/** Unquoted name as written, or a "quoted" one unquoted; null for anything else. */
function sqlName(text, tk) {
  if (!tk) return null;
  const s = text.slice(tk.a, tk.b);
  if (tk.t === 'w') return s.toLowerCase();
  if (tk.t === 'q') return s.slice(1, -1).replace(/""/g, '"');
  return null;
}

/** A name ready to type: quoted when it is not a plain lower-case identifier or is a keyword. */
function sqlIdent(name) {
  return /^[a-z_][a-z0-9_$]*$/.test(name) && !SQL_KEYWORDS.has(name.toUpperCase()) ? name : '"' + name.replace(/"/g, '""') + '"';
}

/* ======================================================================== SQL formatter */

const SQL_FMT_CLAUSE = new Set(['SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'UNION', 'EXCEPT', 'INTERSECT', 'VALUES',
  'SET', 'RETURNING', 'WINDOW', 'INSERT', 'UPDATE', 'DELETE', 'WITH', 'FETCH']);
const SQL_FMT_JOIN = new Set(['JOIN', 'LEFT', 'RIGHT', 'INNER', 'FULL', 'CROSS', 'NATURAL']);
const SQL_FMT_NAME_BEFORE = new Set(['INTO', 'TABLE', 'ON', 'REFERENCES', 'VIEW', 'TYPE', 'INHERITS']);
const SQL_FMT_PAREN_SPACE = new Set(['AND', 'OR', 'IN', 'FROM', 'JOIN', 'AS', 'ON', 'WHERE', 'SELECT', 'VALUES', 'EXISTS', 'NOT', 'USING', 'OVER',
  'FILTER', 'WITHIN', 'WITH', 'INTO', 'TABLE', 'THEN', 'ELSE', 'WHEN', 'RETURNS', 'ANY', 'ALL', 'SOME', 'BY', 'LATERAL', 'CHECK', 'KEY', 'REFERENCES', 'UNIQUE']);

/** Reformat SQL: keywords upper case, one major clause per line, select lists and AND / OR conditions one per
 * line, subqueries indented. Strings, quoted names, comments and $$ bodies are kept exactly as written. */
function sqlFormat(text) {
  const all = sqlTokens(text);
  const sig = all.filter((t) => t.t !== ' ');
  const word = (k) => (sig[k] && sig[k].t === 'w' ? text.slice(sig[k].a, sig[k].b).toUpperCase() : '');
  const ch = (k) => (sig[k] && sig[k].t === 'o' ? text[sig[k].a] : '');
  let out = '';
  let depth = 0;              // open subquery parentheses
  const parens = [];          // per open "(": true when it holds a subquery
  let clause = '';            // last major clause at the current subquery level
  let between = false;
  let lineStart = true;
  const inline = () => parens.length && !parens[parens.length - 1];   // inside a non-subquery "(...)"
  const nl = (extra) => {
    out = out.replace(/[ \t]+$/, '');
    if (!lineStart) out += '\n';
    out += '  '.repeat(Math.max(0, depth * 2 + (extra || 0)));
    lineStart = true;
  };
  const put = (s, space) => {
    if (!lineStart && space) out += ' ';
    out += s;
    lineStart = false;
  };
  for (let k = 0; k < sig.length; k++) {
    const tk = sig[k];
    const raw = text.slice(tk.a, tk.b);
    const prevCh = ch(k - 1), prevW = word(k - 1);
    if (tk.t === 'c') {
      if (raw.startsWith('--')) { put(raw, true); out += '\n' + '  '.repeat(depth * 2 + (clause ? 1 : 0)); lineStart = true; } else put(raw, true);
      continue;
    }
    if (tk.t === 'o') {
      if (raw === '(') {
        const sub = ['SELECT', 'WITH', 'VALUES'].includes(word(k + 1));
        let j = k - 1;   // start of a dotted name before "(": a table name after INTO / ON / ... keeps a space
        while (j >= 2 && ch(j - 1) === '.') j -= 2;
        const named = SQL_FMT_NAME_BEFORE.has(word(j - 1));
        const tight = !named && ((sig[k - 1] && (sig[k - 1].t === 'q' || (sig[k - 1].t === 'w' && !SQL_FMT_PAREN_SPACE.has(prevW)))) || prevCh === '(' || prevCh === '[');
        put('(', !tight);
        parens.push(sub);
        if (sub) { depth++; clause = ''; }
      } else if (raw === ')') {
        const sub = parens.pop();
        if (sub) { depth--; nl(1); clause = 'SUB'; }
        put(')', false);
      } else if (raw === ',') {
        put(',', false);
        if (!inline() && (clause === 'SELECT' || clause === 'SET' || clause === 'WITH' || clause === 'RETURNING')) nl(clause === 'WITH' ? 0 : 1);
      } else if (raw === ';') {
        put(';', false);
        out += '\n\n';
        lineStart = true;
        clause = '';
        depth = 0;
        parens.length = 0;
      } else if (raw === '.' || raw === ':' || raw === '[' || raw === ']') {
        put(raw, false);
      } else {
        const glue = prevCh === '.' || prevCh === ':' || (raw === '=' && /[<>!]/.test(prevCh)) || (/[<>=|&]/.test(prevCh) && /[<>=|&]/.test(raw) && sig[k - 1].b === tk.a);
        put(raw, !glue && prevCh !== '(' && prevCh !== '[');
      }
      continue;
    }
    // words, names, strings, numbers, parameters
    const W = tk.t === 'w' ? raw.toUpperCase() : '';
    const kw = tk.t === 'w' && SQL_KEYWORDS.has(W) && prevCh !== '.' && prevCh !== ':';
    const show = kw ? W : raw;
    const afterDot = prevCh === '.' || prevCh === ':';
    if (kw && !inline() && !afterDot) {
      const isJoin = SQL_FMT_JOIN.has(W) && (W === 'JOIN' || ['JOIN', 'OUTER', 'INNER', 'LEFT', 'RIGHT'].includes(word(k + 1)));
      const continuesJoin = SQL_FMT_JOIN.has(prevW) || prevW === 'OUTER';
      const clauseWord = SQL_FMT_CLAUSE.has(W) && !(W === 'FROM' && prevW === 'DELETE') && !(W === 'SET' && clause === '' && depth === 0 && prevW === '')
        && !(W === 'SELECT' && (prevW === 'ALL' || prevW === 'UNION' || prevW === 'EXCEPT' || prevW === 'INTERSECT')) && !((W === 'UPDATE' || W === 'DELETE') && prevW === 'FOR')
        && !(W === 'WITH' && clause !== '' && clause !== 'SUB') && !(W === 'FETCH' && clause === '');
      if ((clauseWord || isJoin) && !continuesJoin && !(lineStart && out.trim() === '')) {
        nl(0);
        if (clauseWord) clause = W;
        else clause = 'FROM';
      } else if ((W === 'AND' || W === 'OR') && !between && (clause === 'WHERE' || clause === 'HAVING' || clause === 'FROM')) {
        nl(1);
      } else if (clauseWord) clause = W;
      if (W === 'BETWEEN') between = true;
      else if (W === 'AND' && between) between = false;
      if (W === 'ON' && clause === 'FROM' && !lineStart) { put(show, true); continue; }
    }
    put(show, !afterDot && prevCh !== '(' && prevCh !== '[');
  }
  return out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

/* ======================================================================== workspace state */

const ws = {
  sets: {},        // `${sid}\u0000${db}` -> {tabs, active}
  meta: {},        // `${sid}\u0000${db}` -> completions payload (+ cols Map, loading Promise)
  view: null,      // the mounted .wsp element
  ac: null,        // open suggestion popup
  seq: 0,
};
const WS_HISTORY = 100;
const WS_ROWS = [100, 500, 1000, 5000, 10000];

function wsKey(sid, db) { return sid + '\u0000' + db; }
function wsSaved(name) { try { return JSON.parse(store(name) || 'null'); } catch (e) { return null; } }

function wsSet(sid, db) {
  const k = wsKey(sid, db);
  if (!ws.sets[k]) {
    const saved = wsSaved(`ws.tabs.${sid}.${db}`);
    const tabs = (saved && Array.isArray(saved.tabs) && saved.tabs.length ? saved.tabs : [{ title: 'Query 1', sql: '' }])
      .map((t) => wsNewTab(sid, db, t.title, t.sql, t.kind === 'table' && t.schema && t.table ? { kind: 'table', schema: t.schema, table: t.table,
        relkind: t.relkind || 'table', where: t.where || '', order: Array.isArray(t.order) ? t.order : [], limit: WX_PAGES.includes(t.limit) ? t.limit : 100 } : null));
    ws.sets[k] = { sid, db, tabs, active: Math.min((saved && saved.active) || 0, tabs.length - 1) };
  }
  return ws.sets[k];
}

/** A query tab, or with `extra.kind = 'table'` a table viewer tab (explorer.js). */
function wsNewTab(sid, db, title, sql, extra) {
  return Object.assign({ id: ++ws.seq, kind: 'query', sid, db, title: title || 'Query', sql: sql || '', session: null, mode: 'auto', tx: 'idle', running: false,
    results: null, active: 0, errLine: null, maxRows: Number(store('ws.maxrows')) || 1000, timeout: Number(store('ws.timeout')) || 0,
    caret: 0, scrollTop: 0, ranAt: null }, extra || {});
}

function wsPersist(set) {
  store(`ws.tabs.${set.sid}.${set.db}`, JSON.stringify({ active: set.active, tabs: set.tabs.map((t) => (t.kind === 'table'
    ? { kind: 'table', title: t.title, schema: t.schema, table: t.table, relkind: t.relkind, where: t.where, order: t.order, limit: t.limit }
    : { title: t.title, sql: t.sql.slice(0, 200000) })) }));
}
let wsPersistTimer = null;
function wsPersistSoon(set) { clearTimeout(wsPersistTimer); wsPersistTimer = setTimeout(() => wsPersist(set), 400); }

function wsHistory(sid, db) { return wsSaved(`ws.hist.${sid}.${db}`) || []; }
function wsAddHistory(tab, sql, ok, ms) {
  const list = wsHistory(tab.sid, tab.db).filter((h) => h.sql !== sql);
  list.unshift({ sql: sql.slice(0, 20000), at: new Date().toISOString(), ok, ms });
  store(`ws.hist.${tab.sid}.${tab.db}`, JSON.stringify(list.slice(0, WS_HISTORY)));
}

/* ======================================================================== route entry */

/** #/s/<sid>/query[/<db>] */
async function viewQuery(view, token) {
  const sid = state.route.sid;
  let dbs = state.dbs[sid];
  if (!dbs) {
    loadingView(view, 'Connecting to the server…');
    try { dbs = await loadDatabases(sid); } catch (e) { if (!stale(token)) errorCard(view, e, () => route()); return; }
    if (stale(token)) return;
    renderHeader();
  }
  let db = state.route.db;
  if (!db || !dbs.some((d) => d.name === db)) {
    const last = store(`ws.db.${sid}`);
    const pick = dbs.find((d) => d.name === last) || dbs.find((d) => !/^template[01]$/.test(d.name)) || dbs[0];
    if (!pick) { clear(view, el('div', { class: 'card' }, el('div', { class: 'empty' }, el('p', { text: 'No databases on this server.' })))); return; }
    location.replace(`#/s/${enc(sid)}/query/${enc(pick.name)}`);
    return;
  }
  store(`ws.db.${sid}`, db);
  const set = wsSet(sid, db);
  wsLoadMeta(sid, db);
  wsMount(view, set, dbs);
}

/* ======================================================================== layout */

function wsMount(view, set, dbs) {
  wsCloseAc();
  const root = el('div', { class: 'wsp' });
  ws.view = root;
  ws.set = set;
  root._dbs = dbs;
  clear(view, root);
  wsRender();
}

function wsTab() { const set = ws.set; return set && set.tabs[set.active]; }

function wsRender() {
  const root = ws.view;
  if (!root || !root.isConnected) return;
  const set = ws.set;
  const tab = wsTab();
  const prev = root.querySelector('.ed-ta');
  if (prev && prev._tab) { prev._tab.caret = prev.selectionStart; prev._tab.scrollTop = prev.scrollTop; }

  const dbSel = el('select', { class: 'input', 'aria-label': 'Database', title: 'Database',
    onchange: (e) => go(`#/s/${enc(set.sid)}/query/${enc(e.target.value)}`) },
  root._dbs.map((d) => el('option', { value: d.name, selected: d.name === set.db }, d.name)));
  const tabsBar = el('div', { class: 'ws-tabs', role: 'tablist' },
    el('button', { class: 'ws-add', type: 'button', title: store('ws.wx') === '0' ? 'Show the explorer' : 'Hide the explorer', 'aria-label': 'Toggle explorer',
      onclick: () => wxToggleExplorer() }, wxIcon('wx_side')),
    set.tabs.map((t, i) => el('div', { class: 'ws-tab' + (i === set.active ? ' on' : ''), role: 'tab', title: t.kind === 'table' ? `${t.schema}.${t.table}` : t.title,
      onclick: () => { set.active = i; wsPersistSoon(set); wsRender(); },
      ondblclick: () => wsRenameTab(t) },
    t.tx === 'in_transaction' || t.tx === 'failed' ? el('span', { class: 'dot', title: 'Open transaction' }) : null,
    t.running ? el('span', { class: 'spin', style: 'width:10px;height:10px;border-width:2px' }) : t.kind === 'table' ? wxIcon('wx_table', 'wx-t') : null,
    el('span', { text: t.title }),
    el('button', { type: 'button', 'aria-label': 'Close tab', title: 'Close tab', onclick: (e) => { e.stopPropagation(); wsCloseTab(t); } }, icon('x')))),
    el('button', { class: 'ws-add', type: 'button', title: 'New query tab', 'aria-label': 'New query tab', onclick: () => wsAddTab() }, icon('plus')));

  if (tab.kind === 'table') {
    clear(root, tabsBar, el('div', { class: 'ws-body' }, wxPanel(set), el('div', { class: 'ws-center' }, wxTableView(tab, dbSel))));
    return;
  }
  const run =el('button', { class: 'btn sm primary', type: 'button', disabled: tab.running, title: 'Run the statement at the cursor or the selection (Ctrl+Enter)',
    onclick: () => wsRunCurrent() }, icon('play'), 'Run');
  const runAll = el('button', { class: 'btn sm', type: 'button', disabled: tab.running, title: 'Run everything in the editor (Ctrl+Shift+Enter)',
    onclick: () => wsRunAll() }, 'Run all');
  const cancel = el('button', { class: 'btn sm danger', type: 'button', disabled: !tab.running, title: 'Cancel the running statement',
    onclick: () => wsCancel(tab) }, icon('stop'), 'Cancel');
  const explain = el('button', { class: 'btn sm', type: 'button', disabled: tab.running, title: 'Show how PostgreSQL runs the statement at the cursor' }, icon('zap'), 'Explain');
  explain.onclick = () => menu(explain, [
    { icon: 'zap', label: 'Explain plan (Ctrl+E)', title: 'Estimated plan; the statement is not run', action: () => wsExplain(false) },
    { icon: 'activity', label: 'Explain analyze (Ctrl+Shift+E)', title: 'Runs the statement and shows real times; its changes are rolled back', action: () => wsExplain(true) },
  ]);
  const mode = el('select', { class: 'input', 'aria-label': 'Transaction mode', title: 'Auto-commit: each statement is saved at once. Manual: changes wait for Commit.',
    onchange: (e) => wsSetMode(tab, e.target.value, e.target) },
  el('option', { value: 'auto', selected: tab.mode === 'auto' }, 'Auto-commit'), el('option', { value: 'manual', selected: tab.mode === 'manual' }, 'Manual commit'));
  const inTx = tab.tx === 'in_transaction' || tab.tx === 'failed';
  const txBits = tab.mode === 'manual' || inTx ? [
    el('span', { class: 'badge ws-tx ' + (tab.tx === 'failed' ? 'danger' : inTx ? 'warn' : ''), text: tab.tx === 'failed' ? 'transaction failed' : inTx ? 'transaction open' : 'no transaction' }),
    el('button', { class: 'btn sm', type: 'button', disabled: !inTx || tab.running, onclick: () => wsEndTx(tab, 'commit') }, icon('check'), 'Commit'),
    el('button', { class: 'btn sm', type: 'button', disabled: !inTx || tab.running, onclick: () => wsEndTx(tab, 'rollback') }, icon('restore'), 'Rollback'),
  ] : [];
  const rows = el('select', { class: 'input', 'aria-label': 'Row limit', title: 'Most rows read per result',
    onchange: (e) => { tab.maxRows = Number(e.target.value); store('ws.maxrows', String(tab.maxRows)); } },
  WS_ROWS.map((n) => el('option', { value: String(n), selected: n === tab.maxRows }, `${fmt.num(n)} rows`)));
  const timeout = el('select', { class: 'input', 'aria-label': 'Statement timeout', title: 'Cancel statements that run longer than this',
    onchange: (e) => { tab.timeout = Number(e.target.value); store('ws.timeout', String(tab.timeout)); } },
  [[0, 'No timeout'], [10, '10 s'], [30, '30 s'], [60, '1 min'], [300, '5 min'], [1800, '30 min']].map(([v, t]) => el('option', { value: String(v), selected: v === tab.timeout }, t)));
  const more = el('button', { class: 'btn sm icon-only ghost', type: 'button', title: 'More', 'aria-label': 'More' }, icon('more'));
  more.onclick = () => menu(more, [
    { icon: 'history', label: 'Query history…', action: () => wsHistoryModal(tab) },
    { icon: 'code', label: 'Format SQL (Ctrl+Shift+F)', action: () => wsFormat() },
    { icon: 'edit', label: 'Rename tab…', action: () => wsRenameTab(tab) },
    { icon: 'refresh', label: 'Reload suggestions', action: () => { delete ws.meta[wsKey(set.sid, set.db)]; wsLoadMeta(set.sid, set.db); toast('Suggestions reloaded.', 'ok'); } },
    '-',
    { icon: 'link', label: tab.session ? 'Disconnect this tab' : 'Not connected', disabled: !tab.session || tab.running, action: () => wsDisconnect(tab) },
  ]);
  const bar = el('div', { class: 'ws-bar' }, icon('db'), dbSel, run, runAll, cancel, explain, el('span', { style: 'width: 6px' }), mode, txBits,
    el('div', { class: 'grow' }), rows, timeout, more);

  const editor = wsEditor(tab);
  const split = el('div', { class: 'ws-split-h', title: 'Drag to resize', onmousedown: (e) => wsDragSplit(e, editor) });
  const results = wsResults(tab);
  const saved = Number(store('ws.edh'));
  if (saved) editor.style.setProperty('--ed-h', saved + 'px');
  clear(root, tabsBar, el('div', { class: 'ws-body' }, wxPanel(set), el('div', { class: 'ws-center' }, bar, editor, split, results)));
  const ta = editor.querySelector('.ed-ta');
  requestAnimationFrame(() => {
    ta.scrollTop = tab.scrollTop;
    ta.setSelectionRange(tab.caret, tab.caret);
    wsSync(ta);
    if (!modals.length && document.activeElement && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) ta.focus({ preventScroll: true });
  });
}

function wsDragSplit(e, editor) {
  e.preventDefault();
  const startY = e.clientY, startH = editor.offsetHeight;
  const move = (ev) => { const h = Math.max(80, Math.min(window.innerHeight - 220, startH + ev.clientY - startY)); editor.style.setProperty('--ed-h', h + 'px'); };
  const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); store('ws.edh', String(editor.offsetHeight)); };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

/* ======================================================================== tabs */

function wsAddTab(sql, title) {
  const set = ws.set;
  let n = set.tabs.length + 1;
  while (set.tabs.some((t) => t.title === `Query ${n}`)) n++;
  set.tabs.push(wsNewTab(set.sid, set.db, title || `Query ${n}`, sql || ''));
  set.active = set.tabs.length - 1;
  wsPersist(set);
  wsRender();
}

async function wsCloseTab(tab) {
  const set = ws.set;
  if (!(await wsKeepEdits(tab, 'Closing the tab'))) return;
  if (tab.tx === 'in_transaction' || tab.tx === 'failed') {
    const yes = await confirmBox({ title: 'Close with an open transaction?', danger: true, confirmLabel: 'Roll back and close',
      message: `"${tab.title}" has changes that were not committed. Closing the tab rolls them back.` });
    if (!yes) return;
  }
  if (tab.running) wsCancel(tab);
  if (tab.session) api('DELETE', `/api/console/${enc(tab.session)}`).catch(() => {});
  const i = set.tabs.indexOf(tab);
  set.tabs.splice(i, 1);
  if (!set.tabs.length) set.tabs.push(wsNewTab(set.sid, set.db, 'Query 1', ''));
  set.active = Math.min(set.active >= i ? Math.max(0, set.active - 1) : set.active, set.tabs.length - 1);
  wsPersist(set);
  wsRender();
}

function wsRenameTab(tab) {
  const input = el('input', { class: 'input', value: tab.title, maxlength: '60', 'aria-label': 'Tab name' });
  const ok = el('button', { class: 'btn primary', type: 'button' }, 'Rename');
  const m = modal({ title: 'Rename tab', body: el('div', { class: 'field' }, el('label', null, 'Name'), input), actions: [ok] });
  const done = () => { const v = input.value.trim(); if (v) { tab.title = v; wsPersist(ws.set); wsRender(); } m.close(); };
  ok.onclick = done;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(); });
}

function wsHistoryModal(tab) {
  const list = wsHistory(tab.sid, tab.db);
  const q = el('input', { class: 'input', placeholder: 'Search history', 'aria-label': 'Search history' });
  const box = el('div', { class: 'ws-hist' });
  let m = null;
  const draw = () => {
    const s = q.value.toLowerCase();
    const hits = list.filter((h) => !s || h.sql.toLowerCase().includes(s));
    clear(box, hits.length ? hits.map((h) => el('div', { class: 'h', title: 'Open in the editor', onclick: () => { m.close(); wsInsertQuery(h.sql); } },
      el('div', { class: 'row', style: 'gap: 8px; margin-bottom: 4px' }, el('span', { class: 'badge ' + (h.ok ? 'ok' : 'danger'), text: h.ok ? 'ok' : 'error' }),
        el('span', { class: 'subtle', text: `${fmt.date(h.at)} · ${h.ms === null || h.ms === undefined ? '' : Math.round(h.ms) + ' ms'}` })),
      el('pre', { text: h.sql }))) : el('div', { class: 'subtle', text: list.length ? 'Nothing matches.' : 'Queries you run in this database are listed here (kept in this browser only).' }));
  };
  q.addEventListener('input', draw);
  draw();
  m = modal({ title: 'Query history', sub: `${tab.db} · last ${WS_HISTORY} queries`, wide: true, body: [q, el('div', { style: 'height: 10px' }), box],
    actions: [el('button', { class: 'btn', type: 'button', onclick: () => { store(`ws.hist.${tab.sid}.${tab.db}`, '[]'); list.length = 0; draw(); } }, 'Clear history')] });
}

/** Put a query into the current tab (replacing it when empty, else appending). */
function wsInsertQuery(sql) {
  const tab = wsTab();
  tab.sql = tab.sql.trim() ? tab.sql.replace(/\s*$/, '\n\n') + sql : sql;
  tab.caret = tab.sql.length;
  wsPersistSoon(ws.set);
  wsRender();
}

/* ======================================================================== editor */

let wsCharW = 0;
function wsCharWidth(ta) {
  if (wsCharW) return wsCharW;
  const probe = el('span', { style: 'position:absolute;visibility:hidden;white-space:pre;font:inherit', text: 'M'.repeat(50) });
  ta.parentNode.appendChild(probe);
  wsCharW = probe.getBoundingClientRect().width / 50 || 7.8;
  probe.remove();
  return wsCharW;
}

function wsEditor(tab) {
  const gutter = el('div', { class: 'ed-gutter', 'aria-hidden': 'true' });
  const hl = el('pre', { class: 'ed-hl', 'aria-hidden': 'true' });
  const ta = el('textarea', { class: 'ed-ta', spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', wrap: 'off', 'aria-label': 'SQL editor',
    placeholder: 'Type SQL here. Ctrl+Enter runs the statement at the cursor, Ctrl+Space suggests names.' });
  ta.value = tab.sql;
  ta._tab = tab;
  const main = el('div', { class: 'ed-main' }, hl, ta);
  const box = el('div', { class: 'ed' }, gutter, main);
  ta.addEventListener('input', (e) => {
    tab.sql = ta.value;
    if (tab.errLine) tab.errLine = null;
    wsPersistSoon(ws.set);
    wsSync(ta);
    if (!ws.typing) wsMaybeSuggest(ta, e);
  });
  ta.addEventListener('scroll', () => { wsSyncScroll(ta); wsCloseAc(); });
  ta.addEventListener('keydown', (e) => wsKeydown(e, ta));
  ta.addEventListener('blur', () => setTimeout(() => { if (!ws.ac || !ws.ac.el.matches(':hover')) wsCloseAc(); }, 120));
  ta.addEventListener('mousedown', () => wsCloseAc());
  return box;
}

const WS_TOKEN_CLASS = { s: 's', n: 'n', q: 'q', c: 'c', p: 'p' };

/** SQL text as a fragment of spans classed k (keyword), s, n, q, c, p; the caller styles them. */
function wsHighlight(text) {
  const frag = document.createDocumentFragment();
  for (const tk of sqlTokens(text)) {
    const s = text.slice(tk.a, tk.b);
    const cls = tk.t === 'w' ? (SQL_KEYWORDS.has(s.toUpperCase()) ? 'k' : null) : WS_TOKEN_CLASS[tk.t] || null;
    frag.appendChild(cls ? el('span', { class: cls }, s) : document.createTextNode(s));
  }
  return frag;
}

/** Redraw highlighting, line numbers and the error line marker. */
function wsSync(ta) {
  const tab = ta._tab;
  const box = ta.closest('.ed');
  if (!box) return;
  const text = ta.value;
  const hl = box.querySelector('.ed-hl');
  const frag = wsHighlight(text);
  frag.appendChild(document.createTextNode('\n '));   // keeps the last empty line scrollable
  hl.replaceChildren(frag);
  const lines = text.split('\n').length;
  const gutter = box.querySelector('.ed-gutter');
  if (gutter._lines !== lines || gutter._err !== tab.errLine) {
    gutter._lines = lines;
    gutter._err = tab.errLine;
    const inner = el('div');
    for (let i = 1; i <= lines; i++) inner.appendChild(el('div', { class: i === tab.errLine ? 'err' : null, text: String(i) }));
    clear(gutter, inner);
  }
  let mark = box.querySelector('.ed-err');
  if (tab.errLine && tab.errLine <= lines) {
    if (!mark) { mark = el('div', { class: 'ed-err' }); box.querySelector('.ed-main').prepend(mark); }
    mark._line = tab.errLine;
  } else if (mark) mark.remove();
  wsSyncScroll(ta);
}

function wsSyncScroll(ta) {
  const box = ta.closest('.ed');
  if (!box) return;
  const hl = box.querySelector('.ed-hl');
  hl.scrollTop = ta.scrollTop;
  hl.scrollLeft = ta.scrollLeft;
  const inner = box.querySelector('.ed-gutter > div');
  if (inner) inner.style.transform = `translateY(${-ta.scrollTop}px)`;
  const mark = box.querySelector('.ed-err');
  if (mark) mark.style.top = (8 + (mark._line - 1) * 20 - ta.scrollTop) + 'px';
}

/** Insert text at the selection keeping the browser's undo history; the caret ends `back` chars before the end. */
function wsType(ta, text, back = 0, replaceFrom = null) {
  if (replaceFrom !== null) ta.setSelectionRange(replaceFrom, ta.selectionEnd);
  ta.focus();
  ws.typing = true;   // our own edits do not open suggestions
  try {
    if (!document.execCommand('insertText', false, text)) {
      ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input'));
    }
  } finally { ws.typing = false; }
  if (back) { const p = ta.selectionEnd - back; ta.setSelectionRange(p, p); }
}

const WS_PAIRS = { '(': ')', '[': ']', "'": "'", '"': '"' };

function wsKeydown(e, ta) {
  const ac = ws.ac;
  const mod = e.ctrlKey || e.metaKey;
  if (ac) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'PageDown' || e.key === 'PageUp') {
      e.preventDefault();
      const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 8, PageUp: -8 }[e.key];
      ac.sel = Math.max(0, Math.min(ac.items.length - 1, ac.sel + step));
      wsDrawAc();
      return;
    }
    if ((e.key === 'Enter' || e.key === 'Tab') && !mod && !e.shiftKey) { e.preventDefault(); wsAccept(ac.items[ac.sel]); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); wsCloseAc(); return; }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' || e.key === 'End') wsCloseAc();
  }
  if (mod && e.key === 'Enter') { e.preventDefault(); if (e.shiftKey) wsRunAll(); else wsRunCurrent(); return; }
  if (mod && e.key === ' ') { e.preventDefault(); wsSuggest(ta, true); return; }
  if (mod && e.key === '/') { e.preventDefault(); wsToggleComment(ta); return; }
  if ((mod && e.shiftKey && e.key.toLowerCase() === 'f') || (mod && e.altKey && e.key.toLowerCase() === 'l')) { e.preventDefault(); wsFormat(); return; }
  if (mod && e.key.toLowerCase() === 'e') { e.preventDefault(); wsExplain(e.shiftKey); return; }
  if (e.key === 'Escape') { if (ws.ac) { e.stopPropagation(); wsCloseAc(); } return; }
  const v = ta.value, a = ta.selectionStart, b = ta.selectionEnd;
  if (e.key === 'Tab' && !mod) {
    e.preventDefault();
    if (a === b && !e.shiftKey) { wsType(ta, '  '); return; }
    wsIndent(ta, e.shiftKey);
    return;
  }
  if (e.key === 'Enter' && !mod && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    const lineStart = v.lastIndexOf('\n', a - 1) + 1;
    const indent = /^[ \t]*/.exec(v.slice(lineStart, a))[0];
    const extra = /\($/.test(v.slice(lineStart, a).trimEnd()) ? '  ' : '';
    if (extra && v[b] === ')') { wsType(ta, '\n' + indent + extra + '\n' + indent, indent.length + 1); return; }
    wsType(ta, '\n' + indent + extra);
    return;
  }
  if (e.key === 'Backspace' && a === b && a > 0 && WS_PAIRS[v[a - 1]] && v[a] === WS_PAIRS[v[a - 1]]) {
    e.preventDefault();
    ta.setSelectionRange(a - 1, a + 1);
    wsType(ta, '');
    return;
  }
  if (!mod && !e.altKey && e.key.length === 1) {
    const closing = Object.values(WS_PAIRS);
    if (closing.includes(e.key) && a === b && v[a] === e.key && (e.key === ')' || e.key === ']' || wsInsideOpenQuote(v, a, e.key))) {
      e.preventDefault();
      ta.setSelectionRange(a + 1, a + 1);
      return;
    }
    if (WS_PAIRS[e.key]) {
      const next = v[b] || '';
      const prev = v[a - 1] || '';
      const quote = e.key === "'" || e.key === '"';
      if (a !== b) { e.preventDefault(); wsType(ta, e.key + v.slice(a, b) + WS_PAIRS[e.key]); return; }
      if ((!next || /[\s),;\]]/.test(next)) && !(quote && /[\w'"]/.test(prev))) {
        e.preventDefault();
        wsType(ta, e.key + WS_PAIRS[e.key], 1);
      }
    }
  }
}

function wsInsideOpenQuote(v, pos, q) {
  const lineStart = v.lastIndexOf('\n', pos - 1) + 1;
  const count = v.slice(lineStart, pos).split(q).length - 1;
  return count % 2 === 1;
}

function wsLineRange(ta) {
  const v = ta.value;
  const a = v.lastIndexOf('\n', ta.selectionStart - 1) + 1;
  let b = ta.selectionEnd;
  if (b > a && v[b - 1] === '\n') b--;
  const e = v.indexOf('\n', b);
  return [a, e < 0 ? v.length : e];
}

function wsIndent(ta, out) {
  const [a, b] = wsLineRange(ta);
  const lines = ta.value.slice(a, b).split('\n');
  const changed = lines.map((l) => (out ? l.replace(/^( {1,2}|\t)/, '') : '  ' + l)).join('\n');
  ta.setSelectionRange(a, b);
  wsType(ta, changed);
  ta.setSelectionRange(a, a + changed.length);
}

function wsToggleComment(ta) {
  const [a, b] = wsLineRange(ta);
  const lines = ta.value.slice(a, b).split('\n');
  const all = lines.filter((l) => l.trim()).every((l) => /^\s*--/.test(l));
  const changed = lines.map((l) => (!l.trim() ? l : all ? l.replace(/^(\s*)-- ?/, '$1') : l.replace(/^(\s*)/, '$1-- '))).join('\n');
  ta.setSelectionRange(a, b);
  wsType(ta, changed);
  ta.setSelectionRange(a, a + changed.length);
}

/* ======================================================================== suggestions */

function wsLoadMeta(sid, db) {
  const k = wsKey(sid, db);
  if (ws.meta[k]) return ws.meta[k].ready;
  const m = { data: null, cols: new Map(), ready: null };
  ws.meta[k] = m;
  m.ready = api('GET', `/api/servers/${enc(sid)}/databases/${enc(db)}/completions`)
    .then((d) => { m.data = d; return d; })
    .catch((e) => { delete ws.meta[k]; if (e.code !== 'offline') toast(`Suggestions are not available: ${e.message}`, 'warn'); return null; });
  return m.ready;
}

/** Columns of schema.table from the cache, or start loading them (the popup refreshes when they arrive). */
function wsColumns(sid, db, schema, table, onReady) {
  const m = ws.meta[wsKey(sid, db)];
  if (!m) return null;
  const k = schema + '.' + table;
  const hit = m.cols.get(k);
  if (Array.isArray(hit)) return hit;
  if (!hit) {
    const p = api('GET', `/api/servers/${enc(sid)}/databases/${enc(db)}/columns?schema=${enc(schema)}&table=${enc(table)}`)
      .then((cols) => { m.cols.set(k, cols); if (onReady) onReady(); })
      .catch(() => m.cols.set(k, []));
    m.cols.set(k, p);
  }
  return null;
}

/** Find a relation by (optional) schema and name, following the search path for unqualified names. */
function wsFindRel(data, schema, name) {
  if (!data) return null;
  const rels = data.relations;
  if (schema) return rels.find((r) => r.schema === schema && r.name === name) || rels.find((r) => r.schema.toLowerCase() === schema.toLowerCase() && r.name.toLowerCase() === name.toLowerCase()) || null;
  for (const sp of data.search_path.concat(['pg_catalog'])) {
    const r = rels.find((x) => x.schema === sp && x.name === name);
    if (r) return r;
  }
  return rels.find((r) => r.name === name) || rels.find((r) => r.name.toLowerCase() === name.toLowerCase()) || null;
}

const WS_REL_KW = new Set(['FROM', 'JOIN', 'INTO', 'UPDATE', 'TABLE', 'TRUNCATE', 'ONLY', 'DESCRIBE', 'LATERAL']);
// clause keywords that end a FROM list (ON / JOIN / USING continue it)
const WS_CLAUSE_KW = new Set(['SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'SET', 'VALUES', 'RETURNING', 'INTO', 'UPDATE', 'WINDOW', 'UNION', 'EXCEPT', 'INTERSECT']);
const WS_NOT_ALIAS = new Set(['WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS', 'NATURAL', 'ON', 'USING', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET',
  'SET', 'VALUES', 'RETURNING', 'UNION', 'EXCEPT', 'INTERSECT', 'WINDOW', 'FOR', 'LATERAL', 'AS', 'OUTER', 'TABLESAMPLE', 'DEFAULT', 'SELECT', 'FETCH']);
const WS_SNIPPETS = [
  ['sel', 'SELECT * FROM ', 'SELECT * FROM …'],
  ['selc', 'SELECT count(*) FROM ', 'SELECT count(*) FROM …'],
  ['selw', 'SELECT *\nFROM \nWHERE ', 'SELECT … FROM … WHERE …'],
  ['ins', 'INSERT INTO  ()\nVALUES ();', 'INSERT INTO … VALUES …', 13],
  ['upd', 'UPDATE  SET \nWHERE ;', 'UPDATE … SET … WHERE …', 14],
  ['del', 'DELETE FROM  WHERE ;', 'DELETE FROM … WHERE …', 8],
  ['crt', 'CREATE TABLE  (\n  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY\n);', 'CREATE TABLE …', 57],
  ['expl', 'EXPLAIN (ANALYZE, BUFFERS) ', 'EXPLAIN ANALYZE …'],
  ['cte', 'WITH x AS (\n  \n)\nSELECT * FROM x;', 'WITH … AS (…) SELECT', 23],
];

/** Tables referenced in a statement: {alias or name (lower) -> {schema, name}}. */
function wsReferencedTables(text, tokens) {
  const sig = tokens.filter((t) => t.t !== ' ' && t.t !== 'c');
  const out = new Map();
  for (let i = 0; i < sig.length; i++) {
    const w = sig[i].t === 'w' ? text.slice(sig[i].a, sig[i].b).toUpperCase() : '';
    const comma = sig[i].t === 'o' && text[sig[i].a] === ',';
    if (!WS_REL_KW.has(w) && !comma) continue;
    if (comma && !wsInFromList(text, sig, i)) continue;
    let j = i + 1;
    if (sig[j] && sig[j].t === 'w' && text.slice(sig[j].a, sig[j].b).toUpperCase() === 'ONLY') j++;
    const first = sqlName(text, sig[j]);
    if (!first || (sig[j].t === 'w' && SQL_KEYWORDS.has(text.slice(sig[j].a, sig[j].b).toUpperCase()) && !/^[a-z]/.test(first))) continue;
    let schema = null, name = first;
    if (sig[j + 1] && sig[j + 1].t === 'o' && text[sig[j + 1].a] === '.' && sqlName(text, sig[j + 2])) { schema = first; name = sqlName(text, sig[j + 2]); j += 2; }
    j++;
    let alias = null;
    if (sig[j] && sig[j].t === 'w' && text.slice(sig[j].a, sig[j].b).toUpperCase() === 'AS') j++;
    if (sig[j] && (sig[j].t === 'w' || sig[j].t === 'q')) {
      const a = sqlName(text, sig[j]);
      if (sig[j].t === 'q' || !WS_NOT_ALIAS.has(a.toUpperCase())) alias = a;
    }
    const ref = { schema, name };
    out.set(name.toLowerCase(), ref);
    if (alias) out.set(alias.toLowerCase(), ref);
  }
  return out;
}

function wsInFromList(text, sig, i) {
  let depth = 0;
  for (let k = i - 1; k >= 0; k--) {
    const s = text.slice(sig[k].a, sig[k].b);
    if (sig[k].t === 'o' && s === ')') depth++;
    else if (sig[k].t === 'o' && s === '(') { if (!depth) return false; depth--; } else if (!depth && sig[k].t === 'w') {
      const u = s.toUpperCase();
      if (u === 'FROM') return true;
      if (WS_CLAUSE_KW.has(u)) return false;
    }
  }
  return false;
}

function wsMaybeSuggest(ta, e) {
  if (e && e.inputType && !e.inputType.startsWith('insert')) { if (ws.ac) wsSuggest(ta, false); return; }
  const v = ta.value, p = ta.selectionStart;
  const ch = v[p - 1] || '';
  if (/[A-Za-z0-9_$"]/.test(ch) || ch === '.') { wsSuggest(ta, false); return; }
  if (ch === ' ') {
    const prev = /([A-Za-z]+)\s$/.exec(v.slice(Math.max(0, p - 30), p));
    if (prev && WS_REL_KW.has(prev[1].toUpperCase())) { wsSuggest(ta, false, true); return; }
  }
  wsCloseAc();
}

/** Open or refresh the popup for the caret position. `forced`: Ctrl+Space; `afterKeyword`: just typed "FROM ". */
function wsSuggest(ta, forced, afterKeyword) {
  const tab = ta._tab;
  const meta = ws.meta[wsKey(tab.sid, tab.db)];
  const data = meta && meta.data;
  if (!data && meta) meta.ready.then(() => { if (document.activeElement === ta) wsSuggest(ta, forced, afterKeyword); });
  const v = ta.value, pos = ta.selectionStart;
  if (ta.selectionEnd !== pos) { wsCloseAc(); return; }
  const range = sqlStatementAt(v, pos) || [0, v.length];
  const stmtStart = Math.min(range[0], pos);
  const stmt = v.slice(stmtStart, Math.max(range[1], pos));
  const tokens = sqlTokens(stmt);
  const rel = pos - stmtStart;
  const here = tokens.find((t) => t.a < rel && t.b >= rel);
  if (here && (here.t === 's' || here.t === 'c' || here.t === 'n')) { wsCloseAc(); return; }
  // the word being typed and its qualifiers (a.b.<partial>)
  let partial = '', from = pos, quoted = false;
  if (here && (here.t === 'w' || here.t === 'q') && here.b === rel) {
    partial = stmt.slice(here.a, here.b);
    from = stmtStart + here.a;
    if (here.t === 'q') { quoted = true; partial = partial.slice(1).replace(/"$/, '').replace(/""/g, '"'); }
  }
  const before = tokens.filter((t) => t.b <= from - stmtStart && t.t !== ' ' && t.t !== 'c');
  const quals = [];
  let k = before.length - 1;
  while (k >= 1 && before[k].t === 'o' && stmt[before[k].a] === '.' && sqlName(stmt, before[k - 1]) !== null) {
    quals.unshift(sqlName(stmt, before[k - 1]));
    k -= 2;
  }
  const prevTok = before[k];
  const prevWord = prevTok && prevTok.t === 'w' ? stmt.slice(prevTok.a, prevTok.b).toUpperCase() : '';
  const prevComma = prevTok && prevTok.t === 'o' && stmt[prevTok.a] === ',';
  if (!forced && !partial && !quals.length && !afterKeyword) { wsCloseAc(); return; }
  if (!forced && partial.length < 1 && !quals.length && !afterKeyword) { wsCloseAc(); return; }

  const items = [];
  const add = (kind, label, insert, desc, rank) => items.push({ kind, label, insert: insert === undefined ? label : insert, desc: desc || '', rank: rank || 0 });
  const refs = wsReferencedTables(stmt, tokens);
  const refresh = () => { if (document.activeElement === ta) wsSuggest(ta, forced, afterKeyword); };
  const relItems = (schemaFilter) => {
    if (!data) return;
    for (const r of data.relations) {
      if (schemaFilter !== undefined ? r.schema !== schemaFilter : (r.system && !partial)) continue;
      const qualify = schemaFilter === undefined && !data.search_path.includes(r.schema);
      add(r.kind === 'view' || r.kind === 'matview' ? 'v' : 't', r.name, (qualify ? sqlIdent(r.schema) + '.' : '') + sqlIdent(r.name),
        `${r.kind}${qualify || schemaFilter === undefined ? ' · ' + r.schema : ''}`, r.system ? 3 : 0);
    }
  };
  const fnItems = (schemaFilter) => {
    if (!data) return;
    for (const f of data.functions) {
      if (schemaFilter !== undefined && f.schema !== schemaFilter) continue;
      const qualify = schemaFilter === undefined && !data.search_path.includes(f.schema);
      add('f', f.name, (qualify ? sqlIdent(f.schema) + '.' : '') + sqlIdent(f.name) + '()', `${f.kind} (${f.args}) → ${f.result || 'void'}`, 1);
    }
    if (schemaFilter === undefined || schemaFilter === 'pg_catalog') for (const n of data.builtin_functions) add('f', n, n + '()', 'built-in', 2);
  };
  const colItems = (ref, prefixed) => {
    const r = wsFindRel(data, ref.schema, ref.name);
    if (!r) return;
    const cols = wsColumns(tab.sid, tab.db, r.schema, r.name, refresh);
    for (const c of cols || []) add('c', c.name, sqlIdent(c.name), `${c.type}${c.pk ? ' · PK' : ''}${prefixed ? '' : ' · ' + r.name}`, 0);
  };

  if (quals.length) {
    const q = quals[quals.length - 1];
    const ref = quals.length === 1 ? refs.get(q.toLowerCase()) : null;
    const isSchema = data && data.schemas.some((s) => s === q || s.toLowerCase() === q.toLowerCase());
    if (ref) colItems(ref, true);
    else if (quals.length === 2 || (!isSchema && data)) colItems({ schema: quals.length === 2 ? quals[0] : null, name: q }, true);
    if (isSchema && quals.length === 1) {
      const schema = data.schemas.find((s) => s === q) || data.schemas.find((s) => s.toLowerCase() === q.toLowerCase());
      relItems(schema);
      fnItems(schema);
    }
  } else if (WS_REL_KW.has(prevWord) || (prevComma && wsInFromList(stmt, tokens.filter((t) => t.t !== ' ' && t.t !== 'c'), tokens.filter((t) => t.t !== ' ' && t.t !== 'c').indexOf(prevTok)))) {
    relItems();
    if (data) for (const s of data.schemas) if (!/^pg_|^information_schema$/.test(s) || partial) add('s', s, sqlIdent(s) + '.', 'schema', 2);
    if (prevWord === 'FROM' || prevComma) fnItems();
  } else {
    for (const ref of new Set(refs.values())) colItems(ref, false);
    if (!quoted) {
      for (const kw of SQL_KEYWORDS) add('k', kw, kw, 'keyword', 2);
      if (!partial || partial.length >= 2) for (const [key, text, desc, back] of WS_SNIPPETS) items.push({ kind: 'n', label: key, insert: text, back: back || 0, desc, rank: 1, snippet: true });
    }
    relItems();
    fnItems();
  }
  const ranked = wsRank(items, partial);
  if (!ranked.length) { wsCloseAc(); return; }
  wsOpenAc(ta, ranked.slice(0, 120), from, partial);
}

function wsRank(items, partial) {
  const p = partial.toLowerCase();
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const l = it.label.toLowerCase();
    let score;
    if (!p) score = 0;
    else if (l.startsWith(p)) score = 0;
    else if (l.includes('_' + p)) score = 1;
    else if (l.includes(p)) score = 2;
    else continue;
    const key = it.kind + '\u0000' + it.insert;
    if (seen.has(key)) continue;
    seen.add(key);
    if (it.snippet && !l.startsWith(p)) continue;
    if (p && l === p && it.kind === 'k') score = -1;
    out.push(Object.assign({ score }, it));
  }
  out.sort((a, b) => (a.score - b.score) || (a.rank - b.rank) || (a.label.length - b.label.length) || a.label.localeCompare(b.label));
  return out;
}

function wsOpenAc(ta, items, from, partial) {
  if (!ws.ac) {
    const box = el('div', { class: 'ac', role: 'listbox', onmousedown: (e) => e.preventDefault() });
    document.body.appendChild(box);
    ws.ac = { el: box };
  }
  Object.assign(ws.ac, { ta, items, from, partial, sel: ws.ac.items && ws.ac.partial === partial ? Math.min(ws.ac.sel, items.length - 1) : 0 });
  wsDrawAc();
  // position under the start of the word
  const v = ta.value;
  const lineStart = v.lastIndexOf('\n', from - 1) + 1;
  const line = sqlLineOf(v, from);
  let col = 0;
  for (let i = lineStart; i < from; i++) col += v[i] === '\t' ? 4 - (col % 4) : 1;
  const r = ta.getBoundingClientRect();
  const x = r.left + 12 + col * wsCharWidth(ta) - ta.scrollLeft;
  const y = r.top + 8 + line * 20 - ta.scrollTop;
  const box = ws.ac.el;
  const h = box.offsetHeight;
  box.style.left = Math.max(4, Math.min(x - 30, window.innerWidth - box.offsetWidth - 8)) + 'px';
  box.style.top = (y + h + 6 > window.innerHeight ? y - 20 - h - 2 : y + 2) + 'px';
}

const WS_KIND = { t: ['T', 'table'], v: ['V', 'view'], c: ['C', 'column'], f: ['ƒ', 'function'], k: ['K', 'keyword'], s: ['S', 'schema'], n: ['⌁', 'snippet'] };

function wsDrawAc() {
  const ac = ws.ac;
  if (!ac) return;
  const p = ac.partial.toLowerCase();
  clear(ac.el, ac.items.map((it, i) => {
    const l = it.label;
    const at = p ? l.toLowerCase().indexOf(p) : -1;
    const name = at >= 0 ? [l.slice(0, at), el('b', { text: l.slice(at, at + p.length) }), l.slice(at + p.length)] : l;
    return el('div', { class: 'it' + (i === ac.sel ? ' on' : ''), role: 'option', 'aria-selected': i === ac.sel ? 'true' : 'false',
      onmousedown: (e) => { e.preventDefault(); wsAccept(it); }, onmousemove: () => { if (ac.sel !== i) { ac.sel = i; wsDrawAc(); } } },
    el('span', { class: 'kd ' + it.kind, title: WS_KIND[it.kind][1], text: WS_KIND[it.kind][0] }),
    el('span', { class: 'nm' }, name), el('span', { class: 'ds', text: it.desc, title: it.desc }));
  }), el('div', { class: 'ft', text: '↑↓ choose · Enter/Tab insert · Esc close' }));
  const on = ac.el.querySelector('.it.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}

function wsAccept(it) {
  const ac = ws.ac;
  if (!ac || !it) return;
  const ta = ac.ta;
  let text = it.insert, back = it.back || 0;
  if (it.kind === 'f' && text.endsWith('()')) back = 1;
  if (ta.value[ta.selectionStart] === '"' && ac.ta.value[ac.from] === '"') ta.setSelectionRange(ta.selectionStart, ta.selectionStart + 1);
  wsCloseAc();
  wsType(ta, text, back, ac.from);
  if (it.kind === 's') wsSuggest(ta, true);
}

function wsCloseAc() { if (ws.ac) { ws.ac.el.remove(); ws.ac = null; } }

/* ======================================================================== running */

async function wsSession(tab) {
  if (tab.session) return tab.session;
  const res = await api('POST', `/api/servers/${enc(tab.sid)}/databases/${enc(tab.db)}/console`);
  tab.session = res.session_id;
  tab.tx = res.tx_status;
  if (tab.mode === 'manual') {
    const st = await api('POST', `/api/console/${enc(tab.session)}/mode`, { mode: 'manual' });
    tab.tx = st.tx_status;
  }
  return tab.session;
}

/** The selection, or else the statement at the cursor: {tab, text, line}; null (with a toast) when there is none. */
function wsCurrentStatement() {
  const ta = ws.view && ws.view.querySelector('.ed-ta');
  if (!ta) return null;
  const v = ta.value;
  let a = ta.selectionStart, b = ta.selectionEnd;
  if (a === b) {
    const r = sqlStatementAt(v, a);
    if (!r) { toast('There is no statement at the cursor.', 'warn'); return null; }
    [a, b] = r;
  }
  while (a < b && /\s/.test(v[a])) a++;
  return { tab: ta._tab, text: v.slice(a, b), line: sqlLineOf(v, a) };
}

function wsRunCurrent() {
  const cur = wsCurrentStatement();
  if (cur) wsRun(cur.tab, cur.text, cur.line);
}

/** EXPLAIN the statement at the cursor; `analyze` runs it inside a transaction that is rolled back. */
async function wsExplain(analyze) {
  const cur = wsCurrentStatement();
  if (!cur || !cur.text.trim()) return;
  const tab = cur.tab;
  if (tab.running) return;
  wsCloseAc();
  tab.running = true;
  tab.errLine = null;
  wsRender();
  try {
    await wsSession(tab);
    const res = await api('POST', `/api/console/${enc(tab.session)}/explain`, { sql: cur.text, analyze: !!analyze, timeout_s: tab.timeout });
    tab.tx = res.tx_status;
    tab.plan = Object.assign(res, { baseLine: cur.line, at: new Date() });
    tab.active = 'plan';
    if (res.error && res.error.line) tab.errLine = cur.line + res.error.line - 1;
  } catch (e) {
    if (e.code === 'session_lost') { tab.session = null; tab.tx = 'idle'; }
    toastError(e, 'Not explained');
  } finally {
    tab.running = false;
  }
  wsRender();
}

/** Format the selection, or the whole editor, keeping the browser's undo history. */
function wsFormat() {
  const ta = ws.view && ws.view.querySelector('.ed-ta');
  if (!ta) return;
  let a = ta.selectionStart, b = ta.selectionEnd;
  if (a === b) { a = 0; b = ta.value.length; }
  const src = ta.value.slice(a, b);
  if (!src.trim()) return;
  const out = sqlFormat(src) + (/\n\s*$/.test(src) ? '\n' : '');
  if (out === src) return;
  ta.setSelectionRange(a, b);
  wsType(ta, out);
  ta.setSelectionRange(a, a + out.length);
}

function wsRunAll() {
  const tab = wsTab();
  if (!tab.sql.trim()) { toast('The editor is empty.', 'warn'); return; }
  wsRun(tab, tab.sql, 1);
}

/** Ask before dropping unsaved grid edits on the tab's results; true when there are none or the user agrees. */
async function wsKeepEdits(tab, action) {
  const results = tab.kind === 'table' ? [tab.data] : tab.results || [];
  const n = results.reduce((s, r) => s + wsGridPending(r), 0);
  if (!n) return true;
  return confirmBox({ title: 'Discard unsaved changes?', danger: true, confirmLabel: 'Discard changes',
    message: `${fmt.plural(n, 'change')} in the grid ${n === 1 ? 'was' : 'were'} not submitted. ${action} drops ${n === 1 ? 'it' : 'them'}.` });
}

async function wsRun(tab, sqlText, baseLine, retried) {
  if (tab.running || !sqlText.trim()) return;
  if (!retried && !(await wsKeepEdits(tab, 'Running a statement'))) return;
  wsCloseAc();
  tab.running = true;
  tab.errLine = null;
  const t0 = performance.now();
  wsRender();
  let ok = false;
  try {
    await wsSession(tab);
    const res = await api('POST', `/api/console/${enc(tab.session)}/execute`, { sql: sqlText, max_rows: tab.maxRows, timeout_s: tab.timeout });
    tab.tx = res.tx_status;
    tab.results = res.results.map((r) => Object.assign(r, { baseLine }));
    tab.ranAt = new Date();
    const firstRows = tab.results.findIndex((r) => r.has_rows);
    const failed = tab.results.find((r) => r.error);
    tab.active = failed ? tab.results.length : firstRows >= 0 ? firstRows : tab.results.length;
    if (failed && failed.error.line) tab.errLine = baseLine + failed.error.line - 1;
    ok = !failed;
    if (/^\s*(create|alter|drop|comment)\b/im.test(sqlText) && ok) {
      if (ws.set && ws.set.sid === tab.sid && ws.set.db === tab.db) wxRefresh(ws.set, true);
      else delete ws.meta[wsKey(tab.sid, tab.db)];
    }
  } catch (e) {
    if (e.code === 'session_lost') {
      const hadTx = tab.tx === 'in_transaction' || tab.tx === 'failed';
      tab.session = null;
      tab.tx = 'idle';
      if (!retried && !hadTx) { tab.running = false; return wsRun(tab, sqlText, baseLine, true); }
      toast(hadTx ? 'The connection was lost and the open transaction was rolled back. Run again to reconnect.' : e.message, 'err');
    } else toastError(e, 'Query not run');
  } finally {
    tab.running = false;
  }
  wsAddHistory(tab, sqlText.trim(), ok, performance.now() - t0);
  wsRender();
}

async function wsCancel(tab) {
  if (!tab.session || !tab.running) return;
  try { await api('POST', `/api/console/${enc(tab.session)}/cancel`); } catch (e) { toastError(e); }
}

async function wsSetMode(tab, mode, select) {
  const old = tab.mode;
  tab.mode = mode;
  if (!tab.session) { wsRender(); return; }
  try {
    const st = await api('POST', `/api/console/${enc(tab.session)}/mode`, { mode });
    tab.tx = st.tx_status;
  } catch (e) {
    tab.mode = old;
    select.value = old;
    if (e.code === 'session_lost') { tab.session = null; tab.mode = mode; } else toastError(e);
  }
  wsRender();
}

async function wsEndTx(tab, what) {
  if (!tab.session) return;
  try {
    const st = what === 'commit' ? await api('POST', `/api/console/${enc(tab.session)}/commit`)
      : await api('POST', `/api/console/${enc(tab.session)}/rollback`);
    tab.tx = st.tx_status;
    toast(what === 'commit' ? 'Changes committed.' : 'Changes rolled back.', 'ok');
  } catch (e) {
    if (e.code === 'session_lost') { tab.session = null; tab.tx = 'idle'; }
    toastError(e);
  }
  wsRender();
}

async function wsDisconnect(tab) {
  if (tab.tx === 'in_transaction' || tab.tx === 'failed') {
    const yes = await confirmBox({ title: 'Disconnect with an open transaction?', danger: true, confirmLabel: 'Roll back and disconnect',
      message: 'The changes that were not committed are rolled back.' });
    if (!yes) return;
  }
  const id = tab.session;
  tab.session = null;
  tab.tx = 'idle';
  try { await api('DELETE', `/api/console/${enc(id)}`); } catch (e) { /* already gone */ }
  wsRender();
}

/* ======================================================================== results */

function wsResults(tab) {
  const wrap = el('div', { class: 'ws-res' });
  const res = tab.results || [];
  const plan = tab.plan;
  if (tab.running && !res.length && !plan) { wrap.appendChild(el('div', { class: 'ws-empty' }, el('span', { class: 'spin' }), ' Running…')); return wrap; }
  if (!res.length && !plan) {
    wrap.appendChild(el('div', { class: 'ws-empty' }, el('div', null,
      el('div', { style: 'font-weight: 600; margin-bottom: 6px', text: 'No results yet' }),
      el('div', { text: 'Ctrl+Enter runs the statement at the cursor · Ctrl+Shift+Enter runs all · Ctrl+E explains · Ctrl+Space suggests · Ctrl+Shift+F formats · Ctrl+/ comments lines' }))));
    return wrap;
  }
  if (tab.active === 'plan' && !plan) tab.active = res.length;
  if (!res.length) tab.active = 'plan';
  const tabs = el('div', { class: 'ws-rtabs', role: 'tablist' });
  if (plan) {
    tabs.appendChild(el('button', { type: 'button', role: 'tab', class: 'ws-rtab' + (tab.active === 'plan' ? ' on' : '') + (plan.error ? ' err' : ''),
      onclick: () => { tab.active = 'plan'; wsRender(); } }, plan.analyze ? 'Plan (analyze)' : 'Plan'));
  }
  if (tab.active === 'plan') {
    res.forEach((r, i) => { if (r.has_rows || r.error) tabs.appendChild(el('button', { type: 'button', role: 'tab', class: 'ws-rtab' + (r.error ? ' err' : ''), onclick: () => { tab.active = i; wsRender(); } }, r.error ? 'Error' : `Result ${res.slice(0, i + 1).filter((x) => x.has_rows).length}`)); });
    if (res.length) tabs.appendChild(el('button', { type: 'button', role: 'tab', class: 'ws-rtab', onclick: () => { tab.active = res.length; wsRender(); } }, `Messages (${res.length})`));
    if (tab.running) tabs.appendChild(el('span', { class: 'subtle', style: 'margin-left: 8px' }, el('span', { class: 'spin', style: 'width:10px;height:10px;border-width:2px' }), ' running…'));
    wrap.append(tabs, wxPlanView(tab, plan));
    return wrap;
  }
  res.forEach((r, i) => {
    if (!r.has_rows && !r.error) return;
    tabs.appendChild(el('button', { type: 'button', role: 'tab', class: 'ws-rtab' + (tab.active === i ? ' on' : '') + (r.error ? ' err' : ''),
      onclick: () => { tab.active = i; wsRender(); } }, r.error ? 'Error' : `Result ${res.slice(0, i + 1).filter((x) => x.has_rows).length}`));
  });
  const nErr = res.filter((r) => r.error).length;
  tabs.appendChild(el('button', { type: 'button', role: 'tab', class: 'ws-rtab' + (tab.active >= res.length ? ' on' : ''), onclick: () => { tab.active = res.length; wsRender(); } },
    `Messages (${res.length})`));
  if (tab.running) tabs.appendChild(el('span', { class: 'subtle', style: 'margin-left: 8px' }, el('span', { class: 'spin', style: 'width:10px;height:10px;border-width:2px' }), ' running…'));
  wrap.appendChild(tabs);
  const cur = res[tab.active];
  if (cur && cur.has_rows) {
    // after saving edits the statement runs again to show the rows as stored
    wrap.appendChild(wsGrid(cur, { tab, onSaved: () => wsRun(tab, cur.statement, cur.baseLine + (cur.line || 1) - 1) }));
    wrap.appendChild(el('div', { class: 'ws-status' },
      el('span', { text: `${fmt.num(cur.row_count)} row${cur.row_count === 1 ? '' : 's'}${cur.truncated ? ` (first ${fmt.num(cur.row_count)}; raise the row limit to see more)` : ''}` }),
      el('span', { text: `${cur.duration_ms} ms` }), el('span', { text: cur.status }), el('span', { class: 'grow' }),
      el('span', { text: tab.ranAt ? tab.ranAt.toLocaleTimeString() : '' })));
  } else if (cur && cur.error) {
    wrap.appendChild(wsMessages(tab, [cur]));
  } else {
    wrap.appendChild(wsMessages(tab, res));
    wrap.appendChild(el('div', { class: 'ws-status' },
      el('span', { text: `${fmt.plural(res.length, 'statement')}${nErr ? ' · stopped at an error' : ''}` }),
      el('span', { text: `${Math.round(res.reduce((s, r) => s + (r.duration_ms || 0), 0) * 10) / 10} ms` }), el('span', { class: 'grow' }),
      el('span', { text: tab.ranAt ? tab.ranAt.toLocaleTimeString() : '' })));
  }
  return wrap;
}

function wsMessages(tab, list) {
  return el('div', { class: 'ws-msgs' }, list.map((r) => {
    const line = r.baseLine + (r.line || 1) - 1;
    const e = r.error;
    return el('div', { class: 'ws-msg' },
      el('div', null, el('span', { class: e ? 'er' : 'st', text: e ? 'ERROR' : r.status || 'OK' }),
        el('span', { class: 'subtle', text: ` · ${r.duration_ms} ms · ` }), el('a', { onclick: () => wsGotoLine(e && e.line ? r.baseLine + e.line - 1 : line), text: `line ${e && e.line ? r.baseLine + e.line - 1 : line}` }),
        !e && r.has_rows ? el('span', { class: 'subtle', text: ` · ${fmt.plural(r.row_count, 'row')}${r.truncated ? '+' : ''}` }) : null),
      e ? el('div', { class: 'er', text: e.message + (e.sqlstate ? `  [${e.sqlstate}]` : '') }) : null,
      e && e.detail ? el('div', { class: 'nt', text: 'Detail: ' + e.detail }) : null,
      e && e.hint ? el('div', { class: 'nt', text: 'Hint: ' + e.hint }) : null,
      (r.notices || []).map((n) => el('div', { class: 'nt', text: `${n.severity}: ${n.message}` })),
      el('pre', { text: r.statement }));
  }));
}

function wsGotoLine(line) {
  const ta = ws.view && ws.view.querySelector('.ed-ta');
  if (!ta) return;
  const lines = ta.value.split('\n');
  let pos = 0;
  for (let i = 0; i < Math.min(line - 1, lines.length); i++) pos += lines[i].length + 1;
  ta.focus();
  ta.setSelectionRange(pos, pos + (lines[line - 1] || '').length);
  ta.scrollTop = Math.max(0, (line - 4) * 20);
}
