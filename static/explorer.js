/* PG Backup Studio: database explorer, table viewer and DDL view of the SQL workspace (Step 8b).
 * Uses console.js (ws, wsRender, wsNewTab, wsAddTab, wsPersist, wsSession, sqlIdent, wsHighlight) and the page's
 * helpers inside functions only. Globals start with `wx` / `WX_`. Text is always set through text nodes.
 */
'use strict';

const WX_CSS = `
.wx { flex: none; display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border); background: var(--surface); }
.wx-head { display: flex; gap: 4px; align-items: center; padding: 6px 6px 6px 10px; border-bottom: 1px solid var(--border); flex: none; }
.wx-head .t { font-size: 11px; font-weight: 700; letter-spacing: .06em; color: var(--fg-3); margin-right: 4px; }
.wx-head .input { height: 26px; font-size: 12.5px; flex: 1; min-width: 0; padding: 0 8px; }
.wx-tree { flex: 1; overflow: auto; padding: 4px 0 12px; font-size: 12.5px; outline: none; }
.wx-row { display: flex; align-items: center; gap: 5px; height: 22px; padding-right: 10px; cursor: pointer; white-space: nowrap; user-select: none; color: var(--fg); }
.wx-row:hover { background: var(--surface-2); }
.wx-row.on { background: var(--accent-soft); }
.wx-row .chev { width: 14px; height: 14px; flex: none; color: var(--fg-3); display: flex; }
.wx-row .chev .icon { width: 12px; height: 12px; transition: transform .1s; }
.wx-row .chev.open .icon { transform: rotate(90deg); }
.wx-row > .icon { width: 14px; height: 14px; flex: none; color: var(--fg-3); }
.wx-row .nm { overflow: hidden; text-overflow: ellipsis; flex: none; max-width: 100%; }
.wx-row .ds { color: var(--fg-3); overflow: hidden; text-overflow: ellipsis; font-size: 11.5px; min-width: 0; }
.wx-row.muted { color: var(--fg-3); cursor: default; font-style: italic; }
.wx-row.muted:hover { background: none; }
.wx-row .pk { color: var(--warn); font-size: 10px; font-weight: 700; }
.wx-t { color: #2563eb !important; } .wx-v { color: #0891b2 !important; } .wx-f { color: #c026d3 !important; } .wx-s { color: #ea580c !important; }
.wx-split { width: 5px; cursor: col-resize; flex: none; margin-left: -3px; position: relative; z-index: 2; }
.wx-split:hover { background: var(--accent-soft); }
.wt-where { flex: 1; min-width: 180px; font-family: var(--mono); font-size: 12.5px; }
.wt-name { font-weight: 650; font-size: 13px; white-space: nowrap; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.wt-err { margin: 16px; padding: 12px 14px; border: 1px solid var(--danger); border-radius: 6px; background: var(--danger-soft); font-size: 13px; }
.wt-err .m { color: var(--danger); font-weight: 600; white-space: pre-wrap; }
.wt-err pre { margin: 8px 0 0; font-family: var(--mono); font-size: 12px; color: var(--fg-2); white-space: pre-wrap; }
.wt-sql { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 520px; cursor: pointer; }
.wx-ddl { font-family: var(--mono); font-size: 12.5px; line-height: 19px; white-space: pre; overflow: auto; max-height: 62vh; min-height: 120px;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin: 0; tab-size: 4; }
.wx-ddl .k { color: var(--tk-k); font-weight: 600; } .wx-ddl .s { color: var(--tk-s); } .wx-ddl .n { color: var(--tk-n); }
.wx-ddl .q { color: var(--tk-q); } .wx-ddl .c { color: var(--tk-c); font-style: italic; }
.wp { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.wp-sum { display: flex; gap: 14px; align-items: center; padding: 6px 12px; border-bottom: 1px solid var(--border); font-size: 12.5px; flex-wrap: wrap; flex: none; }
.wp-sum b { font-weight: 650; }
.wp-list { flex: 1; overflow: auto; font-size: 12.5px; min-height: 0; }
.wp-head, .wp-row { display: grid; grid-template-columns: minmax(320px, 1fr) 150px 70px 170px 110px 120px; align-items: start; border-bottom: 1px solid var(--border); }
.wp-head { position: sticky; top: 0; z-index: 2; background: var(--surface-2); font-weight: 650; color: var(--fg-2); font-size: 11.5px; }
.wp-head > div, .wp-row > div { padding: 5px 8px; min-width: 0; }
.wp-row { cursor: pointer; }
.wp-row:hover { background: var(--surface-2); }
.wp-row.slow { background: var(--danger-soft); }
.wp-node { display: flex; gap: 4px; align-items: flex-start; }
.wp-node .chev { width: 14px; flex: none; color: var(--fg-3); display: flex; padding-top: 1px; }
.wp-node .chev .icon { width: 12px; height: 12px; transition: transform .1s; }
.wp-node .chev.open .icon { transform: rotate(90deg); }
.wp-t { font-weight: 650; }
.wp-rel { color: var(--tk-q); font-family: var(--mono); }
.wp-sub { color: var(--fg-3); font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-word; margin-top: 2px; }
.wp-num { text-align: right; font-variant-numeric: tabular-nums; }
.wp-bar { height: 4px; border-radius: 2px; background: var(--accent); margin-top: 4px; min-width: 1px; }
.wp-row.slow .wp-bar { background: var(--danger); }
.wp-miss { color: var(--warn); font-weight: 650; font-size: 11px; }
.wp-det { grid-column: 1 / -1; padding: 4px 8px 10px 40px !important; font-family: var(--mono); font-size: 11.5px; color: var(--fg-2); display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; }
.wp-det span:nth-child(odd) { color: var(--fg-3); }
`;
(function wxStyle() {
  const s = document.createElement('style');
  s.textContent = WX_CSS;
  document.head.appendChild(s);
})();

const WX_ICONS = {
  wx_table: ['M3 5h18v14H3z', 'M3 10h18', 'M3 15h18', 'M9 5v14'],
  wx_view: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
  wx_fn: ['M9 20c2 0 2.5-2 3-5l1.5-8c.5-2.5 1-4 3.5-4', 'M7 10h9'],
  wx_seq: ['M4 7h4', 'M4 12h8', 'M4 17h12', 'M18 5v6', 'M16 9l2 2 2-2'],
  wx_type: ['M4 7V4h16v3', 'M9 20h6', 'M12 4v16'],
  wx_col: ['M4 4h16v16H4z', 'M10 4v16'],
  wx_key: ['M15 7a4 4 0 1 1-3.9 5H7v3H4v-3H3v-3h8.1A4 4 0 0 1 15 7z'],
  wx_schema: ['M3 7l9-4 9 4-9 4-9-4z', 'M3 12l9 4 9-4', 'M3 17l9 4 9-4'],
  wx_side: ['M3 4h18v16H3z', 'M9 4v16'],
  wx_filter: ['M3 5h18l-7 8v6l-4 2v-8z'],
};
function wxIcon(name, cls) {
  if (!ICONS.wx_table) Object.assign(ICONS, WX_ICONS);
  const svg = icon(name);
  if (cls) svg.classList.add(cls);
  return svg;
}

const WX_GROUPS = [
  ['tables', 'Tables', 'wx_table', 'wx-t'], ['views', 'Views', 'wx_view', 'wx-v'], ['matviews', 'Materialized views', 'wx_view', 'wx-v'],
  ['foreign', 'Foreign tables', 'wx_table', 'wx-t'], ['sequences', 'Sequences', 'wx_seq', 'wx-s'], ['functions', 'Functions', 'wx_fn', 'wx-f'],
  ['procedures', 'Procedures', 'wx_fn', 'wx-f'], ['aggregates', 'Aggregates', 'wx_fn', 'wx-f'], ['types', 'Types', 'wx_type', null],
];
const WX_REL_GROUPS = new Set(['tables', 'views', 'matviews', 'foreign']);
const WX_KIND = { tables: 'table', views: 'view', matviews: 'matview', foreign: 'foreign', sequences: 'sequence', functions: 'function',
  procedures: 'procedure', aggregates: 'aggregate', types: 'type' };
const WX_SHOW = 300;   // tree rows drawn per group; the filter narrows the rest
const WX_PAGES = [100, 500, 1000, 5000];

function wxQname(schema, name) { return (schema === 'public' ? '' : sqlIdent(schema) + '.') + sqlIdent(name); }

function wxTreeState(set) {
  if (!set.tree) set.tree = { schemas: null, loading: null, error: null, open: new Set(), objs: {}, detail: {}, filter: '', system: false, sel: null, scroll: 0 };
  return set.tree;
}

/* ======================================================================== panel */

/** The explorer panel plus its resize handle, or nothing when hidden. */
function wxPanel(set) {
  if (store('ws.wx') === '0') return [];
  const tr = wxTreeState(set);
  if (!tr.schemas && !tr.loading && !tr.error) wxLoadSchemas(set);
  const filter = el('input', { class: 'input', type: 'search', placeholder: 'Filter', 'aria-label': 'Filter objects', value: tr.filter,
    title: 'Show objects whose name contains this text (in the schemas loaded so far)' });
  filter.addEventListener('input', () => { tr.filter = filter.value; wxDraw(set); });
  const more = el('button', { class: 'btn sm icon-only ghost', type: 'button', title: 'More', 'aria-label': 'Explorer options' }, icon('more'));
  more.onclick = () => menu(more, [
    { icon: 'refresh', label: 'Refresh', action: () => wxRefresh(set) },
    { icon: tr.system ? 'check' : 'wx_schema', label: tr.system ? 'Hide system schemas' : 'Show system schemas', action: () => { tr.system = !tr.system; wxDraw(set); } },
    { icon: 'x', label: 'Collapse all', action: () => { tr.open.clear(); wxDraw(set); } },
    '-',
    { icon: 'wx_side', label: 'Hide explorer', action: () => { store('ws.wx', '0'); wsRender(); } },
  ]);
  const tree = el('div', { class: 'wx-tree', role: 'tree', 'aria-label': 'Database objects', tabindex: '-1' });
  tree.addEventListener('scroll', () => { tr.scroll = tree.scrollTop; });
  const width = Math.max(180, Math.min(600, Number(store('ws.wxw')) || 270));
  const panel = el('div', { class: 'wx', style: `width:${width}px` },
    el('div', { class: 'wx-head' }, el('span', { class: 't', text: 'EXPLORER' }), filter,
      el('button', { class: 'btn sm icon-only ghost', type: 'button', title: 'Refresh', 'aria-label': 'Refresh explorer', onclick: () => wxRefresh(set) }, icon('refresh')), more),
    tree);
  const split = el('div', { class: 'wx-split', title: 'Drag to resize', onmousedown: (e) => wxDragWidth(e, panel) });
  wxDraw(set, tree);
  requestAnimationFrame(() => { tree.scrollTop = tr.scroll; });
  return [panel, split];
}

function wxDragWidth(e, panel) {
  e.preventDefault();
  const startX = e.clientX, startW = panel.offsetWidth;
  const move = (ev) => { panel.style.width = Math.max(180, Math.min(600, startW + ev.clientX - startX)) + 'px'; };
  const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); store('ws.wxw', String(panel.offsetWidth)); };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

function wxToggleExplorer() { store('ws.wx', store('ws.wx') === '0' ? '1' : '0'); wsRender(); }

/* ======================================================================== loading */

async function wxLoadSchemas(set) {
  const tr = wxTreeState(set);
  tr.error = null;
  tr.loading = api('GET', `/api/servers/${enc(set.sid)}/databases/${enc(set.db)}/explorer`);
  try {
    const res = await tr.loading;
    tr.schemas = res.schemas;
    const user = res.schemas.filter((s) => !s.system);
    if (!tr.opened) {   // first load: open public (or the only schema)
      tr.opened = true;
      const pick = user.find((s) => s.name === 'public') || (user.length === 1 ? user[0] : null);
      if (pick) { tr.open.add('s:' + pick.name); wxLoadObjects(set, pick.name); }
    }
    for (const k of tr.open) if (k.startsWith('s:') && !tr.objs[k.slice(2)]) wxLoadObjects(set, k.slice(2));
  } catch (e) {
    tr.error = e.message;
  } finally {
    tr.loading = null;
  }
  wxDraw(set);
}

async function wxLoadObjects(set, schema) {
  const tr = wxTreeState(set);
  if (tr.objs[schema]) return;
  tr.objs[schema] = { loading: true };
  try {
    tr.objs[schema] = await api('GET', `/api/servers/${enc(set.sid)}/databases/${enc(set.db)}/explorer?schema=${enc(schema)}`);
  } catch (e) {
    tr.objs[schema] = { error: e.message };
  }
  wxDraw(set);
}

async function wxLoadDetail(set, schema, name) {
  const tr = wxTreeState(set);
  const k = schema + '\u0000' + name;
  if (tr.detail[k]) return;
  tr.detail[k] = { loading: true };
  try {
    tr.detail[k] = await api('GET', `/api/servers/${enc(set.sid)}/databases/${enc(set.db)}/object?schema=${enc(schema)}&name=${enc(name)}`);
  } catch (e) {
    tr.detail[k] = { error: e.message };
  }
  wxDraw(set);
}

/** Reload everything that was loaded, keeping what is open. `quiet`: after a DDL statement. */
function wxRefresh(set, quiet) {
  const tr = wxTreeState(set);
  tr.schemas = null;
  tr.objs = {};
  tr.detail = {};
  delete ws.meta[wsKey(set.sid, set.db)];
  wsLoadMeta(set.sid, set.db);
  wxLoadSchemas(set);
  wxDraw(set);
  if (!quiet) toast('Explorer refreshed.', 'ok');
}

/* ======================================================================== tree */

function wxDraw(set, tree) {
  tree = tree || (ws.view && ws.set === set && ws.view.querySelector('.wx-tree'));
  if (!tree) return;
  const tr = wxTreeState(set);
  const f = tr.filter.trim().toLowerCase();
  const rows = [];
  const row = (depth, opts) => {
    const r = el('div', { class: 'wx-row' + (opts.muted ? ' muted' : '') + (opts.key && tr.sel === opts.key ? ' on' : ''), role: opts.muted ? null : 'treeitem',
      'aria-expanded': opts.toggle ? String(!!opts.open) : null, title: opts.title || null, style: `padding-left:${6 + depth * 14}px` },
    el('span', { class: 'chev' + (opts.open ? ' open' : '') }, opts.toggle ? icon('chevron') : null),
    opts.icon ? wxIcon(opts.icon, opts.iconCls) : null,
    el('span', { class: 'nm', text: opts.label }),
    opts.pk ? el('span', { class: 'pk', text: 'PK' }) : null,
    opts.desc ? el('span', { class: 'ds', text: opts.desc }) : null);
    if (!opts.muted) {
      // every click redraws the tree (the row element is replaced), so a native dblclick never reaches it:
      // a second click on the same row within 450 ms counts as a double-click
      r.addEventListener('click', () => {
        const now = Date.now();
        const last = tr.lastClick;
        tr.lastClick = { key: opts.key, at: now };
        if (opts.key) tr.sel = opts.key;
        if (opts.toggle && opts.open2 && last && last.key === opts.key && now - last.at < 450) {
          tr.lastClick = null;
          opts.toggle();   // undo the first click's expand / collapse
          opts.open2();
        } else if (opts.toggle) opts.toggle();
        else { wxDraw(set); if (opts.open2) opts.open2(); }   // leaves (functions, sequences, types): one click opens
      });
      if (opts.menu) r.addEventListener('contextmenu', (e) => { e.preventDefault(); if (opts.key) { tr.sel = opts.key; wxDraw(set); } menu(r, opts.menu()); });
    }
    rows.push(r);
  };
  const toggler = (key, onOpen) => () => {
    if (tr.open.has(key)) tr.open.delete(key); else { tr.open.add(key); if (onOpen) onOpen(); }
    wxDraw(set);
  };

  if (tr.error) {
    rows.push(el('div', { class: 'wt-err', style: 'margin: 10px' }, el('div', { class: 'm', text: tr.error }),
      el('button', { class: 'btn sm', type: 'button', style: 'margin-top: 8px', onclick: () => wxLoadSchemas(set) }, 'Try again')));
  } else if (!tr.schemas) {
    row(0, { label: 'Loading…', muted: true });
  } else {
    const shown = tr.schemas.filter((s) => tr.system || !s.system);
    if (f && !shown.some((s) => tr.objs[s.name] && tr.objs[s.name].schema)) row(0, { label: 'Expand a schema to search in it', muted: true });
    for (const s of shown) {
      const sk = 's:' + s.name;
      const objs = tr.objs[s.name];
      const open = tr.open.has(sk) || (f && objs && objs.schema);
      row(0, { key: sk, label: s.name, icon: 'wx_schema', desc: s.system ? 'system' : '', open, title: s.comment || `Schema ${s.name} · owner ${s.owner}`,
        toggle: toggler(sk, () => wxLoadObjects(set, s.name)), menu: () => wxSchemaMenu(set, s.name) });
      if (!open) continue;
      if (!objs || objs.loading) { row(1, { label: 'Loading…', muted: true }); continue; }
      if (objs.error) { row(1, { label: objs.error, muted: true, title: objs.error }); continue; }
      let any = false;
      for (const [g, label, ic, icCls] of WX_GROUPS) {
        const all = objs[g];
        if (!all.length) continue;
        const items = f ? all.filter((o) => o.name.toLowerCase().includes(f)) : all;
        if (f && !items.length) continue;
        any = true;
        const gk = `g:${s.name}:${g}`;
        const gopen = tr.open.has(gk) || !!f;
        row(1, { key: gk, label, icon: ic, iconCls: icCls, desc: f ? `${items.length} of ${all.length}` : String(all.length), open: gopen, toggle: toggler(gk) });
        if (!gopen) continue;
        for (const o of items.slice(0, WX_SHOW)) wxObjectRows(set, row, toggler, s.name, g, o, ic, icCls);
        if (items.length > WX_SHOW) row(2, { label: `${fmt.num(items.length - WX_SHOW)} more — type in the filter to narrow`, muted: true });
      }
      if (!any) row(1, { label: f ? 'Nothing matches' : 'No objects', muted: true });
      if (objs.truncated && objs.truncated.length) row(1, { label: `Only the first ${fmt.num(20000)} ${objs.truncated.join(', ')} are listed`, muted: true });
    }
  }
  tree.replaceChildren(...rows);
}

function wxObjectRows(set, row, toggler, schema, g, o, ic, icCls) {
  const tr = wxTreeState(set);
  const kind = WX_KIND[g];
  if (WX_REL_GROUPS.has(g)) {
    const key = `r:${schema}:${o.name}`;
    const open = tr.open.has(key);
    const bits = [];
    if (o.partitioned) bits.push('partitioned');
    if (o.rows !== null && o.rows !== undefined && o.rows >= 0) bits.push(`~${fmt.num(Math.round(o.rows))} rows`);
    if (o.size) bits.push(fmt.bytes(o.size));
    row(2, { key, label: o.name, icon: ic, iconCls: icCls, desc: bits.join(' · '), open, title: o.comment || `${kind} ${schema}.${o.name} · double-click to open its data`,
      toggle: toggler(key, () => wxLoadDetail(set, schema, o.name)), open2: () => wxOpenTable(schema, o.name, kind), menu: () => wxRelMenu(set, schema, o.name, kind) });
    if (open) wxDetailRows(set, row, toggler, schema, o.name);
    return;
  }
  const routine = g === 'functions' || g === 'procedures' || g === 'aggregates';
  const label = routine ? `${o.name}(${o.args})` : o.name;
  const desc = routine ? (o.result ? '→ ' + o.result : '') : g === 'types' ? o.kind : '';
  row(2, { key: `o:${schema}:${g}:${o.oid || o.name}`, label, icon: ic, iconCls: icCls, desc, title: o.comment || `${kind} ${schema}.${label} · click to show its DDL`,
    open2: () => wxShowDdl(set, schema, kind, o.name, o.oid), menu: () => wxObjMenu(set, schema, g, o) });
}

function wxDetailRows(set, row, toggler, schema, name) {
  const tr = wxTreeState(set);
  const d = tr.detail[schema + '\u0000' + name];
  if (!d || d.loading) { row(3, { label: 'Loading…', muted: true }); return; }
  if (d.error) { row(3, { label: d.error, muted: true, title: d.error }); return; }
  const sub = (id, label, list, draw) => {
    if (!list.length) return;
    const key = `d:${schema}:${name}:${id}`;
    const open = tr.open.has(key) || (id === 'cols' && !tr.open.has(key + ':closed'));
    row(3, { key, label, desc: String(list.length), icon: id === 'cols' ? 'wx_col' : id === 'keys' ? 'wx_key' : id === 'idx' ? 'zap' : 'activity', open,
      toggle: () => {
        if (id === 'cols') { if (open) tr.open.add(key + ':closed'); else tr.open.delete(key + ':closed'); tr.open.delete(key); } else if (open) tr.open.delete(key); else tr.open.add(key);
        wxDraw(set);
      } });
    if (open) list.forEach(draw);
  };
  sub('cols', 'Columns', d.columns, (c) => row(4, { label: c.name, pk: c.pk, desc: c.type + (c.nullable ? '' : ' · not null'),
    title: [c.default !== null ? `default ${c.default}` : '', c.comment || ''].filter(Boolean).join('\n') || null,
    menu: () => [{ icon: 'copy', label: 'Copy column name', action: () => wsGridCopy(sqlIdent(c.name)) }] }));
  sub('keys', 'Keys and constraints', d.constraints, (k) => row(4, { label: k.name, desc: k.kind, title: k.def,
    menu: () => [{ icon: 'copy', label: 'Copy definition', action: () => wsGridCopy(k.def) }] }));
  sub('idx', 'Indexes', d.indexes, (i) => row(4, { label: i.name, desc: (i.primary ? 'primary' : i.unique ? 'unique' : '') + (i.size !== null ? ` · ${fmt.bytes(i.size)}` : ''), title: i.def,
    menu: () => [{ icon: 'copy', label: 'Copy definition', action: () => wsGridCopy(i.def + ';') }] }));
  sub('trg', 'Triggers', d.triggers, (t) => row(4, { label: t.name, desc: t.enabled ? '' : 'disabled', title: t.def,
    menu: () => [{ icon: 'copy', label: 'Copy definition', action: () => wsGridCopy(t.def + ';') }] }));
}

/* ======================================================================== context menus */

function wxSchemaMenu(set, schema) {
  return [
    { icon: 'code', label: 'New query here', action: () => wsAddTab(`SET search_path TO ${sqlIdent(schema)}, public;\n\n`) },
    { icon: 'copy', label: 'Copy name', action: () => wsGridCopy(sqlIdent(schema)) },
    { icon: 'refresh', label: 'Refresh', action: () => { delete wxTreeState(set).objs[schema]; wxLoadObjects(set, schema); wxDraw(set); } },
  ];
}

function wxRelMenu(set, schema, name, kind) {
  const q = wxQname(schema, name);
  return [
    { icon: 'wx_table', label: 'Open data', action: () => wxOpenTable(schema, name, kind) },
    { icon: 'code', label: 'New query', action: () => wsAddTab(`SELECT *\nFROM ${q}\nLIMIT 100;`, name) },
    { icon: 'file', label: 'Show DDL', action: () => wxShowDdl(set, schema, kind, name) },
    '-',
    { icon: 'copy', label: 'Copy name', action: () => wsGridCopy(sqlIdent(name)) },
    { icon: 'copy', label: 'Copy qualified name', action: () => wsGridCopy(sqlIdent(schema) + '.' + sqlIdent(name)) },
    { icon: 'refresh', label: 'Refresh', action: () => { delete wxTreeState(set).detail[schema + '\u0000' + name]; wxLoadDetail(set, schema, name); wxDraw(set); } },
  ];
}

function wxObjMenu(set, schema, g, o) {
  const kind = WX_KIND[g];
  const q = wxQname(schema, o.name);
  const items = [{ icon: 'file', label: 'Show DDL', action: () => wxShowDdl(set, schema, kind, o.name, o.oid) }];
  if (g === 'sequences') items.push({ icon: 'code', label: 'New query', action: () => wsAddTab(`SELECT last_value, is_called\nFROM ${q};`, o.name) });
  if (g === 'functions') items.push({ icon: 'code', label: 'New query', action: () => wsAddTab(`SELECT ${q}(${o.args ? `/* ${o.args} */` : ''});`, o.name) });
  if (g === 'procedures') items.push({ icon: 'code', label: 'New query', action: () => wsAddTab(`CALL ${q}();`, o.name) });
  items.push('-', { icon: 'copy', label: 'Copy name', action: () => wsGridCopy(sqlIdent(o.name)) },
    { icon: 'copy', label: 'Copy qualified name', action: () => wsGridCopy(sqlIdent(schema) + '.' + sqlIdent(o.name)) });
  return items;
}

/* ======================================================================== DDL */

async function wxShowDdl(set, schema, kind, name, oid) {
  const pre = el('pre', { class: 'wx-ddl' }, el('span', { class: 'spin' }), ' Loading…');
  const copy = el('button', { class: 'btn', type: 'button', disabled: true }, icon('copy'), 'Copy');
  const open = el('button', { class: 'btn primary', type: 'button', disabled: true }, icon('code'), 'Open in a query tab');
  const m = modal({ title: 'DDL', sub: `${kind} ${schema}.${name}`, wide: true, body: pre, actions: [copy, open] });
  try {
    const q = oid ? `oid=${enc(String(oid))}` : `name=${enc(name)}`;
    const res = await api('GET', `/api/servers/${enc(set.sid)}/databases/${enc(set.db)}/ddl?schema=${enc(schema)}&kind=${enc(kind)}&${q}`);
    pre.replaceChildren(wsHighlight(res.ddl));
    copy.disabled = open.disabled = false;
    copy.onclick = () => wsGridCopy(res.ddl);
    open.onclick = () => { m.close(); wsAddTab(res.ddl, `${name} DDL`); };
  } catch (e) {
    pre.replaceChildren(document.createTextNode(e.message));
  }
}

/* ======================================================================== table viewer */

function wxOpenTable(schema, name, kind) {
  const set = ws.set;
  let tab = set.tabs.find((t) => t.kind === 'table' && t.schema === schema && t.table === name);
  if (!tab) {
    tab = wsNewTab(set.sid, set.db, name, '', { kind: 'table', schema, table: name, relkind: kind || 'table', where: '', order: [], limit: 100 });
    set.tabs.push(tab);
  }
  set.active = set.tabs.indexOf(tab);
  wsPersist(set);
  wsRender();
}

async function wxLoad(tab, count, retried) {
  if (tab.running) return;
  tab.running = true;
  tab.loadFailed = false;
  wsRender();
  try {
    await wsSession(tab);
    const res = await api('POST', `/api/console/${enc(tab.session)}/table-data`, { schema: tab.schema, table: tab.table, where: tab.where,
      order_by: tab.order, limit: tab.limit, offset: tab.offset || 0, count: !!count });
    tab.tx = res.tx_status;
    tab.data = res.result;
    tab.hasMore = res.has_more;
    tab.pageSql = res.sql;
    tab.loadedAt = new Date();
    if (res.count !== null) tab.count = res.count;
  } catch (e) {
    if (e.code === 'session_lost' && !retried) { tab.session = null; tab.running = false; return wxLoad(tab, count, true); }
    tab.loadFailed = true;
    toastError(e, 'Data not loaded');
  } finally {
    tab.running = false;
  }
  wsRender();
}

/** Load other rows after `change()` (paging, filter, sort, page size), asking first when grid edits are pending. */
async function wxGo(tab, change, count) {
  if (tab.running || !(await wsKeepEdits(tab, 'Loading other rows'))) return;
  if (change) change();
  wsPersistSoon(ws.set);
  wxLoad(tab, count);
}

/** Reload from the first page after the filter, sort or page size changed. */
function wxReload(tab, change) {
  wxGo(tab, () => { if (change) change(); tab.offset = 0; tab.count = null; });
}

function wxSort(tab, column, add) {
  const order = tab.order.slice();
  const at = order.findIndex((o) => o.column === column);
  const cur = at >= 0 ? order[at] : null;
  const next = !cur ? { column, desc: false } : !cur.desc ? { column, desc: true } : null;   // none → asc → desc → none
  if (add) {
    if (cur) order.splice(at, 1, ...(next ? [next] : [])); else order.push(next);
    tab.order = order;
  } else tab.order = next ? [next] : [];
  tab.offset = 0;
  wsPersistSoon(ws.set);
  wxLoad(tab);
}

/** Toolbar and body of a table viewer tab (returned as the center column's children). */
function wxTableView(tab, dbSel) {
  if (!tab.data && !tab.running && !tab.loadFailed) setTimeout(() => wxLoad(tab), 0);
  const where = el('input', { class: 'input wt-where', value: tab.where, placeholder: 'WHERE …   e.g. id > 100 AND name ILIKE \'%a%\'', 'aria-label': 'Filter (WHERE condition)',
    spellcheck: 'false', title: 'A condition for WHERE, as typed. Enter applies it.' });
  const apply = () => { const w = where.value.trim(); if (w !== tab.where) wxReload(tab, () => { tab.where = w; }); else wxGo(tab); };
  where.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); apply(); } });
  const size = el('select', { class: 'input', 'aria-label': 'Rows per page', title: 'Rows per page',
    onchange: (e) => { const n = Number(e.target.value); wxReload(tab, () => { tab.limit = n; }); } },
  WX_PAGES.map((n) => el('option', { value: String(n), selected: n === tab.limit }, `${fmt.num(n)} / page`)));
  const off = tab.offset || 0;
  const n = tab.data && !tab.data.error ? tab.data.row_count : 0;
  const range = n ? `${fmt.num(off + 1)}–${fmt.num(off + n)}` : off ? `after ${fmt.num(off)}` : '0';
  const pager = [
    el('button', { class: 'btn sm icon-only', type: 'button', title: 'Previous page', 'aria-label': 'Previous page', disabled: tab.running || !off,
      onclick: () => wxGo(tab, () => { tab.offset = Math.max(0, off - tab.limit); }) }, el('span', { style: 'display:flex;transform:rotate(180deg)' }, icon('chevron'))),
    el('span', { class: 'subtle', style: 'white-space: nowrap; font-size: 12.5px', text: `${range}${tab.count !== null && tab.count !== undefined ? ' of ' + fmt.num(tab.count) : ''}` }),
    el('button', { class: 'btn sm icon-only', type: 'button', title: 'Next page', 'aria-label': 'Next page', disabled: tab.running || !tab.hasMore,
      onclick: () => wxGo(tab, () => { tab.offset = off + tab.limit; }) }, icon('chevron')),
  ];
  const more = el('button', { class: 'btn sm icon-only ghost', type: 'button', title: 'More', 'aria-label': 'More' }, icon('more'));
  more.onclick = () => menu(more, [
    { icon: 'code', label: 'Open as a query', action: () => wsAddTab(wxPageQuery(tab), tab.table) },
    { icon: 'file', label: 'Show DDL', action: () => wxShowDdl(ws.set, tab.schema, tab.relkind || 'table', tab.table) },
    { icon: 'x', label: 'Clear sort and filter', disabled: !tab.order.length && !tab.where, action: () => wxReload(tab, () => { tab.order = []; tab.where = ''; }) },
  ]);
  const bar = el('div', { class: 'ws-bar' }, icon('db'), dbSel,
    el('span', { class: 'wt-name', title: `${tab.schema}.${tab.table}` }, wxIcon(tab.relkind === 'view' || tab.relkind === 'matview' ? 'wx_view' : 'wx_table', 'wx-t'), ' ', `${tab.schema}.${tab.table}`),
    where, el('button', { class: 'btn sm primary', type: 'button', disabled: tab.running, onclick: apply }, icon('play'), 'Apply'),
    el('button', { class: 'btn sm danger', type: 'button', disabled: !tab.running, title: 'Cancel', onclick: () => wsCancel(tab) }, icon('stop')),
    size, pager,
    el('button', { class: 'btn sm', type: 'button', disabled: tab.running, title: 'Count the rows that match the filter (count(*), may be slow on big tables)',
      onclick: () => wxGo(tab, null, true) }, 'Count'),
    el('button', { class: 'btn sm icon-only ghost', type: 'button', title: 'Reload', 'aria-label': 'Reload', disabled: tab.running, onclick: () => wxGo(tab) }, icon('refresh')),
    more);

  const res = el('div', { class: 'ws-res' });
  const d = tab.data;
  if (!d) {
    res.appendChild(el('div', { class: 'ws-empty' }, tab.running ? [el('span', { class: 'spin' }), ' Loading…'] : 'Not loaded. Press Reload.'));
  } else if (d.error) {
    res.appendChild(el('div', { class: 'wt-err' }, el('div', { class: 'm', text: d.error.message + (d.error.sqlstate ? `  [${d.error.sqlstate}]` : '') }),
      d.error.hint ? el('div', { text: 'Hint: ' + d.error.hint }) : null, el('pre', { text: tab.pageSql || '' })));
  } else {
    if (tab.running) res.appendChild(el('div', { class: 'ws-rtabs' }, el('span', { class: 'subtle' }, el('span', { class: 'spin', style: 'width:10px;height:10px;border-width:2px' }), ' loading…')));
    res.appendChild(wsGrid(d, { tab, sort: tab.order, onSort: (c, add) => { if (!tab.running) wxSort(tab, c, add); }, firstRow: off + 1, onSaved: () => wxLoad(tab) }));
    res.appendChild(el('div', { class: 'ws-status' },
      el('span', { text: `${fmt.plural(n, 'row')}${tab.hasMore ? ' (more on the next page)' : ''}` }),
      el('span', { text: `${d.duration_ms} ms` }),
      el('span', { class: 'wt-sql', title: (tab.pageSql || '') + '\n\nClick to copy', text: (tab.pageSql || '').replace(/\s+/g, ' '), onclick: () => wsGridCopy(tab.pageSql || '') }),
      el('span', { class: 'grow' }), el('span', { text: tab.loadedAt ? tab.loadedAt.toLocaleTimeString() : '' })));
  }
  return [bar, res];
}

/** The current page as an editable query (without the extra look-ahead row). */
function wxPageQuery(tab) {
  const q = sqlIdent(tab.schema) + '.' + sqlIdent(tab.table);
  const order = tab.order.map((o) => sqlIdent(o.column) + (o.desc ? ' DESC' : '')).join(', ');
  return `SELECT *\nFROM ${q}${tab.where ? `\nWHERE ${tab.where}` : ''}${order ? `\nORDER BY ${order}` : ''}\nLIMIT ${tab.limit}${tab.offset ? ` OFFSET ${tab.offset}` : ''};`;
}

/* ======================================================================== EXPLAIN plan view (8d) */

const WP_SUB_KEYS = ['Index Cond', 'Recheck Cond', 'Hash Cond', 'Merge Cond', 'Join Filter', 'Filter', 'Sort Key', 'Group Key'];
const WP_MISS = 10;   // an estimate off by this factor (and by more than 10 rows) is flagged

/** Prepare a plan tree: total and self time (self cost without ANALYZE), misestimates, flat list in display order. */
function wpPrepare(root, analyze) {
  const list = [];
  const walk = (n, depth) => {
    const kids = n.Plans || [];
    n._depth = depth;
    n._loops = n['Actual Loops'] || 1;
    n._never = analyze && n['Actual Loops'] === 0;
    n._total = analyze ? (n['Actual Total Time'] || 0) * n._loops : n['Total Cost'] || 0;
    list.push(n);
    kids.forEach((k) => walk(k, depth + 1));
    n._self = Math.max(0, n._total - kids.reduce((s, k) => s + k._total, 0));
    const est = n['Plan Rows'] || 0, act = n['Actual Rows'];
    if (analyze && !n._never && act !== undefined) {
      const hi = Math.max(est, act), lo = Math.max(Math.min(est, act), 1);
      if (hi / lo >= WP_MISS && hi - lo > 10) n._miss = `${Math.round(hi / lo)}× ${act > est ? 'more' : 'fewer'} rows than estimated`;
    }
  };
  walk(root, 0);
  const slow = list.reduce((a, n) => (!a || n._self > a._self ? n : a), null);
  return { list, slow, total: root._total || 1 };
}

function wpLabel(n) {
  let t = n['Node Type'];
  if (t === 'Aggregate' && n.Strategy && n.Strategy !== 'Plain') t = `${n.Strategy} Aggregate`;
  if (n['Join Type'] && n['Join Type'] !== 'Inner') t += ` (${n['Join Type']})`;
  if (n['Parent Relationship'] === 'InitPlan' || n['Parent Relationship'] === 'SubPlan') t = `${n['Subplan Name'] || n['Parent Relationship']} · ${t}`;
  return t;
}

function wpTarget(n) {
  const rel = n['Relation Name'] ? (n.Schema ? n.Schema + '.' : '') + n['Relation Name'] : n['CTE Name'] || n['Function Name'] || '';
  const alias = n.Alias && n.Alias !== n['Relation Name'] && n.Alias !== n['CTE Name'] && n.Alias !== n['Function Name'] ? ' ' + n.Alias : '';
  const idx = n['Index Name'] ? ` using ${n['Index Name']}` : '';
  return `${rel}${alias}${idx}`.trim();
}

function wpMs(v) { return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v >= 10 ? `${v.toFixed(1)} ms` : `${v.toFixed(3)} ms`; }

function wpValue(v) { return Array.isArray(v) ? v.join(', ') : v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v); }

/** The Plan result tab: summary, node tree (slowest node and bad estimates marked), all details on click. */
function wxPlanView(tab, p) {
  const box = el('div', { class: 'wp' });
  if (p.error) {
    const e = p.error;
    box.appendChild(el('div', { class: 'wt-err' }, el('div', { class: 'm', text: e.message + (e.sqlstate ? `  [${e.sqlstate}]` : '') }),
      e.detail ? el('div', { text: 'Detail: ' + e.detail }) : null, e.hint ? el('div', { text: 'Hint: ' + e.hint }) : null,
      p.analyze ? el('div', { class: 'subtle', style: 'margin-top: 6px', text: 'Nothing was kept: EXPLAIN ANALYZE runs in a transaction that is rolled back.' }) : null,
      el('pre', { text: p.statement })));
    return box;
  }
  const analyze = p.analyze;
  const prep = p._prep || (p._prep = wpPrepare(p.plan.Plan, analyze));
  if (!p._open) p._open = new Set();      // nodes with their details shown
  if (!p._closed) p._closed = new Set();  // nodes whose children are hidden
  const root = p.plan.Plan;
  const summary = el('div', { class: 'wp-sum' },
    el('span', null, el('b', { text: analyze ? 'EXPLAIN ANALYZE' : 'EXPLAIN' }), ` · ${fmt.plural(prep.list.length, 'node')}`),
    p.plan['Planning Time'] !== undefined ? el('span', { text: `planning ${wpMs(p.plan['Planning Time'])}` }) : null,
    p.plan['Execution Time'] !== undefined ? el('span', null, 'execution ', el('b', { text: wpMs(p.plan['Execution Time']) })) : null,
    el('span', { text: `total cost ${root['Total Cost']}` }),
    analyze ? el('span', { class: 'badge ok', title: 'The statement ran inside a transaction that was rolled back', text: 'changes rolled back' })
      : el('span', { class: 'subtle', text: 'estimated; the statement was not run' }),
    analyze && prep.list.length > 1 ? el('span', { class: 'subtle', text: `slowest: ${prep.slow['Node Type']} (${Math.round((prep.slow._self / prep.total) * 100)}% of the time)` }) : null,
    el('div', { class: 'grow' }),
    el('button', { class: 'btn sm ghost', type: 'button', disabled: !p._closed.size, onclick: () => { p._closed.clear(); wsRender(); } }, 'Expand all'),
    el('button', { class: 'btn sm ghost', type: 'button', onclick: () => wsGridCopy(JSON.stringify([p.plan], (k, v) => (k.startsWith('_') ? undefined : v), 2)) }, icon('copy'), 'Copy JSON'),
    el('button', { class: 'btn sm ghost', type: 'button', disabled: tab.running, title: 'Explain the same statement again', onclick: () => wxReExplain(tab, p) }, icon('refresh'), 'Again'));
  const head = el('div', { class: 'wp-head' }, el('div', { text: 'Node' }), el('div', { class: 'wp-num', text: analyze ? 'Rows est. → actual' : 'Rows (est.)' }),
    el('div', { class: 'wp-num', text: analyze ? 'Loops' : '' }), el('div', { class: 'wp-num', text: analyze ? 'Time self / total' : 'Cost self / total' }),
    el('div', { class: 'wp-num', text: 'Startup cost' }), el('div', { class: 'wp-num', text: analyze ? 'Buffers hit / read' : '' }));
  const list = el('div', { class: 'wp-list' }, head);
  const hidden = new Set();
  prep.list.forEach((n, i) => {
    if (hidden.has(n)) { (n.Plans || []).forEach((k) => hidden.add(k)); return; }
    if (p._closed.has(i)) (n.Plans || []).forEach((k) => hidden.add(k));
    const kids = (n.Plans || []).length;
    const sub = WP_SUB_KEYS.filter((k) => n[k] !== undefined).map((k) => `${k}: ${wpValue(n[k])}`)
      .concat(n['Rows Removed by Filter'] ? [`Rows removed by filter: ${fmt.num(n['Rows Removed by Filter'])}`] : []).join('\n');
    const pct = Math.min(100, (n._self / prep.total) * 100);
    const rows = analyze ? `${fmt.num(n['Plan Rows'])} → ${n._never ? 'never run' : fmt.num(n['Actual Rows'])}` : fmt.num(n['Plan Rows']);
    const time = analyze ? `${wpMs(n._self)} / ${wpMs(n._total)}` : `${Math.round(n._self * 100) / 100} / ${n['Total Cost']}`;
    const buf = n['Shared Hit Blocks'] !== undefined ? `${fmt.num(n['Shared Hit Blocks'])} / ${fmt.num(n['Shared Read Blocks'] || 0)}` : '';
    const target = wpTarget(n);
    const chev = el('span', { class: 'chev' + (kids && !p._closed.has(i) ? ' open' : ''), title: kids ? 'Show / hide the nodes under this one' : null }, kids ? icon('chevron') : null);
    chev.addEventListener('click', (e) => { e.stopPropagation(); if (!kids) return; if (p._closed.has(i)) p._closed.delete(i); else p._closed.add(i); wsRender(); });
    const row = el('div', { class: 'wp-row' + (n === prep.slow && prep.list.length > 1 ? ' slow' : ''), title: 'Click for all details' },
      el('div', { class: 'wp-node', style: `padding-left:${8 + n._depth * 18}px` }, chev,
        el('div', { style: 'min-width: 0; flex: 1' },
          el('div', null, el('span', { class: 'wp-t', text: wpLabel(n) }), target ? el('span', { class: 'wp-rel', text: ' ' + target }) : null),
          sub ? el('div', { class: 'wp-sub', text: sub }) : null,
          n._miss ? el('div', { class: 'wp-miss', text: '⚠ ' + n._miss }) : null,
          el('div', { class: 'wp-bar', style: `width:${pct}%`, title: `${Math.round(pct)}% of the ${analyze ? 'time' : 'cost'}` }))),
      el('div', { class: 'wp-num', text: rows }),
      el('div', { class: 'wp-num', text: analyze ? fmt.num(n._loops) : '' }),
      el('div', { class: 'wp-num', text: time }),
      el('div', { class: 'wp-num', text: String(n['Startup Cost']) }),
      el('div', { class: 'wp-num', text: buf }));
    row.addEventListener('click', () => { if (p._open.has(i)) p._open.delete(i); else p._open.add(i); wsRender(); });
    list.appendChild(row);
    if (p._open.has(i)) {
      const det = el('div', { class: 'wp-det' });
      for (const [k, v] of Object.entries(n)) if (k !== 'Plans' && !k.startsWith('_')) det.append(el('span', { text: k }), el('span', { text: wpValue(v) }));
      list.appendChild(el('div', { class: 'wp-row', style: 'cursor: default' }, det));
    }
  });
  if (p.plan.Triggers && p.plan.Triggers.length) {
    list.appendChild(el('div', { class: 'wp-sum' }, el('b', { text: 'Triggers' }),
      p.plan.Triggers.map((t) => el('span', { text: `${t['Trigger Name']}${t.Relation ? ' on ' + t.Relation : ''}: ${wpMs(t.Time || 0)} × ${fmt.num(t.Calls || 0)}` }))));
  }
  box.append(summary, list);
  return box;
}

async function wxReExplain(tab, p) {
  if (tab.running) return;
  tab.running = true;
  wsRender();
  try {
    await wsSession(tab);
    const res = await api('POST', `/api/console/${enc(tab.session)}/explain`, { sql: p.statement, analyze: p.analyze, timeout_s: tab.timeout });
    tab.tx = res.tx_status;
    tab.plan = Object.assign(res, { baseLine: p.baseLine, at: new Date() });
    tab.active = 'plan';
  } catch (e) {
    if (e.code === 'session_lost') { tab.session = null; tab.tx = 'idle'; }
    toastError(e, 'Not explained');
  } finally {
    tab.running = false;
  }
  wsRender();
}
