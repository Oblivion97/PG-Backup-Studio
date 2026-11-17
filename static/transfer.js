/* PG Backup Studio: the per-server Transfer tab (decision 013) — copy one table's rows into a table of another
 * database on any saved server. Pick the source table, pick (or name) the target table, compare: an exact match is a
 * direct transfer, a different table gets a column mapping, a missing one is created. The review shows the SQL that
 * will run; the copy itself is a job (Activity / History). Uses the page's helpers (el, clear, icon, api, modal, busy,
 * toast, toastError, errorCard, loadingView, stale, fmt, server, state, enc, go, pollJobs, loadDatabases, stepNum)
 * inside functions only. Text is always set through text nodes.
 */
'use strict';

/* ICONS and el are defined by the page's main script, which runs after this file. */
document.addEventListener('DOMContentLoaded', () => {
  ICONS.swap = ['M7 4L3 8l4 4', 'M3 8h14', 'M17 20l4-4-4-4', 'M21 16H7'];
  document.head.appendChild(el('style', null, `
  .job-ic.transfer { background: color-mix(in srgb, #0ea5e9 15%, transparent); color: #0ea5e9; }
  .tr-map td, .tr-map th { vertical-align: middle; }
  .tr-map select { min-width: 150px; }
  .tr-sql { margin: 0; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; word-break: break-word; background: var(--surface-2);
    border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px; max-height: 220px; overflow: auto; }
  .tr-on { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; color: var(--fg-3); margin: 10px 0 4px; }
  .tr-arrow { display: flex; align-items: center; justify-content: center; color: var(--fg-3); }
  `));
});

/* ---------------------------------------------------------------- job / history helpers used by index.html */

function kindName(k) { return { backup: 'Backup', restore: 'Restore', transfer: 'Transfer' }[k] || k; }
function kindIcon(k) { return k === 'transfer' ? 'swap' : k === 'backup' ? 'backup' : 'restore'; }
function trLabel(x) { return x ? `${x.server ? x.server + ' / ' : ''}${x.database} / ${x.table}` : ''; }

function trJobItems(j) {
  const p = j.progress || {};
  const t = j.transfer || {};
  return [
    ['From', trLabel(t.source)],
    ['To', trLabel(t.target) + (t.target && t.target.created ? ' (new)' : '')],
    ['Rows copied', `${fmt.num(p.rows)}${p.est_rows ? ` / ≈${fmt.num(p.est_rows)}` : ''}`],
    ['Existing rows', TR_MODES[j.mode] || j.mode || '—'],
    ['Elapsed', fmt.dur(j.elapsed_s)],
  ];
}

function trResult(j, res) {
  const wrap = el('div', { class: 'stack' });
  if (j.status === 'failed' && j.error) wrap.appendChild(el('div', { class: 'alert danger' }, icon('alert'), el('div', { class: 'body' }, el('b', { text: 'The transfer failed' }), j.error)));
  if (j.status === 'success') wrap.appendChild(el('div', { class: 'alert ok' }, icon('check'), el('div', { class: 'body' },
    el('b', { text: `${fmt.num(res.rows_written)} rows written to ${res.target}` }),
    [res.rows_skipped ? `${fmt.num(res.rows_skipped)} rows were skipped because they already existed.` : null,
      res.created ? 'The table was created.' : null, res.truncated ? 'Its earlier rows were deleted first.' : null,
      res.sequences && res.sequences.length ? `Sequences moved forward: ${res.sequences.join(', ')}.` : null].filter(Boolean).join(' '))));
  if (j.transfer && j.transfer.source && j.transfer.source.query) wrap.appendChild(el('div', null, el('div', { class: 'tr-on', text: 'Source query' }),
    el('pre', { class: 'tr-sql', text: j.transfer.source.query })));
  if (j.transfer && j.transfer.source && j.transfer.source.where) wrap.appendChild(el('div', { class: 'subtle' }, 'Row filter: ', el('span', { class: 'mono', text: j.transfer.source.where })));
  return wrap;
}

function trHistoryText(h) {
  const src = h.source || {};
  const from = src.query ? `${src.database || '?'} (query)` : `${src.database || '?'}.${src.table || '?'}`;
  return `Transfer · ${from} → ${h.table || '?'}${h.created ? ' (new)' : ''}`;
}

/* ---------------------------------------------------------------- the tab */

const TR_MODES = { append: 'Add to them', skip: 'Skip existing', replace: 'Delete first' };
const TR_NOTE = {
  same: ['ok', 'same type'], convert: ['', 'converted'], check: ['warn', 'check type'], default: ['', 'default / empty'],
  generated: ['', 'generated'], required: ['danger', 'required'],
};
let TR = null;
let trView = null;
const trMeta = {};   // `${sid}|${db}` -> schemas, `${sid}|${db}|${schema}` -> objects

function trFresh(sid) {
  return { sid, smode: 'table', query: '', sdb: '', sschema: '', stable: '', where: '', tsid: sid, tdb: '', tschema: '', ttable: '', cmp: null, cmpKey: '',
    map: {}, existing: 'append', confirm: '', err: {} };
}

async function viewTransfer(view, token) {
  const sid = state.route.sid;
  if (!TR || TR.sid !== sid) TR = trFresh(sid);
  trView = view;
  loadingView(view, 'Connecting…');
  try { if (!state.dbs[sid]) await loadDatabases(sid); } catch (e) { if (!stale(token)) errorCard(view, e, () => route()); return; }
  if (stale(token)) return;
  drawTransfer();
}

function trAdd(node, ...xs) { return append(node, xs); }   // skips null, unlike node.append
function trRefresh() { if (trView && trView.isConnected && state.route.view === 'transfer') drawTransfer(); }

/** Load and cache schemas (no schema) or the objects of one schema; redraws when done. */
function trLoad(sid, db, schema) {
  const key = schema === undefined ? `${sid}|${db}` : `${sid}|${db}|${schema}`;
  if (trMeta[key]) return trMeta[key];
  trMeta[key] = { loading: true };
  const url = `/api/servers/${enc(sid)}/databases/${enc(db)}/explorer` + (schema === undefined ? '' : `?schema=${encodeURIComponent(schema)}`);
  api('GET', url).then((r) => { trMeta[key] = { data: r }; }, (e) => { trMeta[key] = { error: e.message }; }).then(trRefresh);
  return trMeta[key];
}
function trTargetDbs() {
  const t = TR;
  if (state.dbs[t.tsid] || t.tdbsLoading === t.tsid) return state.dbs[t.tsid];
  t.tdbsLoading = t.tsid;
  loadDatabases(t.tsid).then(() => { t.tdbsErr = null; }, (e) => { t.tdbsErr = e.message; }).then(() => { t.tdbsLoading = null; trRefresh(); });
  return null;
}

function trKey() {
  const t = TR;
  return JSON.stringify([t.smode, t.sdb, t.smode === 'query' ? t.query.trim() : [t.sschema, t.stable], t.tsid, t.tdb, t.tschema, t.ttable]);
}
function trIsQuery() { return TR.smode === 'query'; }
function trSourceText() { const t = TR; return trIsQuery() ? (t.sdb ? `${t.sdb} · query` : '') : (t.stable ? `${t.sdb}.${t.sschema}.${t.stable}` : ''); }

/** Open the Transfer tab with this query as the source (from the SQL workspace). */
function trFromQuery(sid, db, sqlText) {
  TR = Object.assign(trFresh(sid), { smode: 'query', sdb: db, query: String(sqlText || '').trim() });
  go(`#/s/${enc(sid)}/transfer`);
}

function trSelect(label, value, options, onchange, placeholder, disabled) {
  return el('div', { class: 'field' }, el('label', null, label),
    el('select', { class: 'input', disabled: !!disabled, onchange: (e) => onchange(e.target.value) },
      el('option', { value: '' }, placeholder || 'Choose…'),
      options.map((o) => el('option', { value: o.value, selected: o.value === value }, o.label))));
}
function trStatus(meta, what) {
  if (!meta || meta.loading) return el('div', { class: 'subtle' }, el('span', { class: 'spin' }), ` Loading ${what}…`);
  if (meta.error) return el('div', { class: 'alert danger' }, icon('alert'), el('div', { class: 'body', text: meta.error }));
  return null;
}
function trSchemas(meta) { return meta && meta.data ? meta.data.schemas.filter((s) => !s.system).map((s) => s.name) : []; }

function drawTransfer() {
  const view = trView;
  const t = TR;
  const scrollY = $('content').scrollTop;
  const src = server(t.sid);
  const sdbs = state.dbs[t.sid] || [];

  // 1. source
  const srcFields = el('div', { class: 'stack' });
  const sRow = el('div', { class: 'form-grid', style: 'grid-template-columns: repeat(3, minmax(0, 1fr))' },
    trSelect('Database', t.sdb, sdbs.map((d) => ({ value: d.name, label: d.name })), (v) => { Object.assign(t, { sdb: v, sschema: '', stable: '' }); trRefresh(); }));
  trAdd(srcFields, sRow);
  let sObjs = null;
  if (trIsQuery()) {
    trAdd(srcFields, el('div', { class: 'field' }, el('label', null, 'Query'),
      el('textarea', { class: 'input mono', spellcheck: 'false', style: 'height: 170px; padding: 8px 10px; resize: vertical; line-height: 1.45',
        placeholder: 'SELECT o.id, c.name, o.total\nFROM orders o\nJOIN customers c ON c.id = o.customer_id\nWHERE o.created_at >= \'2026-01-01\'',
        oninput: (e) => { t.query = e.target.value; }, onchange: () => trRefresh() }, t.query),
      el('div', { class: 'hint', text: 'Any query that returns rows: joins, WITH, GROUP BY… Each column of the result needs its own name (use AS). It runs read-only on the source database.' })));
  } else if (t.sdb) {
    const sm = trLoad(t.sid, t.sdb);
    const names = trSchemas(sm);
    if (!t.sschema && names.length) t.sschema = names.includes('public') ? 'public' : names[0];
    trAdd(sRow, trSelect('Schema', t.sschema, names.map((n) => ({ value: n, label: n })), (v) => { Object.assign(t, { sschema: v, stable: '' }); trRefresh(); }, 'Choose…', !sm.data));
    if (t.sschema && sm.data) {
      const om = trLoad(t.sid, t.sdb, t.sschema);
      sObjs = om.data ? [...om.data.tables.map((x) => ({ ...x, k: x.partitioned ? 'partitioned' : '' })), ...om.data.views.map((x) => ({ ...x, k: 'view' })),
        ...om.data.matviews.map((x) => ({ ...x, k: 'mat. view' })), ...om.data.foreign.map((x) => ({ ...x, k: 'foreign' }))] : [];
      trAdd(sRow, trSelect('Table', t.stable, sObjs.map((x) => ({ value: x.name, label: x.name + (x.k ? ` (${x.k})` : '') + (x.rows > 0 ? ` · ≈${fmt.num(Math.round(x.rows))} rows` : '') })),
        (v) => { t.stable = v; if (!t.ttable || t.ttableAuto) { t.ttable = v; t.ttableAuto = true; } if (!t.tschema) t.tschema = t.sschema; trRefresh(); }, 'Choose…', !om.data));
      trAdd(srcFields, trStatus(om, 'tables'));
    } else trAdd(srcFields, trStatus(sm, 'schemas'));
    if (t.stable) trAdd(srcFields, el('div', { class: 'field' }, el('label', null, 'Only rows where (optional)'),
      el('input', { class: 'input mono', value: t.where, placeholder: "created_at >= '2026-01-01' AND status = 'paid'", spellcheck: 'false',
        oninput: (e) => { t.where = e.target.value; } }),
      el('div', { class: 'hint', text: 'A SQL condition on the source table. Leave empty to copy every row. It runs read-only.' })));
  }

  // 2. target
  const tdbs = trTargetDbs();
  const tgt = server(t.tsid);
  const tgtFields = el('div', { class: 'stack' });
  trAdd(tgtFields, el('div', { class: 'field' }, el('label', null, 'Server'),
    el('select', { class: 'input', onchange: (e) => { Object.assign(t, { tsid: e.target.value, tdb: '', cmp: null }); trRefresh(); } },
      state.servers.map((x) => el('option', { value: x.id, selected: x.id === t.tsid }, `${x.name} · ${x.host}:${x.port}`)))));
  if (t.tdbsErr && !tdbs) {
    trAdd(tgtFields, el('div', { class: 'alert danger' }, icon('alert'), el('div', { class: 'body' }, el('b', { text: `Cannot connect to ${tgt ? tgt.name : 'the server'}` }), t.tdbsErr,
      el('div', { style: 'margin-top: 8px' }, el('button', { class: 'btn sm', type: 'button', onclick: () => { t.tdbsErr = null; delete state.dbs[t.tsid]; trRefresh(); } }, 'Retry')))));
  } else if (!tdbs) {
    trAdd(tgtFields, el('div', { class: 'subtle' }, el('span', { class: 'spin' }), ` Connecting to ${tgt ? tgt.name : 'the server'}…`));
  } else {
    const tRow = el('div', { class: 'form-grid', style: 'grid-template-columns: repeat(3, minmax(0, 1fr))' },
      trSelect('Database', t.tdb, tdbs.map((d) => ({ value: d.name, label: d.name })), (v) => { t.tdb = v; t.cmp = null; trRefresh(); }));
    trAdd(tgtFields, tRow);
    if (t.tdb) {
      const tm = trLoad(t.tsid, t.tdb);
      const names = trSchemas(tm);
      const listId = 'tr-schemas';
      trAdd(tRow, el('div', { class: 'field' }, el('label', null, 'Schema'),
        el('input', { class: 'input mono', value: t.tschema, list: listId, spellcheck: 'false', placeholder: 'public',
          onchange: (e) => { t.tschema = e.target.value.trim(); t.cmp = null; trRefresh(); } }),
        el('datalist', { id: listId }, names.map((n) => el('option', { value: n })))));
      let tables = [];
      if (t.tschema && names.includes(t.tschema)) {
        const om = trLoad(t.tsid, t.tdb, t.tschema);
        tables = om.data ? [...om.data.tables] : [];
      }
      trAdd(tRow, el('div', { class: 'field' }, el('label', null, 'Table'),
        el('input', { class: 'input mono', value: t.ttable, list: 'tr-tables', spellcheck: 'false', placeholder: 'table name',
          onchange: (e) => { t.ttable = e.target.value.trim(); t.ttableAuto = false; t.cmp = null; trRefresh(); } }),
        el('datalist', { id: 'tr-tables' }, tables.map((x) => el('option', { value: x.name })))));
      const schemaNew = t.tschema && tm.data && !names.includes(t.tschema);
      const tableNew = t.ttable && t.tschema && !schemaNew && trMeta[`${t.tsid}|${t.tdb}|${t.tschema}`] && trMeta[`${t.tsid}|${t.tdb}|${t.tschema}`].data && !tables.some((x) => x.name === t.ttable);
      trAdd(tgtFields, el('div', { class: 'hint subtle', text: schemaNew ? `Schema ${t.tschema} does not exist on ${t.tdb}; it will be created with the table.`
        : tableNew ? `${t.tschema}.${t.ttable} does not exist yet; it can be created from the source table.` : 'Pick an existing table, or type a new name to create one.' }), tm.loading || tm.error ? trStatus(tm, 'schemas') : null);
    }
  }
  const ready = t.sdb && (trIsQuery() ? t.query.trim() : t.sschema && t.stable) && t.tdb && t.tschema && t.ttable;
  const cmpBtn = el('button', { class: 'btn primary', type: 'button', disabled: !ready }, icon('search'), t.cmp ? 'Compare again' : 'Compare columns');
  cmpBtn.onclick = () => trCompare(cmpBtn);
  if (t.cmp && t.cmpKey !== trKey()) t.cmp = null;

  // 3. columns and 4. existing rows
  const cards = [];
  if (t.cmp) cards.push(trColumnsCard(), trExistingCard());

  // summary
  const start = el('button', { class: 'btn primary lg', type: 'button', style: 'width: 100%' }, icon('swap'), 'Review and start');
  const hint = el('div', { class: 'subtle', style: 'text-align: center' });
  const why = trWhyNot();
  start.disabled = !!why;
  if (t.existing === 'replace' && t.cmp && t.cmp.match !== 'missing') start.className = 'btn danger lg';
  hint.textContent = why || 'Shows the SQL first; nothing changes until you confirm';
  start.onclick = () => trReview(start);
  const c = t.cmp;
  const est = c && c.source.est_rows !== null && c.source.est_rows !== undefined ? '≈' + fmt.num(c.source.est_rows) : '—';
  const matchText = !c ? 'Not compared' : { identical: 'Exact match: direct', different: 'Different: mapped', missing: 'Missing: create it' }[c.match];

  clear(view, el('div', { class: 'grid-main' },
    el('div', { class: 'stack', style: 'gap: 20px; min-width: 0' },
      el('div', { class: 'card' }, el('div', { class: 'card-h' }, stepNum(1), el('h2', { text: 'Copy rows from' }),
        el('div', { class: 'seg sm' }, [['table', 'A table'], ['query', 'A query']].map(([k, label]) => el('button', { type: 'button', class: t.smode === k ? 'on' : '',
          onclick: () => { if (t.smode === k) return; t.smode = k; t.cmp = null; if (k === 'query' && t.ttableAuto) { t.ttable = ''; t.ttableAuto = false; } trRefresh(); } }, label))),
        el('div', { class: 'grow' }), el('span', { class: 'subtle' }, icon('server'), ' ', src ? src.name : '')), el('div', { class: 'card-b' }, srcFields)),
      el('div', { class: 'card' }, el('div', { class: 'card-h' }, stepNum(2), el('h2', { text: 'Into' })),
        el('div', { class: 'card-b' }, tgtFields), el('div', { class: 'card-f' }, el('div', { class: 'grow subtle', text: 'Compare checks whether the two tables have the same columns.' }), cmpBtn)),
      cards),
    el('div', { class: 'sticky-side' }, el('div', { class: 'card' },
      el('div', { class: 'card-h' }, el('h2', { text: 'Summary' })),
      el('div', { class: 'card-b' }, el('div', { class: 'summary-list' },
        el('div', { class: 'r' }, el('span', { text: 'From' }), el('span', { class: 'mono', text: trSourceText() || '—', title: trSourceText() })),
        el('div', { class: 'r' }, el('span', { text: 'Into' }), el('span', { class: 'mono', text: t.ttable && t.tdb ? `${t.tdb}.${t.tschema}.${t.ttable}` : '—' })),
        el('div', { class: 'r' }, el('span', { text: 'Server' }), el('span', { text: tgt ? tgt.name : '—' })),
        el('div', { class: 'r' }, el('span', { text: 'Columns' }), el('span', { text: matchText })),
        el('div', { class: 'r' }, el('span', { text: 'Rows' }), el('span', { text: !trIsQuery() && t.where.trim() ? `${est} before the filter` : est })),
        c && c.match !== 'missing' ? el('div', { class: 'r' }, el('span', { text: 'Existing rows' }), el('span', { text: TR_MODES[t.existing] })) : null),
      el('div', { class: 'alert info', style: 'margin-top: 14px' }, icon('shield'), el('div', { class: 'body', text: 'One transaction on the target: if anything fails or you cancel, nothing is changed there.' }))),
      el('div', { class: 'card-f', style: 'flex-direction: column; align-items: stretch' }, start, hint)))));
  $('content').scrollTop = scrollY;
}

async function trCompare(btn) {
  const t = TR;
  busy(btn, true, 'Comparing…');
  try {
    const key = trKey();
    const r = await api('POST', `/api/servers/${enc(t.sid)}/transfer/compare`, {
      source: trIsQuery() ? { database: t.sdb, query: t.query } : { database: t.sdb, schema: t.sschema, table: t.stable },
      target: { server_id: t.tsid, database: t.tdb, schema: t.tschema, table: t.ttable } });
    t.cmp = r;
    t.cmpKey = key;
    t.map = {};
    for (const m of r.mapping) t.map[m.target] = m.source || '';
    if (r.match === 'missing' || (t.existing === 'skip' && !(r.target.unique_keys || []).length)) t.existing = 'append';
    t.confirm = '';
  } catch (e) { toastError(e, 'Could not compare'); }
  busy(btn, false);
  trRefresh();
}

function trNote(srcCol, tgtCol) {
  if (tgtCol.generated) return 'generated';
  if (!srcCol) return tgtCol.not_null && !tgtCol.default && !tgtCol.identity ? 'required' : 'default';
  if (srcCol.type === tgtCol.type) return 'same';
  return srcCol.category === tgtCol.category || tgtCol.category === 'S' ? 'convert' : 'check';
}

function trColumnsCard() {
  const t = TR;
  const c = t.cmp;
  const srcCols = Object.fromEntries(c.source.columns.map((x) => [x.name, x]));
  let head, body;
  if (c.match === 'missing') {
    head = el('div', { class: 'alert info' }, icon('info'), el('div', { class: 'body' }, el('b', { text: `${t.tschema}.${t.ttable} does not exist on ${t.tdb}` }),
      trIsQuery() ? 'It will be created with one column per column of the query result (same names and types), then filled. It has no primary key; add one later if you need it.'
        : 'It will be created with the source table\'s columns, NOT NULLs, defaults and primary key, then filled. Serial columns become identity columns. Indexes other than the primary key, foreign keys, triggers and grants are not copied.'));
    body = el('div', null, el('div', { class: 'tr-on', text: 'Will run on the target' }), el('pre', { class: 'tr-sql', text: (c.create_sql || []).join(';\n\n') + ';' }));
  } else {
    const identical = c.match === 'identical';
    head = identical
      ? el('div', { class: 'alert ok' }, icon('check'), el('div', { class: 'body' }, el('b', { text: 'The columns match exactly' }),
        `Same ${fmt.plural(c.source.columns.length, 'column')} with the same types: a direct transfer. You can still leave columns out below.`))
      : el('div', { class: 'alert warn' }, icon('alert'), el('div', { class: 'body' }, el('b', { text: 'The columns are different' }),
        'Choose which source column fills each target column. Columns with the same name are matched for you. A target column left empty gets its default (or NULL).'));
    const rows = c.target.columns.map((tc) => {
      const chosen = t.map[tc.name] || '';
      const note = trNote(srcCols[chosen], tc);
      const [cls, label] = TR_NOTE[note];
      const sel = el('select', { class: 'input sm', disabled: tc.generated, 'aria-label': `Source for ${tc.name}`,
        onchange: (e) => { t.map[tc.name] = e.target.value; trRefresh(); } },
      el('option', { value: '' }, tc.generated ? '— computed by the target —' : '— leave empty (default) —'),
      c.source.columns.map((sc) => el('option', { value: sc.name, selected: sc.name === chosen }, `${sc.name} · ${sc.type}`)));
      return el('tr', null,
        el('td', null, el('div', { class: 'cell-name' }, el('span', { class: 'n mono', text: tc.name }),
          (c.target.primary_key || []).includes(tc.name) ? el('span', { class: 'badge accent', text: 'PK' }) : null)),
        el('td', { class: 'mono subtle', text: tc.type + (tc.not_null ? ' not null' : '') }),
        el('td', null, el('span', { class: 'tr-arrow' }, '←')),
        el('td', null, sel),
        el('td', null, el('span', { class: 'badge ' + cls, text: label, title: note === 'check' ? 'Different kinds of type: values that cannot be read as the target type stop the transfer.' : '' })));
    });
    const unused = c.source.columns.filter((sc) => !Object.values(t.map).includes(sc.name)).map((sc) => sc.name);
    body = el('div', { class: 'stack' },
      el('div', { class: 'table-wrap' }, el('table', { class: 't tr-map' },
        el('thead', null, el('tr', null, el('th', null, `Target column (${t.tschema}.${t.ttable})`), el('th', null, 'Type'), el('th'), el('th', null, `Source column (${trIsQuery() ? 'query result' : t.stable})`), el('th', null, ''))),
        el('tbody', null, rows))),
      el('div', { class: 'row wrap', style: 'gap: 8px' },
        el('button', { class: 'btn sm', type: 'button', onclick: () => { for (const m of c.mapping) t.map[m.target] = m.source || ''; trRefresh(); } }, icon('refresh'), 'Match by name again'),
        unused.length ? el('span', { class: 'subtle', text: `Not copied: ${unused.join(', ')}` }) : null));
  }
  return el('div', { class: 'card' }, el('div', { class: 'card-h' }, stepNum(3), el('h2', { text: 'Columns' })), el('div', { class: 'card-b stack' }, head, body));
}

function trExistingCard() {
  const t = TR;
  const c = t.cmp;
  if (c.match === 'missing') return null;
  const keys = c.target.unique_keys || [];
  const cards = el('div', { class: 'radio-cards' }, [
    ['append', 'Add to them', 'Keep the rows already there and add the new ones. A duplicate key stops the transfer.'],
    ['skip', 'Skip rows that already exist', keys.length ? `Rows whose ${keys[0].join(', ')} is already there are left as they are.` : 'Needs a primary key or unique constraint on the target.'],
    ['replace', 'Delete existing rows first', 'Empties the target table (TRUNCATE), then copies. Undone if the transfer fails.'],
  ].map(([k, title, d]) => el('button', { type: 'button', class: 'radio-card' + (t.existing === k ? ' on' : ''), disabled: k === 'skip' && !keys.length,
    onclick: () => { t.existing = k; t.confirm = ''; trRefresh(); } }, el('span', { class: 't', text: title }), el('span', { class: 'd', text: d }))));
  const extra = t.existing === 'replace' ? el('div', { class: 'alert danger' }, icon('alert'), el('div', { class: 'body' },
    el('b', { text: `Every row in ${t.tschema}.${t.ttable} on ${t.tdb} is deleted first` }),
    'Tables that other tables point at (foreign keys) cannot be emptied this way.',
    el('div', { class: 'field', style: 'margin-top: 10px' }, el('label', null, 'Type ', el('span', { class: 'mono', text: t.ttable }), ' to confirm'),
      el('input', { class: 'input mono', value: t.confirm, autocomplete: 'off', spellcheck: 'false', oninput: (e) => { t.confirm = e.target.value; trStartState(); } })))) : null;
  return el('div', { class: 'card' }, el('div', { class: 'card-h' }, stepNum(4), el('h2', { text: 'Rows already in the target' }),
    el('div', { class: 'grow' }), c.target.est_rows !== null ? el('span', { class: 'subtle', text: `≈${fmt.num(c.target.est_rows)} rows there now` }) : null),
  el('div', { class: 'card-b stack' }, cards, extra));
}

function trStartState() {
  const btn = trView && trView.querySelector('.sticky-side .btn.lg');
  if (!btn) return;
  const why = trWhyNot();
  btn.disabled = !!why;
  const hint = btn.nextSibling;
  if (hint) hint.textContent = why || 'Shows the SQL first; nothing changes until you confirm';
}

function trColumns() {
  return Object.entries(TR.map).filter(([, s]) => s).map(([target, source]) => ({ target, source }));
}

function trWhyNot() {
  const t = TR;
  if (trIsQuery() ? !t.sdb || !t.query.trim() : !t.stable) return trIsQuery() ? 'Choose the database and type the query' : 'Choose the source table';
  if (!t.tdb || !t.tschema || !t.ttable) return 'Choose the target table';
  if (!t.cmp) return 'Compare the columns first';
  if (t.cmp.match !== 'missing') {
    if (!trColumns().length) return 'Map at least one column';
    const srcCols = Object.fromEntries(t.cmp.source.columns.map((x) => [x.name, x]));
    const req = t.cmp.target.columns.filter((tc) => trNote(srcCols[t.map[tc.name]], tc) === 'required').map((tc) => tc.name);
    if (req.length) return `Map a source column to ${req.join(', ')} (NOT NULL, no default)`;
    if (t.existing === 'replace' && t.confirm !== t.ttable) return 'Type the table name to confirm';
  }
  return '';
}

function trSpec() {
  const t = TR;
  return { source: trIsQuery() ? { database: t.sdb, query: t.query } : { database: t.sdb, schema: t.sschema, table: t.stable, where: t.where.trim() || null },
    target: { server_id: t.tsid, database: t.tdb, schema: t.tschema, table: t.ttable },
    create: t.cmp.match === 'missing', columns: t.cmp.match === 'missing' ? null : trColumns(),
    existing: t.cmp.match === 'missing' ? 'append' : t.existing, confirm: t.existing === 'replace' ? t.confirm : null };
}

async function trReview(btn) {
  const t = TR;
  const spec = trSpec();
  busy(btn, true, 'Checking…');
  let p;
  try { p = await api('POST', `/api/servers/${enc(t.sid)}/transfer/preview`, spec); } catch (e) { busy(btn, false); toastError(e, 'Cannot transfer'); return; }
  busy(btn, false);
  const tgt = server(t.tsid);
  const go1 = el('button', { class: 'btn ' + (spec.existing === 'replace' ? 'danger' : 'primary'), type: 'button' }, icon('play'), 'Start transfer');
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
  const m = modal({ title: 'Start this transfer?', iconName: 'swap', wide: true,
    sub: `${trSourceText()} → ${tgt ? tgt.name + ' / ' : ''}${t.tdb}.${t.tschema}.${t.ttable}`,
    body: [
      el('div', { class: 'summary-list' },
        el('div', { class: 'r' }, el('span', { text: 'Rows to copy' }), el('span', { text: p.est_rows !== null ? '≈' + fmt.num(p.est_rows) : 'not known' })),
        el('div', { class: 'r' }, el('span', { text: 'Columns' }), el('span', { text: String(p.columns.length) })),
        el('div', { class: 'r' }, el('span', { text: 'Existing rows' }), el('span', { text: spec.create ? 'New table' : TR_MODES[spec.existing] })),
        p.sequences.length ? el('div', { class: 'r' }, el('span', { text: 'Sequences' }), el('span', { text: 'moved forward: ' + p.sequences.join(', ') })) : null),
      p.warnings.length ? el('div', { class: 'alert warn' }, icon('alert'), el('div', { class: 'body' }, p.warnings.map((w) => el('div', { text: w })))) : null,
      el('div', null, p.statements.map((s, i) => [i === 0 || s.on !== p.statements[i - 1].on ? el('div', { class: 'tr-on', text: s.on === 'source' ? 'Reads on the source (read-only)' : 'Runs on the target, in one transaction' }) : null,
        el('pre', { class: 'tr-sql', style: 'margin-bottom: 6px', text: s.sql })])),
    ],
    actions: [cancel, go1] });
  cancel.onclick = () => m.close();
  go1.onclick = async () => {
    busy(go1, true, 'Starting…');
    try {
      const res = await api('POST', `/api/servers/${enc(t.sid)}/transfer`, spec);
      m.close();
      toast(`Transfer into ${t.tdb}.${t.tschema}.${t.ttable} started.`, 'ok');
      state.openJob = res.job_id;
      t.cmp = null;
      delete trMeta[`${t.tsid}|${t.tdb}`];
      delete trMeta[`${t.tsid}|${t.tdb}|${t.tschema}`];
      await pollJobs();
      go(`#/activity/${enc(res.job_id)}`);
    } catch (e) { busy(go1, false); m.error(e.message); }
  };
}
