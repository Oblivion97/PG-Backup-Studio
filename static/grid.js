/* PG Backup Studio: result grid of the SQL workspace (Step 8a, 8c). Only the visible rows are in the DOM.
 *
 * 8c: client sort and filter, column resize and hide, cell range selection, copy as TSV / CSV / JSON / INSERT /
 * Markdown, export to a file, value viewer, and editing (cells, NULL, DEFAULT, add / duplicate / delete rows) for
 * results that come from one table with its primary key (`result.editable`, decided by the server). Changes stay
 * pending on the result until Submit (apply-edits) or Revert.
 *
 * Grid state lives on the result object (`result._g`), so it survives the workspace redrawing the grid.
 * Globals start with `wsGrid` / `WS_GRID`. Text is always set through text nodes (decision 008 §2).
 */
'use strict';

const WS_GRID_CSS = `
.gr-box { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.gr-bar { display: flex; gap: 6px; align-items: center; padding: 4px 10px; border-bottom: 1px solid var(--border); background: var(--surface); flex: none; flex-wrap: wrap; min-height: 34px; }
.gr-bar .btn.sm { height: 26px; }
.gr-bar .ro { color: var(--fg-3); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 520px; display: flex; gap: 5px; align-items: center; }
.gr-bar .ro .icon { width: 13px; height: 13px; flex: none; }
.gr-bar .cnt { color: var(--fg-3); font-size: 12px; }
.gr-bar .pend { color: var(--warn); font-size: 12px; font-weight: 600; }
.gr { flex: 1; position: relative; overflow: auto; min-height: 0; font-family: var(--mono); font-size: 12.5px; outline: none; }
.gr-in { position: relative; width: max-content; min-width: 100%; }
.gr-head { position: sticky; top: 0; z-index: 3; background: var(--surface-2); border-bottom: 1px solid var(--border-strong); }
.gr-hrow { display: flex; }
.gr-c { flex: none; box-sizing: border-box; padding: 0 8px; height: 26px; line-height: 26px; overflow: hidden; text-overflow: ellipsis;
  white-space: pre; border-right: 1px solid var(--border); }
.gr-head .gr-c { height: 40px; line-height: 15px; padding-top: 5px; font-family: inherit; position: relative; cursor: pointer; user-select: none; }
.gr-head .gr-c:hover { background: var(--surface-3); }
.gr-head .gr-c .nm { display: block; font-weight: 650; color: var(--fg); overflow: hidden; text-overflow: ellipsis; }
.gr-head .gr-c .ty { display: block; color: var(--fg-3); font-size: 11px; overflow: hidden; text-overflow: ellipsis; }
.gr-head .gr-c .key { color: var(--warn); font-size: 10px; font-weight: 700; margin-left: 4px; }
.gr-rs { position: absolute; top: 0; right: -3px; width: 7px; height: 100%; cursor: col-resize; z-index: 2; }
.gr-rs:hover { background: var(--accent); opacity: .4; }
.gr-frow .gr-c { height: 30px; padding: 3px 4px; cursor: default; }
.gr-frow .gr-c:hover { background: none; }
.gr-frow input { width: 100%; height: 23px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 4px; background: var(--surface);
  color: var(--fg); font: inherit; font-size: 12px; padding: 0 5px; outline: none; }
.gr-frow input:focus { border-color: var(--accent); }
.gr-body { position: relative; }
.gr-row { position: absolute; left: 0; display: flex; border-bottom: 1px solid var(--border); background: var(--surface); }
.gr-row:hover .gr-c { background: var(--surface-2); }
.gr-n { position: sticky; left: 0; z-index: 2; background: var(--surface-2) !important; color: var(--fg-3); text-align: right; cursor: default; }
.gr-head .gr-n { z-index: 4; cursor: default; }
.gr-null, .gr-def { color: var(--fg-3); font-style: italic; }
.gr-num { text-align: right; }
.gr-c.mod { background: var(--warn-soft) !important; }
.gr-row.del .gr-c { text-decoration: line-through; color: var(--danger); background: var(--danger-soft) !important; }
.gr-row.new .gr-c { background: var(--ok-soft); }
.gr-row.new .gr-n { color: var(--ok); }
.gr-c.sel { background: var(--accent-soft) !important; }
.gr-c.cur { box-shadow: inset 0 0 0 2px var(--accent); }
.gr-ed { position: absolute; z-index: 5; box-sizing: border-box; height: 27px; border: 2px solid var(--accent); background: var(--surface); color: var(--fg);
  font: inherit; padding: 0 6px; outline: none; }
.gr-foot { display: flex; gap: 10px; align-items: center; padding: 4px 12px; border-top: 1px solid var(--border); background: var(--surface); font-size: 12px; min-height: 26px; flex: none; }
.gr-foot .v { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; color: var(--fg-2); }
.gr-view { width: 100%; min-height: 300px; max-height: 60vh; box-sizing: border-box; font-family: var(--mono); font-size: 12.5px; line-height: 19px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); color: var(--fg); resize: vertical; tab-size: 2; white-space: pre; }
.gr-sqlprev { max-height: 55vh; }
`;
(function wsGridStyle() {
  const s = document.createElement('style');
  s.textContent = WS_GRID_CSS;
  document.head.appendChild(s);
})();

const WS_GRID_ROW = 27;           // row height incl. border
const WS_GRID_SHOW = 300;         // characters shown in a cell
const WS_GRID_NUMERIC = /^(smallint|integer|bigint|numeric|real|double precision|money|oid)/;
const WS_DEFAULT = Object.freeze({ default: true });   // a pending "set to DEFAULT"

/* ======================================================================== state and edits */

function wsGridState(result) {
  if (!result._g) {
    result._g = { sortCol: -1, desc: false, filters: {}, showFilter: false, hidden: new Set(), widths: null,
      upd: new Map(), del: new Set(), ins: [] };
  }
  return result._g;
}

/** Number of unsaved changes on a result (cells changed, rows deleted, rows added). */
function wsGridPending(result) {
  const g = result && result._g;
  if (!g) return 0;
  let n = g.del.size + g.ins.length;
  for (const [r, m] of g.upd) if (!g.del.has(r)) n += m.size;
  return n;
}

function wsGridRevert(result) {
  const g = wsGridState(result);
  g.upd.clear();
  g.del.clear();
  g.ins = [];
}

/** Value shown for row id `r` (>= 0: a fetched row, < 0: an added row) and column `c`. */
function wsGridValue(result, r, c) {
  const g = result._g;
  if (r < 0) { const v = g.ins[-r - 1][c]; return v === undefined ? WS_DEFAULT : v; }
  const m = g.upd.get(r);
  return m && m.has(c) ? m.get(c) : result.rows[r][c];
}

function wsGridSet(result, r, c, v) {
  const g = result._g;
  if (r < 0) { g.ins[-r - 1][c] = v; return; }
  let m = g.upd.get(r);
  if (v === result.rows[r][c]) { if (m) { m.delete(c); if (!m.size) g.upd.delete(r); } return; }
  if (!m) g.upd.set(r, m = new Map());
  m.set(c, v);
}

/** Pending edits as apply-edits changes: deletes, then updates, then inserts. */
function wsGridChanges(result) {
  const g = result._g, ed = result.editable, out = [];
  const key = (r) => Object.fromEntries(ed.key.map((c) => [ed.columns[c], result.rows[r][c]]));
  const val = (v) => (v === WS_DEFAULT ? { default: true } : v);
  for (const r of g.del) if (r >= 0) out.push({ op: 'delete', key: key(r) });
  for (const [r, m] of g.upd) if (!g.del.has(r)) out.push({ op: 'update', key: key(r), set: Object.fromEntries([...m].map(([c, v]) => [ed.columns[c], val(v)])) });
  for (const row of g.ins) {
    const values = {};
    row.forEach((v, c) => { if (v !== undefined && v !== WS_DEFAULT) values[ed.columns[c]] = v; });
    out.push({ op: 'insert', values });
  }
  return out;
}

/** Row ids in display order: fetched rows filtered and sorted on the client, then the added rows. */
function wsGridView(result) {
  const g = result._g;
  const cols = result.columns;
  let ids = result.rows.map((_, i) => i);
  const fs = Object.entries(g.filters).filter(([, f]) => f);
  if (fs.length) {
    ids = ids.filter((r) => fs.every(([c, f]) => {
      const v = wsGridValue(result, r, Number(c));
      if (f === 'NULL') return v === null;
      return typeof v === 'string' && v.toLowerCase().includes(f.toLowerCase());
    }));
  }
  if (g.sortCol >= 0 && g.sortCol < cols.length) {
    const c = g.sortCol;
    const num = WS_GRID_NUMERIC.test(cols[c].type || '');
    const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    ids.sort((a, b) => {
      const x = result.rows[a][c], y = result.rows[b][c];
      if (x === null || y === null) return x === y ? a - b : x === null ? 1 : -1;   // NULLs last either way
      const d = num ? Number(x) - Number(y) : coll.compare(x, y);
      return (g.desc ? -d : d) || a - b;
    });
  }
  return ids.concat(g.ins.map((_, k) => -1 - k));
}

/* ======================================================================== formats */

function wsGridSqlLiteral(v, type) {
  if (v === null || v === WS_DEFAULT) return 'NULL';
  if (WS_GRID_NUMERIC.test(type || '') && /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(v)) return v;
  if (type === 'boolean' && (v === 't' || v === 'f')) return v === 't' ? 'TRUE' : 'FALSE';
  return "'" + v.replace(/'/g, "''") + "'";
}

/** `data` (rows of text / null) in one of: tsv (no header), csv, json, insert, markdown. */
function wsGridFormat(kind, names, types, data, table) {
  const plain = (v) => (v === null || v === WS_DEFAULT ? null : v);
  const quoted = (v, sep) => { v = plain(v); if (v === null) return ''; return /["\r\n]/.test(v) || v.includes(sep) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  if (kind === 'tsv') return data.map((r) => r.map((v) => quoted(v, '\t')).join('\t')).join('\n');
  if (kind === 'csv') return [names].concat(data).map((r) => r.map((v) => quoted(v, ',')).join(',')).join('\r\n') + '\r\n';
  if (kind === 'json') return JSON.stringify(data.map((r) => Object.fromEntries(names.map((n, i) => [n, plain(r[i])]))), null, 2) + '\n';
  if (kind === 'insert') {
    const cols = names.map((n) => sqlIdent(n)).join(', ');
    return data.map((r) => `INSERT INTO ${table} (${cols}) VALUES (${r.map((v, i) => wsGridSqlLiteral(v, types[i])).join(', ')});`).join('\n') + '\n';
  }
  const md = (v) => { v = plain(v); return v === null ? 'NULL' : v.replace(/\|/g, '\\|').replace(/\r?\n/g, ' '); };
  return [`| ${names.map(md).join(' | ')} |`, `| ${names.map((_, i) => (WS_GRID_NUMERIC.test(types[i] || '') ? '---:' : '---')).join(' | ')} |`]
    .concat(data.map((r) => `| ${r.map(md).join(' | ')} |`)).join('\n') + '\n';
}

const WS_GRID_FORMATS = { csv: ['CSV', 'text/csv'], json: ['JSON', 'application/json'], insert: ['SQL INSERT', 'application/sql'],
  markdown: ['Markdown', 'text/markdown'], tsv: ['TSV', 'text/tab-separated-values'] };
const WS_GRID_EXT = { csv: 'csv', json: 'json', insert: 'sql', markdown: 'md', tsv: 'tsv' };

function wsGridDownload(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }));
  const a = el('a', { href: url, download: name, style: 'display:none' });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function wsGridCopy(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('Copied.', 'ok'), () => toast('The browser did not allow copying.', 'warn'));
  } else toast('The browser does not allow copying here.', 'warn');
}

/* ======================================================================== grid */

/** Grid for one statement result. opts: {tab, sort, onSort(name, add) (server sort), firstRow, onSaved()}. */
function wsGrid(result, opts) {
  opts = opts || {};
  const g = wsGridState(result);
  const cols = result.columns;
  const rows = result.rows;
  const ed = result.editable || null;
  const keyCols = new Set(ed ? ed.key : []);
  const first = opts.firstRow || 1;
  const numeric = cols.map((c) => WS_GRID_NUMERIC.test(c.type || ''));
  if (!g.widths || g.widths.length !== cols.length) {
    g.widths = cols.map((c, i) => {
      let w = Math.max(c.name.length + (keyCols.has(i) ? 3 : 0), (c.type || '').length * 0.85);
      for (let r = 0; r < Math.min(rows.length, 200); r++) { const v = rows[r][i]; w = Math.max(w, v === null ? 4 : Math.min(v.length, 60)); }
      return Math.round(Math.max(60, Math.min(420, w * 7.4 + 20)));
    });
  }
  let view = wsGridView(result);
  let vis = [];        // visible column indexes in order
  let lefts = [];      // left offset per visible position
  let numW = 44;
  const sel = { a: null, b: null };   // anchor and focus {i: display index, p: visible position}
  let editor = null;
  let lastDown = { i: -1, p: -1, at: 0 };

  const scroller = el('div', { class: 'gr', tabindex: '0', role: 'grid' });
  const inner = el('div', { class: 'gr-in' });
  const head = el('div', { class: 'gr-head' });
  const body = el('div', { class: 'gr-body' });
  const bar = el('div', { class: 'gr-bar' });
  const foot = el('div', { class: 'gr-foot' }, el('span', { class: 'subtle', text: ed ? 'Click a cell to select it · double-click or Enter edits · right-click for more' : 'Click a cell to see its full value · right-click for more' }));
  inner.append(head, body);
  scroller.appendChild(inner);

  const layout = () => {
    vis = cols.map((_, i) => i).filter((i) => !g.hidden.has(i));
    numW = Math.max(44, String(view.length + first).length * 8 + 20);
    let x = numW;
    lefts = vis.map((c) => { const l = x; x += g.widths[c]; return l; });
    body.style.height = view.length * WS_GRID_ROW + 'px';
    scroller.setAttribute('aria-rowcount', String(view.length + 1));
  };

  /* ---------- header and filter row ---------- */
  const sortMark = (c) => {
    if (opts.onSort) {
      const list = opts.sort || [];
      const k = list.findIndex((o) => o.column === cols[c].name);
      return k < 0 ? '' : (list[k].desc ? ' ▼' : ' ▲') + (list.length > 1 ? String(k + 1) : '');
    }
    return g.sortCol === c ? (g.desc ? ' ▼' : ' ▲') : '';
  };
  const drawHead = () => {
    const hrow = el('div', { class: 'gr-hrow', role: 'row' }, el('div', { class: 'gr-c gr-n', style: `width:${numW}px`, title: 'Right-click a header for column options' }, ''));
    for (const c of vis) {
      const col = cols[c];
      const rs = el('div', { class: 'gr-rs', title: 'Drag to resize', onclick: (e) => e.stopPropagation() });
      rs.addEventListener('mousedown', (e) => resizeStart(e, c));
      const cell = el('div', { class: 'gr-c', role: 'columnheader', style: `width:${g.widths[c]}px`,
        title: `${col.name} (${col.type})${ed && ed.columns[c] !== col.name ? `\ncolumn ${ed.columns[c]}` : ''}\nClick to sort${opts.onSort ? ', Shift+click adds a sort column' : ''} · right-click for options` },
      el('span', { class: 'nm' }, col.name + sortMark(c), keyCols.has(c) ? el('span', { class: 'key', text: 'PK' }) : null),
      el('span', { class: 'ty', text: col.type }), rs);
      cell.addEventListener('click', (e) => sortBy(c, e.shiftKey));
      cell.addEventListener('contextmenu', (e) => { e.preventDefault(); menu(cell, headMenu(c)); });
      hrow.appendChild(cell);
    }
    const parts = [hrow];
    if (g.showFilter) {
      const frow = el('div', { class: 'gr-hrow gr-frow' }, el('div', { class: 'gr-c gr-n', style: `width:${numW}px` }, ''));
      for (const c of vis) {
        const input = el('input', { value: g.filters[c] || '', placeholder: 'filter', 'aria-label': `Filter ${cols[c].name}`, spellcheck: 'false',
          title: 'Rows whose value contains this text (any case); NULL matches empty values' });
        input.addEventListener('input', () => { g.filters[c] = input.value; refilter(); });
        input.addEventListener('keydown', (e) => e.stopPropagation());
        frow.appendChild(el('div', { class: 'gr-c', style: `width:${g.widths[c]}px` }, input));
      }
      parts.push(frow);
    }
    head.replaceChildren(...parts);
  };

  const sortBy = (c, add) => {
    if (opts.onSort) { if (wsGridPending(result)) { toast('Submit or revert the changes before sorting.', 'warn'); return; } opts.onSort(cols[c].name, add); return; }
    if (g.sortCol !== c) { g.sortCol = c; g.desc = false; } else if (!g.desc) g.desc = true; else g.sortCol = -1;
    rebuild();
  };

  const resizeStart = (e, c) => {
    e.preventDefault();
    e.stopPropagation();
    const x0 = e.clientX, w0 = g.widths[c];
    const move = (ev) => { g.widths[c] = Math.max(40, Math.min(1600, w0 + ev.clientX - x0)); layout(); drawHead(); draw(true); };
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  };

  /* ---------- body ---------- */
  const range = () => {
    if (!sel.a) return null;
    return { i0: Math.min(sel.a.i, sel.b.i), i1: Math.max(sel.a.i, sel.b.i), p0: Math.min(sel.a.p, sel.b.p), p1: Math.max(sel.a.p, sel.b.p) };
  };
  let drawn = '';
  const draw = (force) => {
    const top = scroller.scrollTop;
    const lo = Math.max(0, Math.floor(top / WS_GRID_ROW) - 10);
    const hi = Math.min(view.length, Math.ceil((top + (scroller.clientHeight || 600)) / WS_GRID_ROW) + 10);
    const rg = range();
    const key = `${lo}:${hi}:${rg ? Object.values(rg).join(',') : ''}:${sel.b ? sel.b.i + ',' + sel.b.p : ''}`;
    if (key === drawn && !force) return;
    drawn = key;
    const frag = document.createDocumentFragment();
    for (let i = lo; i < hi; i++) {
      const r = view[i];
      const m = r >= 0 ? g.upd.get(r) : null;
      const row = el('div', { class: 'gr-row' + (r < 0 ? ' new' : g.del.has(r) ? ' del' : ''), role: 'row', style: `top:${i * WS_GRID_ROW}px`, 'data-i': String(i) },
        el('div', { class: 'gr-c gr-n', style: `width:${numW}px`, text: r < 0 ? '+' : String(r + first), title: r < 0 ? 'Added row (not saved)' : g.del.has(r) ? 'Deleted (not saved)' : null }));
      vis.forEach((c, p) => {
        const v = wsGridValue(result, r, c);
        let cls = 'gr-c';
        if (v === null) cls += ' gr-null'; else if (v === WS_DEFAULT) cls += ' gr-def'; else if (numeric[c]) cls += ' gr-num';
        if (m && m.has(c)) cls += ' mod';
        if (rg && i >= rg.i0 && i <= rg.i1 && p >= rg.p0 && p <= rg.p1) cls += ' sel';
        if (sel.b && sel.b.i === i && sel.b.p === p) cls += ' cur';
        const text = v === null ? 'NULL' : v === WS_DEFAULT ? 'DEFAULT' : v.length > WS_GRID_SHOW ? v.slice(0, WS_GRID_SHOW) + '…' : v.replace(/\n/g, '↵');
        row.appendChild(el('div', { class: cls, style: `width:${g.widths[c]}px`, 'data-p': String(p) }, text));
      });
      frag.appendChild(row);
    }
    body.replaceChildren(frag);
  };

  const showFoot = () => {
    if (!sel.b) return;
    const r = view[sel.b.i], c = vis[sel.b.p];
    const v = wsGridValue(result, r, c);
    const rg = range();
    const n = rg ? (rg.i1 - rg.i0 + 1) * (rg.p1 - rg.p0 + 1) : 1;
    const shown = v === null ? 'NULL' : v === WS_DEFAULT ? 'DEFAULT' : v;
    clear(foot, el('span', { class: 'badge', text: `${cols[c].name} · ${r < 0 ? 'new row' : 'row ' + (r + first)}` }),
      n > 1 ? el('span', { class: 'subtle', text: `${fmt.num(n)} cells selected` }) : null,
      el('span', { class: 'v', text: shown, title: shown.slice(0, 2000) }),
      el('button', { class: 'btn sm ghost', type: 'button', onclick: () => viewValue() }, icon('search'), 'View'),
      el('button', { class: 'btn sm ghost', type: 'button', onclick: () => copySel('tsv') }, icon('copy'), 'Copy'));
  };

  const select = (i, p, extend) => {
    if (!view.length || !vis.length) return;
    i = Math.max(0, Math.min(view.length - 1, i));
    p = Math.max(0, Math.min(vis.length - 1, p));
    if (!extend || !sel.a) sel.a = { i, p };
    sel.b = { i, p };
    draw();
    showFoot();
    const y = i * WS_GRID_ROW, hh = head.offsetHeight;
    if (y < scroller.scrollTop) scroller.scrollTop = y;
    else if (y + WS_GRID_ROW > scroller.scrollTop + scroller.clientHeight - hh) scroller.scrollTop = y + WS_GRID_ROW - scroller.clientHeight + hh;
    const x = lefts[p], w = g.widths[vis[p]];
    if (x - numW < scroller.scrollLeft) scroller.scrollLeft = Math.max(0, x - numW);
    else if (x + w > scroller.scrollLeft + scroller.clientWidth) scroller.scrollLeft = x + w - scroller.clientWidth;
  };

  const rebuild = () => {   // after sort / filter / hide / row changes
    closeEditor(true);
    view = wsGridView(result);
    sel.a = sel.b = null;
    layout();
    drawHead();
    draw(true);
    drawBar();
  };
  let filterTimer = null;
  const refilter = () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { view = wsGridView(result); sel.a = sel.b = null; layout(); draw(true); drawBar(); }, 120);
  };
  const changed = () => { draw(true); drawBar(); showFoot(); };

  /* ---------- selection data, copy, export ---------- */
  const selData = () => {
    const rg = range();
    if (!rg) return null;
    const cs = vis.slice(rg.p0, rg.p1 + 1);
    return { cs, data: view.slice(rg.i0, rg.i1 + 1).map((r) => cs.map((c) => wsGridValue(result, r, c))) };
  };
  const tableName = () => (ed ? sqlIdent(ed.schema) + '.' + sqlIdent(ed.table) : 'table_name');
  const copySel = (kind) => {
    const d = selData();
    if (!d) { toast('Select cells first.', 'warn'); return; }
    wsGridCopy(wsGridFormat(kind, d.cs.map((c) => (ed && kind === 'insert' ? ed.columns[c] : cols[c].name)), d.cs.map((c) => cols[c].type), d.data, tableName()).replace(/\n$/, ''));
  };
  const exportAll = (kind) => {
    const ids = view.filter((r) => !(r >= 0 && g.del.has(r)));
    const names = vis.map((c) => (ed && kind === 'insert' ? ed.columns[c] : cols[c].name));
    const text = wsGridFormat(kind, names, vis.map((c) => cols[c].type), ids.map((r) => vis.map((c) => wsGridValue(result, r, c))), tableName());
    const base = ((opts.tab && opts.tab.title) || 'result').replace(/[^\w.-]+/g, '_').slice(0, 60) || 'result';
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('T', '_').slice(0, 15);
    wsGridDownload(`${base}_${stamp}.${WS_GRID_EXT[kind]}`, text, WS_GRID_FORMATS[kind][1]);
    toast(`Exported ${fmt.plural(ids.length, 'row')} as ${WS_GRID_FORMATS[kind][0]}.`, 'ok');
  };

  /* ---------- editing ---------- */
  const canEdit = () => !!ed && !!opts.tab;
  const editableCell = (i, p) => canEdit() && !(view[i] >= 0 && g.del.has(view[i]));
  const closeEditor = (commit) => {
    if (!editor) return;
    const { input, r, c } = editor;
    editor = null;
    if (commit) wsGridSet(result, r, c, input.value);
    input.remove();
    if (commit) changed();
    scroller.focus({ preventScroll: true });
  };
  const openEditor = (initial) => {
    if (!sel.b || !editableCell(sel.b.i, sel.b.p)) return;
    closeEditor(true);
    const { i, p } = sel.b;
    const r = view[i], c = vis[p];
    const v = wsGridValue(result, r, c);
    if (v !== null && v !== WS_DEFAULT && (v.includes('\n') || v.length > 2000)) { viewValue(); return; }   // multi-line: the value editor
    const input = el('input', { class: 'gr-ed', spellcheck: 'false', 'aria-label': `Edit ${cols[c].name}`,
      style: `top:${head.offsetHeight + i * WS_GRID_ROW}px;left:${lefts[p]}px;width:${Math.max(g.widths[c], 120)}px` });
    input.value = initial !== undefined ? initial : v === null || v === WS_DEFAULT ? '' : v;
    input.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); closeEditor(true); select(i + 1 < view.length ? i + 1 : i, p); } else if (e.key === 'Escape') { e.preventDefault(); closeEditor(false); } else if (e.key === 'Tab') { e.preventDefault(); closeEditor(true); select(i, p + (e.shiftKey ? -1 : 1)); }
    });
    input.addEventListener('blur', () => { if (editor && editor.input === input) closeEditor(true); });
    inner.appendChild(input);
    editor = { input, r, c };
    input.focus();
    if (initial === undefined) input.select();
  };
  const setSelected = (v) => {
    const rg = range();
    if (!rg || !canEdit()) return;
    for (let i = rg.i0; i <= rg.i1; i++) {
      if (!editableCell(i, 0)) continue;
      for (let p = rg.p0; p <= rg.p1; p++) wsGridSet(result, view[i], vis[p], v);
    }
    changed();
  };
  const selRows = () => { const rg = range(); return rg ? view.slice(rg.i0, rg.i1 + 1) : []; };
  const addRow = (values) => {
    g.ins.push(values || cols.map(() => undefined));
    rebuild();
    select(view.length - 1, 0);
  };
  const duplicateRows = () => {
    const ids = selRows();
    if (!ids.length) { toast('Select a row first.', 'warn'); return; }
    for (const r of ids) g.ins.push(cols.map((_, c) => (keyCols.has(c) ? undefined : wsGridValue(result, r, c))));
    rebuild();
    select(view.length - 1, 0);
  };
  const deleteRows = () => {
    const ids = selRows();
    if (!ids.length) { toast('Select rows first.', 'warn'); return; }
    const added = ids.filter((r) => r < 0).map((r) => -r - 1).sort((a, b) => b - a);
    const fetched = ids.filter((r) => r >= 0);
    const undo = fetched.length && fetched.every((r) => g.del.has(r));   // all marked already: unmark
    for (const r of fetched) if (undo) g.del.delete(r); else g.del.add(r);
    for (const k of added) g.ins.splice(k, 1);
    if (added.length) rebuild(); else changed();
  };
  const revertSelected = () => {
    for (const r of selRows()) { if (r >= 0) { g.upd.delete(r); g.del.delete(r); } }
    changed();
  };

  const submit = async (preview) => {
    const changes = wsGridChanges(result);
    if (!changes.length) return;
    closeEditor(true);
    const tab = opts.tab;
    const payload = { schema: ed.schema, table: ed.table, changes };
    try {
      await wsSession(tab);
      const res = preview ? await api('POST', `/api/console/${enc(tab.session)}/apply-edits?preview=1`, payload)
        : await api('POST', `/api/console/${enc(tab.session)}/apply-edits`, payload);
      tab.tx = res.tx_status;
      if (preview) { previewModal(res.statements); return; }
      if (res.error) {
        const e = res.error;
        modal({ title: e.conflict ? 'Changes not saved: row changed' : 'Changes not saved', iconName: 'alert', tone: 'danger', wide: true,
          body: [el('p', { text: e.message + (e.sqlstate ? `  [${e.sqlstate}]` : '') }), e.detail ? el('p', { class: 'subtle', text: 'Detail: ' + e.detail }) : null,
            e.hint ? el('p', { class: 'subtle', text: 'Hint: ' + e.hint }) : null,
            el('pre', { class: 'wx-ddl gr-sqlprev' }, wsHighlight(res.statements[e.index] + ';'))] });
        drawBar();
        return;
      }
      wsGridRevert(result);
      toast(`${fmt.plural(res.applied, 'change')} saved${tab.mode === 'manual' || tab.tx === 'in_transaction' ? '; commit the transaction to keep them' : ''}.`, 'ok');
      if (opts.onSaved) opts.onSaved(); else wsRender();
    } catch (e) {
      if (e.code === 'session_lost') { tab.session = null; tab.tx = 'idle'; }
      toastError(e, 'Changes not saved');
    }
  };
  const previewModal = (statements) => {
    const go = el('button', { class: 'btn primary', type: 'button' }, icon('check'), `Submit ${fmt.plural(statements.length, 'statement')}`);
    const copy = el('button', { class: 'btn', type: 'button', onclick: () => wsGridCopy(statements.map((s) => s + ';').join('\n')) }, icon('copy'), 'Copy');
    const m = modal({ title: 'SQL to be run', sub: `${ed.schema}.${ed.table} · ${opts.tab.mode === 'manual' ? 'inside the open transaction' : 'in one transaction'}`, wide: true,
      body: el('pre', { class: 'wx-ddl gr-sqlprev' }, wsHighlight(statements.map((s) => s + ';').join('\n'))), actions: [copy, go] });
    go.onclick = () => { m.close(); submit(false); };
  };

  /* ---------- value viewer ---------- */
  const viewValue = () => {
    if (!sel.b) return;
    const { i, p } = sel.b;
    const r = view[i], c = vis[p];
    const v = wsGridValue(result, r, c);
    let text = v === null || v === WS_DEFAULT ? '' : v;
    let pretty = false;
    if (/^json/.test(cols[c].type || '') || (text && /^\s*[[{]/.test(text))) {
      try { text = JSON.stringify(JSON.parse(text), null, 2); pretty = text !== v; } catch (e) { /* not JSON: as is */ }
    }
    const can = editableCell(i, p);
    const area = el('textarea', { class: 'gr-view', spellcheck: 'false', readonly: !can, 'aria-label': 'Value' });
    area.value = text;
    const copy = el('button', { class: 'btn', type: 'button', onclick: () => wsGridCopy(area.value) }, icon('copy'), 'Copy');
    const actions = [copy];
    let m = null;
    if (can) {
      actions.push(el('button', { class: 'btn', type: 'button', onclick: () => { wsGridSet(result, r, c, null); m.close(); changed(); } }, 'Set NULL'));
      actions.push(el('button', { class: 'btn primary', type: 'button', onclick: () => {
        let out = area.value;
        if (pretty && area.value === text) out = v;   // unchanged pretty JSON keeps its original text
        wsGridSet(result, r, c, out);
        m.close();
        changed();
      } }, icon('check'), 'Set value'));
    }
    m = modal({ title: cols[c].name, sub: `${cols[c].type} · ${r < 0 ? 'new row' : 'row ' + (r + first)}${v === null ? ' · NULL' : ''}${pretty ? ' · JSON shown formatted' : ''}${can ? '' : ' · read-only'}`,
      wide: true, body: area, actions, onClose: () => scroller.focus({ preventScroll: true }) });
  };

  /* ---------- menus and toolbar ---------- */
  const headMenu = (c) => {
    const items = [
      { icon: 'chevron', label: 'Sort ascending', action: () => { if (opts.onSort) sortBy(c, false); else { g.sortCol = c; g.desc = false; rebuild(); } } },
      { icon: 'chevron', label: 'Sort descending', action: () => { if (opts.onSort) { sortBy(c, false); } else { g.sortCol = c; g.desc = true; rebuild(); } } },
      { icon: 'search', label: g.showFilter ? 'Hide filter row' : 'Show filter row', action: () => toggleFilter() },
      '-',
      { icon: 'x', label: 'Hide column', disabled: vis.length < 2, action: () => { g.hidden.add(c); rebuild(); } },
    ];
    if (g.hidden.size) items.push({ icon: 'check', label: 'Show all columns', action: () => { g.hidden.clear(); rebuild(); } });
    items.push({ icon: 'copy', label: 'Copy column name', action: () => wsGridCopy(cols[c].name) });
    return items;
  };
  const cellMenu = () => {
    const items = [
      { icon: 'copy', label: 'Copy (Ctrl+C)', action: () => copySel('tsv') },
      { icon: 'copy', label: 'Copy as CSV', action: () => copySel('csv') },
      { icon: 'copy', label: 'Copy as JSON', action: () => copySel('json') },
      { icon: 'copy', label: 'Copy as INSERT', action: () => copySel('insert') },
      { icon: 'copy', label: 'Copy as Markdown', action: () => copySel('markdown') },
      { icon: 'search', label: 'View value…', action: () => viewValue() },
      { icon: 'search', label: 'Filter by this value', action: () => {
        const c = vis[sel.b.p], v = wsGridValue(result, view[sel.b.i], c);
        g.showFilter = true; g.filters[c] = v === null ? 'NULL' : v === WS_DEFAULT ? '' : v; rebuild();
      } },
    ];
    if (canEdit()) {
      items.push('-',
        { icon: 'edit', label: 'Edit (Enter)', action: () => openEditor() },
        { icon: 'x', label: 'Set NULL', action: () => setSelected(null) },
        { icon: 'refresh', label: 'Set DEFAULT', action: () => setSelected(WS_DEFAULT) },
        { icon: 'copy', label: 'Duplicate row', action: () => duplicateRows() },
        { icon: 'trash', label: 'Delete row (Delete)', danger: true, action: () => deleteRows() },
        { icon: 'restore', label: 'Revert selected rows', action: () => revertSelected() });
    }
    return items;
  };
  const toggleFilter = () => {
    g.showFilter = !g.showFilter;
    if (!g.showFilter) g.filters = {};
    rebuild();
    if (g.showFilter) { const f = head.querySelector('.gr-frow input'); if (f) f.focus(); }
  };
  const drawBar = () => {
    const pending = wsGridPending(result);
    const fetched = view.filter((r) => r >= 0).length;
    const copyBtn = el('button', { class: 'btn sm ghost', type: 'button', title: 'Copy the selected cells' }, icon('copy'), 'Copy');
    copyBtn.onclick = () => menu(copyBtn, Object.entries(WS_GRID_FORMATS).map(([k, [label]]) => ({ icon: 'copy', label: `Copy selection as ${label}`, action: () => copySel(k) })));
    const expBtn = el('button', { class: 'btn sm ghost', type: 'button', title: 'Save the rows shown (filter, sort and hidden columns apply) to a file' }, icon('backup'), 'Export');
    expBtn.onclick = () => menu(expBtn, Object.entries(WS_GRID_FORMATS).map(([k, [label]]) => ({ icon: 'file', label: `${label} file`, action: () => exportAll(k) })));
    const left = [
      el('button', { class: 'btn sm ghost' + (g.showFilter ? ' on' : ''), type: 'button', title: 'Filter rows by column values (on the rows loaded here)', onclick: () => toggleFilter() }, icon('search'), g.showFilter ? 'Hide filter' : 'Filter'),
      g.hidden.size ? el('button', { class: 'btn sm ghost', type: 'button', title: 'Show the hidden columns', onclick: () => { g.hidden.clear(); rebuild(); } }, `${g.hidden.size} hidden`) : null,
      copyBtn, expBtn,
      fetched !== rows.length ? el('span', { class: 'cnt', text: `${fmt.num(fetched)} of ${fmt.num(rows.length)} rows shown` }) : null,
    ];
    let right = [];
    if (canEdit()) {
      right = [
        el('button', { class: 'btn sm ghost', type: 'button', title: 'Add a row', onclick: () => addRow() }, icon('plus'), 'Row'),
        el('button', { class: 'btn sm ghost', type: 'button', title: 'Delete the selected rows (Delete); again to undo', onclick: () => deleteRows() }, icon('trash'), 'Delete'),
        pending ? el('span', { class: 'pend', text: `${fmt.plural(pending, 'change')} pending` }) : null,
        el('button', { class: 'btn sm', type: 'button', disabled: !pending, onclick: () => { wsGridRevert(result); rebuild(); } }, icon('restore'), 'Revert'),
        el('button', { class: 'btn sm', type: 'button', disabled: !pending, onclick: () => submit(true) }, icon('code'), 'Preview SQL'),
        el('button', { class: 'btn sm primary', type: 'button', disabled: !pending, onclick: () => submit(false) }, icon('check'), 'Submit'),
      ];
    } else if (result.readonly) {
      right = [el('span', { class: 'ro', title: result.readonly }, icon('lock'), 'Read-only: ' + result.readonly)];
    }
    clear(bar, left, el('div', { class: 'grow' }), right);
  };

  /* ---------- events ---------- */
  scroller.addEventListener('scroll', () => requestAnimationFrame(() => draw()));
  body.addEventListener('mousedown', (e) => {
    const cell = e.target.closest('.gr-c[data-p]');
    const row = e.target.closest('.gr-row');
    if (!cell || !row) return;
    const i = Number(row.dataset.i), p = Number(cell.dataset.p);
    if (e.button === 2) {   // right-click inside the selection keeps it
      const rg = range();
      if (rg && i >= rg.i0 && i <= rg.i1 && p >= rg.p0 && p <= rg.p1) return;
    }
    scroller.focus({ preventScroll: true });
    const now = Date.now();
    const dbl = e.button === 0 && !e.shiftKey && lastDown.i === i && lastDown.p === p && now - lastDown.at < 450;
    lastDown = { i, p, at: now };
    select(i, p, e.shiftKey);
    if (dbl) { lastDown.at = 0; if (editableCell(i, p)) openEditor(); else viewValue(); }
    if (e.button === 0 && !e.shiftKey) {   // drag to select a range
      const move = (ev) => {
        const t = document.elementFromPoint(ev.clientX, ev.clientY);
        const c2 = t && t.closest && t.closest('.gr-c[data-p]'), r2 = t && t.closest && t.closest('.gr-row');
        if (c2 && r2 && body.contains(r2)) select(Number(r2.dataset.i), Number(c2.dataset.p), true);
      };
      const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    }
  });
  body.addEventListener('contextmenu', (e) => {
    const cell = e.target.closest('.gr-c[data-p]');
    if (!cell || !sel.b) return;
    e.preventDefault();
    menu(cell, cellMenu());
  });
  scroller.addEventListener('keydown', (e) => {
    if (editor || e.target !== scroller) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'a') { e.preventDefault(); if (view.length) { sel.a = { i: 0, p: 0 }; select(view.length - 1, vis.length - 1, true); } return; }
    if (!sel.b) { if (/^Arrow/.test(e.key) && view.length) { e.preventDefault(); select(0, 0); } return; }
    const page = Math.max(1, Math.floor(scroller.clientHeight / WS_GRID_ROW) - 2);
    const mv = { ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], PageDown: [page, 0], PageUp: [-page, 0] }[e.key];
    if (mv) { e.preventDefault(); select(sel.b.i + mv[0], sel.b.p + mv[1], e.shiftKey); return; }
    if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); select(mod ? (e.key === 'Home' ? 0 : view.length - 1) : sel.b.i, e.key === 'Home' ? 0 : vis.length - 1, e.shiftKey); return; }
    if (mod && e.key.toLowerCase() === 'c') { e.preventDefault(); copySel('tsv'); return; }
    if (e.key === 'Enter' || e.key === 'F2') { e.preventDefault(); if (canEdit()) openEditor(); else viewValue(); return; }
    if (e.key === 'Delete' && canEdit()) { e.preventDefault(); deleteRows(); return; }
    if (e.key === 'Escape' && sel.a && (sel.a.i !== sel.b.i || sel.a.p !== sel.b.p)) { e.preventDefault(); select(sel.b.i, sel.b.p); return; }
    if (!mod && !e.altKey && e.key.length === 1 && canEdit()) { e.preventDefault(); openEditor(e.key); }
  });

  layout();
  drawHead();
  drawBar();
  const box = el('div', { class: 'gr-box' }, bar, scroller, foot);
  requestAnimationFrame(() => draw(true));
  draw(true);
  return box;
}
