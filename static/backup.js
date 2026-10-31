/* PG Backup Studio: the backup panel's table list (moved out of index.html). Two views: List (one table, sorted by
 * size or name) and Grid (cards grouped by size ranges fitted to the database's tables). Uses the page's helpers (el, clear,
 * fmt, icon, toast, tkey, store, refreshBackup) inside functions only. Text is always set through text nodes.
 */
'use strict';

function setOpt(b, schema, table, patch) {
  const k = tkey(schema, table);
  b.opts.set(k, Object.assign({ data: 'full', order_by: null, limit: 1000 }, b.opts.get(k) || {}, patch));
}

const TABLE_PAGE = 100;
const SIZE_UNITS = [['GB', 2 ** 30], ['MB', 2 ** 20], ['KB', 2 ** 10]];

// Smallest "round" size >= bytes: 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5 × a power of ten, in KB, MB or GB (130 MB → 150 MB).
function niceSize(bytes) {
  const [, unit] = SIZE_UNITS.find(([, u]) => bytes >= u) || SIZE_UNITS[2];
  const x = Math.max(bytes / unit, 1), mag = 10 ** Math.floor(Math.log10(x));
  return [1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10].map((m) => m * mag).find((v) => v >= x - 1e-9) * unit;
}
function sizeText(bytes) {
  if (!bytes) return '0';
  const [name, unit] = SIZE_UNITS.find(([, u]) => bytes >= u) || SIZE_UNITS[2];
  return `${+(bytes / unit).toFixed(2)} ${name}`;
}

// Size ranges fitted to this database's tables: up to 10 groups with about the same number of tables each, cut at
// round sizes (e.g. 0–1 MB, 1–5 MB, 5–20 MB, 20–100 MB, 100–200 MB, …). Returns the ascending upper bounds.
function sizeBounds(tables) {
  const sizes = tables.map((t) => t.size || 0).sort((x, y) => x - y);
  if (!sizes.length) return [];
  const groups = Math.min(10, Math.max(1, Math.round(Math.sqrt(sizes.length))));
  const bounds = [];
  for (let i = 1; i < groups; i++) {
    const cut = niceSize(sizes[Math.floor((i * sizes.length) / groups)] + 1);
    if (cut > (bounds[bounds.length - 1] || 0) && cut <= sizes[sizes.length - 1]) bounds.push(cut);
  }
  bounds.push(niceSize(sizes[sizes.length - 1] + 1));
  return bounds;
}
const rangeIndex = (bytes, bounds) => { const i = bounds.findIndex((hi) => (bytes || 0) < hi); return i < 0 ? bounds.length - 1 : i; };
const rangeLabel = (i, bounds) => `${sizeText(i ? bounds[i - 1] : 0)} – ${sizeText(bounds[i])}`;

function tablesCard(b) {
  const c = b.check;
  const card = el('div', { class: 'card' });
  const head = el('div', { class: 'card-h' }, el('h2', { text: 'Tables and data options' }),
    c ? el('span', { class: 'desc', text: `${fmt.plural(c.tables.length, 'table')} · ${c.tables.filter((t) => t.large).length} large` }) : null);
  card.appendChild(head);
  if (!c) { card.appendChild(el('div', { class: 'loading' }, el('span', { class: 'spin' }), 'Waiting for the check…')); return card; }
  if (!c.tables.length) { card.appendChild(el('div', { class: 'empty' }, el('p', { text: 'No tables in the selected schemas.' }))); return card; }
  if (!b.view) b.view = store('tableView') === 'grid' ? 'grid' : 'list';
  const schemaMode = b.mode === 'schema';
  const optOf = (t) => b.opts.get(tkey(t.schema, t.name)) || { data: 'full' };
  const filter = el('input', { class: 'input sm', placeholder: 'Filter tables', value: b.filter, 'data-filter': '1', 'aria-label': 'Filter tables' });
  const show = el('select', { class: 'input sm', style: 'width: auto', 'aria-label': 'Which tables to show' },
    [['all', 'All tables'], ['large', 'Large tables'], ['changed', 'Not full']].map(([v, t]) => el('option', { value: v, selected: b.show === v }, t)));
  const sort = el('select', { class: 'input sm', style: 'width: auto', 'aria-label': 'Sort tables' },
    [['size', 'Largest first'], ['name', 'By name']].map(([v, t]) => el('option', { value: v, selected: b.sort === v }, t)));
  const viewSeg = el('div', { class: 'seg sm', role: 'group', 'aria-label': 'View' });
  const all = el('input', { type: 'checkbox', 'aria-label': 'Select every table on this page' });
  const bulk = el('div', { class: 'toolbar bulk' });
  const pager = el('div', { class: 'row wrap', style: 'padding: 10px 18px; gap: 10px' });
  const scroller = el('div', { class: 'table-wrap', style: 'max-height: 640px; overflow-y: auto', 'data-tw': '1', onscroll: (e) => { b.tScroll = e.target.scrollTop; } });
  const bounds = sizeBounds(c.tables);   // grid groups, fitted to every table in the check (stable while filtering)
  let matched = [];
  let shown = [];

  const setMany = (keys, data) => {
    let skipped = 0;
    for (const t of c.tables) {
      const k = tkey(t.schema, t.name);
      if (!keys.has(k)) continue;
      const o = optOf(t);
      const order = o.order_by || t.default_order_column;
      if (data === 'latest' && !order) { skipped++; continue; }
      setOpt(b, t.schema, t.name, { data, order_by: order });
    }
    if (skipped) toast(`${fmt.plural(skipped, 'table')} kept their option: no primary key or date/time column to order "Latest N" by.`, 'warn');
    refreshBackup();
  };
  const drawBulk = () => {
    const n = b.sel.size;
    clear(bulk,
      el('span', { style: 'font-weight: 600', text: n ? `${fmt.plural(n, 'table')} selected` : 'Tick tables to change several at once' }),
      matched.length > shown.length && n < matched.length ? el('button', { class: 'btn sm ghost', type: 'button', onclick: () => { for (const t of matched) b.sel.add(tkey(t.schema, t.name)); draw(); } }, `Select all ${fmt.num(matched.length)} matching`) : null,
      n ? el('button', { class: 'btn sm ghost', type: 'button', onclick: () => { b.sel.clear(); draw(); } }, 'Clear selection') : null,
      el('div', { class: 'grow' }),
      el('span', { class: 'subtle', text: 'Set to' }),
      el('div', { class: 'seg sm' }, [['full', 'Full'], ['latest', 'Latest N'], ['schema', 'Schema only']].map(([v, label]) =>
        el('button', { type: 'button', disabled: !n || schemaMode, onclick: () => setMany(b.sel, v) }, label))));
  };
  const drawViewSeg = () => clear(viewSeg, [['list', 'List'], ['grid', 'Grid']].map(([v, label]) =>
    el('button', { type: 'button', class: b.view === v ? 'on' : '', 'aria-pressed': String(b.view === v),
      onclick: () => { if (b.view === v) return; b.view = v; store('tableView', v); b.page = 0; b.tScroll = 0; scroller.scrollTop = 0; drawViewSeg(); draw(); } }, label)));
  const emptyText = () => b.show === 'large' ? 'No large tables. Choose "All tables" to see every table.'
    : b.show === 'changed' ? 'Every table is backed up in full.' : 'No table matches the filter.';

  const listBody = () => {
    const tbody = el('tbody', null, shown.map((t) => tableRow(b, t, schemaMode, draw)));
    if (!shown.length) tbody.appendChild(el('tr', null, el('td', { colspan: '5' }, el('div', { class: 'empty', style: 'padding: 20px' }, el('p', { text: emptyText() })))));
    return el('table', { class: 't' },
      el('thead', null, el('tr', null, el('th', { style: 'width: 34px' }, all), el('th', null, 'Table'), el('th', { class: 'num' }, 'Size'), el('th', { class: 'num' }, 'Est. rows'), el('th', null, 'Data to back up'))),
      tbody);
  };
  const gridBody = () => {
    if (!shown.length) return el('div', { class: 'empty', style: 'padding: 20px' }, el('p', { text: emptyText() }));
    const totals = new Map();   // per range over every matching table, not just this page
    for (const t of matched) {
      const key = rangeIndex(t.size, bounds);
      const g = totals.get(key) || { count: 0, bytes: 0, keys: [] };
      g.count++; g.bytes += t.size || 0; g.keys.push(tkey(t.schema, t.name));
      totals.set(key, g);
    }
    const out = [];
    let cur = null;
    for (const t of shown) {
      const ri = rangeIndex(t.size, bounds);
      if (!cur || cur.ri !== ri) {
        const g = totals.get(ri);
        const allSel = g.keys.every((k) => b.sel.has(k));
        cur = { ri, grid: el('div', { class: 'tgrid' }) };
        out.push(el('div', { class: 'tg-h' },
          el('b', { text: rangeLabel(ri, bounds) }),
          el('span', { class: 'subtle', text: `${fmt.plural(g.count, 'table')} · ${fmt.bytes(g.bytes)}` }),
          el('div', { class: 'grow' }),
          el('button', { class: 'btn sm ghost', type: 'button',
            onclick: () => { for (const k of g.keys) { if (allSel) b.sel.delete(k); else b.sel.add(k); } draw(); } },
          allSel ? 'Clear group' : `Select group (${fmt.num(g.count)})`)), cur.grid);
      }
      cur.grid.appendChild(tableTile(b, t, schemaMode, draw));
    }
    return el('div', { class: 'tgroups' }, out);
  };

  const draw = () => {
    const q = b.filter.toLowerCase();
    const bySort = b.sort === 'name' ? (x, y) => x.qname.localeCompare(y.qname)
      : (x, y) => (y.large - x.large) || (y.size - x.size) || x.qname.localeCompare(y.qname);
    // grid: largest size range first, then the chosen order inside each range
    const order = b.view === 'grid' ? (x, y) => (rangeIndex(y.size, bounds) - rangeIndex(x.size, bounds)) || bySort(x, y) : bySort;
    matched = c.tables.filter((t) => (b.show !== 'large' || t.large) && (b.show !== 'changed' || optOf(t).data !== 'full') && (!q || t.qname.toLowerCase().includes(q))).sort(order);
    const pages = Math.max(1, Math.ceil(matched.length / TABLE_PAGE));
    b.page = Math.min(Math.max(0, b.page), pages - 1);
    shown = matched.slice(b.page * TABLE_PAGE, (b.page + 1) * TABLE_PAGE);
    const top = scroller.scrollTop;
    clear(scroller, b.view === 'grid' ? gridBody() : listBody());
    scroller.scrollTop = top;
    const onPage = shown.filter((t) => b.sel.has(tkey(t.schema, t.name))).length;
    all.checked = shown.length > 0 && onPage === shown.length;
    all.indeterminate = onPage > 0 && onPage < shown.length;
    const from = matched.length ? b.page * TABLE_PAGE + 1 : 0;
    const turn = (d) => { b.page += d; scroller.scrollTop = 0; draw(); };
    clear(pager,
      el('span', { class: 'subtle', text: `${fmt.num(from)}–${fmt.num(b.page * TABLE_PAGE + shown.length)} of ${fmt.num(matched.length)} tables${matched.length < c.tables.length ? ` (${fmt.num(c.tables.length)} in total)` : ''}` }),
      el('div', { class: 'grow' }),
      pages > 1 ? el('button', { class: 'btn sm', type: 'button', disabled: b.page === 0, onclick: () => turn(-1) }, 'Previous') : null,
      pages > 1 ? el('span', { class: 'subtle', text: `Page ${b.page + 1} of ${pages}` }) : null,
      pages > 1 ? el('button', { class: 'btn sm', type: 'button', disabled: b.page >= pages - 1, onclick: () => turn(1) }, 'Next') : null);
    drawBulk();
  };
  filter.addEventListener('input', () => { b.filter = filter.value; b.page = 0; draw(); });
  show.addEventListener('change', () => { b.show = show.value; b.page = 0; draw(); });
  sort.addEventListener('change', () => { b.sort = sort.value; b.page = 0; draw(); });
  all.addEventListener('change', () => { for (const t of shown) { const k = tkey(t.schema, t.name); if (all.checked) b.sel.add(k); else b.sel.delete(k); } draw(); });
  drawViewSeg();
  draw();
  card.append(
    el('div', { class: 'toolbar' }, el('div', { class: 'search' }, icon('search'), filter), show, sort, viewSeg, el('div', { class: 'grow' }),
      schemaMode ? el('span', { class: 'badge', text: 'Schema-only mode: no rows are copied' }) : null),
    bulk, scroller, pager);
  return card;
}

// The pieces one table shows in either view: tick box, name with badges, size, rows, data option controls.
function tableParts(b, t, schemaMode, redraw) {
  const k = tkey(t.schema, t.name);
  const o = b.opts.get(k) || { data: 'full', order_by: t.default_order_column, limit: 1000 };
  const seg = el('div', { class: 'seg sm' }, [['full', 'Full'], ['latest', 'Latest N'], ['schema', 'Schema only']].map(([v, label]) =>
    el('button', { type: 'button', class: o.data === v ? 'on' : '', disabled: schemaMode || (v === 'latest' && !t.order_columns.length && !o.order_by),
      title: v === 'latest' && !t.order_columns.length ? 'No primary key or date/time column to order by' : null,
      onclick: () => { setOpt(b, t.schema, t.name, { data: v, order_by: o.order_by || t.default_order_column }); refreshBackup(); } }, label)));
  const option = [seg];
  if (o.data === 'latest' && !schemaMode) {
    const cols = t.order_columns.slice();
    if (o.order_by && !cols.some((c) => c.name === o.order_by)) cols.push({ name: o.order_by, type: '?', pk: false, timestamp_like: false, index: null });
    const sel = el('select', { class: 'input sm', 'aria-label': 'Ordering column', onchange: (e) => { setOpt(b, t.schema, t.name, { order_by: e.target.value }); refreshBackup(); } },
      cols.map((c) => el('option', { value: c.name, selected: c.name === o.order_by },
        `${c.name} (${c.type}${c.pk ? ', PK' : ''})${c.name === t.default_order_column ? ' · suggested' : ''}${c.index && c.index.usable ? ' · indexed' : ''}`)));
    const n = el('input', { class: 'input sm n', value: String(o.limit), inputmode: 'numeric', 'aria-label': 'Number of newest rows',
      onchange: (e) => { setOpt(b, t.schema, t.name, { limit: e.target.value }); refreshBackup(); } });
    const chosen = cols.find((c) => c.name === o.order_by);
    const idx = chosen && chosen.index;
    option.push(el('div', { class: 'order-cell' }, el('span', { class: 'subtle', text: 'newest' }), n, el('span', { class: 'subtle', text: 'rows by' }), sel));
    if (idx && idx.message) option.push(el('div', { class: 'advice ' + (idx.usable ? 'good' : 'bad') }, icon(idx.usable ? 'check' : 'alert'), el('span', { text: idx.message })));
  }
  if (t.option_error) option.push(el('div', { class: 'advice bad', style: 'color: var(--danger)' }, icon('alert'), el('span', { text: t.option_error })));
  const pick = el('input', { type: 'checkbox', checked: b.sel.has(k), 'aria-label': `Select ${t.qname}`,
    onchange: (e) => { if (e.target.checked) b.sel.add(k); else b.sel.delete(k); redraw(); } });
  const name = el('div', { class: 'cell-name' }, el('span', { class: 'n', text: t.qname, title: t.qname }),
    t.large ? el('span', { class: 'badge warn', title: 'Over the large-table threshold (' + t.large_reason.join(' and ') + ')' }, 'large') : null,
    t.kind === 'partitioned' ? el('span', { class: 'badge info', text: `${t.partitions} partitions` }) : null);
  const size = [fmt.bytes(t.size), t.size_exact ? '' : ' ≈'];
  const sizeTitle = t.size_exact ? '' : 'Estimated (no permission for the exact size)';
  const rows = t.rows_unknown ? el('span', { class: 'subtle', text: 'not analyzed' }) : '≈ ' + fmt.num(t.est_rows);
  const rowsText = t.rows_unknown ? 'rows not analyzed' : `≈ ${fmt.num(t.est_rows)} rows`;
  return { pick, name, size, sizeTitle, rows, rowsText, option };
}

function tableRow(b, t, schemaMode, redraw) {
  const p = tableParts(b, t, schemaMode, redraw);
  return el('tr', { class: t.large ? 'large' : '' },
    el('td', null, p.pick),
    el('td', null, p.name),
    el('td', { class: 'num', title: p.sizeTitle }, ...p.size),
    el('td', { class: 'num' }, p.rows),
    el('td', null, ...p.option));
}

function tableTile(b, t, schemaMode, redraw) {
  const p = tableParts(b, t, schemaMode, redraw);
  return el('div', { class: 'tcard' + (t.large ? ' large' : '') },
    el('div', { class: 'row', style: 'gap: 8px; min-width: 0' }, p.pick, p.name),
    el('div', { class: 'subtle', title: p.sizeTitle }, ...p.size, ' · ', p.rowsText),
    ...p.option);
}
