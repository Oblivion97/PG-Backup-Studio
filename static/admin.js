/* PG Backup Studio: the per-server Admin tab (decision 011) — Sessions (find stale ones, cancel / end) and Roles &
 * users (create, edit, passwords, memberships, drop). Uses the page's helpers (el, clear, icon, api, modal, busy, toast,
 * toastError, confirmBox, errorCard, loadingView, stale, statBox, fmt, server, state, enc, store, routeToken) inside
 * functions only. Text is always set through text nodes. What is offered follows the login's rights
 * (/admin/capabilities); the server is the final judge and its refusals are shown as they come.
 */
'use strict';

const ADM = { sub: 'sessions', filter: '', stateFilter: 'all', workers: false, staleOnly: false, auto: 0, picked: new Set(),
  roleFilter: '', builtin: false, timer: null, data: null, roles: null };
const ADM_STATES = [['all', 'All states'], ['active', 'Active'], ['idle', 'Idle'], ['idle in transaction', 'Idle in transaction'],
  ['idle in transaction (aborted)', 'Idle in transaction (aborted)'], ['fastpath function call', 'Fastpath'], ['disabled', 'Disabled']];

function admNum(key, dflt) { const v = Number(store('admin.' + key)); return v > 0 ? v : dflt; }
function admStaleTx() { return admNum('staleTx', 5); }       // minutes "idle in transaction" before a session counts as stale
function admStaleIdle() { return admNum('staleIdle', 30); }  // minutes "idle" before a session counts as stale

/** 'tx' (idle in transaction too long: holds locks), 'idle' (idle too long) or null. */
function admStale(r) {
  if (!r.client || r.state_secs === null) return null;
  if (r.state && r.state.startsWith('idle in transaction') && r.state_secs >= admStaleTx() * 60) return 'tx';
  if (r.state === 'idle' && r.state_secs >= admStaleIdle() * 60) return 'idle';
  return null;
}

function admStopTimer() { if (ADM.timer) { clearInterval(ADM.timer); ADM.timer = null; } }

async function viewAdmin(view, token) {
  admStopTimer();
  ADM.picked.clear();
  const sid = state.route.sid;
  loadingView(view, 'Checking what this login may do…');
  let caps;
  try { caps = await api('GET', `/api/servers/${enc(sid)}/admin/capabilities`); } catch (e) {
    if (stale(token)) return;
    errorCard(view, e, () => route());
    return;
  }
  if (stale(token)) return;
  ADM.caps = caps;
  const body = el('div');
  const seg = el('div', { class: 'seg' });
  const drawSeg = () => clear(seg, [['sessions', 'Sessions', 'activity'], ['roles', 'Roles & users', 'key']].map(([k, label, ic]) =>
    el('button', { type: 'button', class: ADM.sub === k ? 'on' : '', onclick: () => { ADM.sub = k; drawSeg(); show(); } }, icon(ic), ' ', label)));
  const rights = [caps.superuser ? 'superuser' : null, !caps.superuser && caps.createrole ? 'can create roles' : null,
    !caps.superuser && caps.signal_backend ? "can end other users' sessions" : null].filter(Boolean);
  const note = caps.can_manage_roles || caps.can_end_others ? null : el('div', { class: 'alert info', style: 'margin-bottom: 14px' }, icon('info'),
    el('div', { class: 'body' }, el('b', { text: 'Read-only for this login' }),
      `${caps.user} is not a superuser, has no CREATEROLE and is not in pg_signal_backend, so it can see sessions and roles and end only its own sessions. Save the server with an admin login to manage them.`));
  clear(view, el('div', { class: 'row', style: 'margin-bottom: 14px; flex-wrap: wrap' }, seg, el('div', { class: 'grow' }),
    el('span', { class: 'subtle' }, 'Logged in as ', el('b', { class: 'mono', text: caps.user }), rights.length ? ` · ${rights.join(' · ')}` : ' · regular role')),
  note, body);
  const show = () => { admStopTimer(); (ADM.sub === 'roles' ? admRoles : admSessions)(body, token); };
  drawSeg();
  show();
}

/* ---------------------------------------------------------------- sessions */

async function admSessions(box, token) {
  const sid = state.route.sid;
  if (!ADM.data || ADM.data.sid !== sid) loadingView(box, 'Loading sessions…');
  const load = async (quiet) => {
    try {
      const d = await api('GET', `/api/servers/${enc(sid)}/admin/sessions`);
      if (stale(token) || ADM.sub !== 'sessions') return;
      ADM.data = Object.assign(d, { sid });
      const alive = new Set(d.sessions.map((r) => r.pid));
      for (const p of [...ADM.picked]) if (!alive.has(p)) ADM.picked.delete(p);
      admDrawSessions(box, token, load);
    } catch (e) {
      if (stale(token)) return;
      if (quiet) toastError(e, 'Refresh failed'); else errorCard(box, e, () => load());
    }
  };
  await load();
}

function admDrawSessions(box, token, load) {
  const d = ADM.data, caps = d.capabilities;
  const q = ADM.filter.toLowerCase();
  const rows = d.sessions.filter((r) => (ADM.workers || r.client)
    && (ADM.stateFilter === 'all' || r.state === ADM.stateFilter)
    && (!ADM.staleOnly || admStale(r))
    && (!q || [r.user, r.database, r.app, r.client_addr, r.query, String(r.pid)].some((v) => v && String(v).toLowerCase().includes(q))));
  const clients = d.sessions.filter((r) => r.client);
  const count = (f) => clients.filter(f).length;
  const staleN = count((r) => admStale(r));
  const stats = el('div', { class: 'stats' },
    statBox('Client sessions', fmt.num(clients.length), `${fmt.num(d.sessions.length - clients.length)} background processes`),
    statBox('Active', fmt.num(count((r) => r.state === 'active')), 'running a query now'),
    statBox('Idle', fmt.num(count((r) => r.state === 'idle')), 'connected, doing nothing'),
    statBox('Idle in transaction', fmt.num(count((r) => r.state && r.state.startsWith('idle in transaction'))), 'holding locks until they finish'),
    statBox('Stale', fmt.num(staleN), `idle in tx > ${admStaleTx()} min, idle > ${admStaleIdle()} min`));

  const filter = el('input', { class: 'input', placeholder: 'Filter: user, database, app, client, query, PID', value: ADM.filter, style: 'max-width: 320px', 'aria-label': 'Filter sessions' });
  filter.oninput = () => { ADM.filter = filter.value; admDrawSessions(box, token, load); const f = box.querySelector('[aria-label="Filter sessions"]'); f.focus(); f.setSelectionRange(f.value.length, f.value.length); };
  const stSel = el('select', { class: 'input', style: 'width: auto', 'aria-label': 'State' },
    ADM_STATES.map(([k, label]) => el('option', { value: k, selected: ADM.stateFilter === k }, label)));
  stSel.onchange = () => { ADM.stateFilter = stSel.value; admDrawSessions(box, token, load); };
  const chk = (label, key, title) => el('label', { class: 'check', title: title || null },
    el('input', { type: 'checkbox', checked: ADM[key], onchange: (e) => { ADM[key] = e.target.checked; admDrawSessions(box, token, load); } }), label);
  const num = (key, dflt, label) => {
    const i = el('input', { class: 'input', inputmode: 'numeric', value: String(admNum(key, dflt)), style: 'width: 56px', 'aria-label': label });
    i.onchange = () => { const v = Math.round(Number(i.value)); if (v > 0) store('admin.' + key, String(v)); admDrawSessions(box, token, load); };
    return i;
  };
  const autoSel = el('select', { class: 'input', style: 'width: auto', 'aria-label': 'Auto refresh' },
    [[0, 'Auto refresh: off'], [5, 'Every 5 s'], [15, 'Every 15 s']].map(([v, label]) => el('option', { value: String(v), selected: ADM.auto === v }, label)));
  autoSel.onchange = () => { ADM.auto = Number(autoSel.value); setTimer(); };
  const refresh = el('button', { class: 'btn', type: 'button', onclick: async () => { busy(refresh, true, 'Refreshing…'); await load(true); } }, icon('refresh'), 'Refresh');
  const setTimer = () => {
    admStopTimer();
    if (ADM.auto) ADM.timer = setInterval(() => {
      if (stale(token) || ADM.sub !== 'sessions') { admStopTimer(); return; }
      if (!modals.length && !document.querySelector('.menu')) load(true);
    }, ADM.auto * 1000);
  };
  setTimer();

  const selectable = (r) => r.client && r.can_signal;
  const picked = d.sessions.filter((r) => ADM.picked.has(r.pid));
  const pickStale = el('button', { class: 'btn', type: 'button', disabled: !staleN, title: 'Tick every stale session this login may end',
    onclick: () => { ADM.picked = new Set(clients.filter((r) => admStale(r) && selectable(r)).map((r) => r.pid)); ADM.staleOnly = true; admDrawSessions(box, token, load); } },
  icon('clock'), `Select stale (${staleN})`);
  const act = (action) => admSignal(action, picked, load);
  const cancelBtn = el('button', { class: 'btn', type: 'button', disabled: !picked.length, onclick: () => act('cancel'),
    title: 'Stop the running query; the connection stays open' }, icon('stop'), 'Cancel query');
  const endBtn = el('button', { class: 'btn danger', type: 'button', disabled: !picked.length, onclick: () => act('terminate'),
    title: 'Disconnect the session; an open transaction is rolled back' }, icon('x'), `End ${picked.length ? fmt.plural(picked.length, 'session') : 'sessions'}`);

  const all = el('input', { type: 'checkbox', 'aria-label': 'Select all shown', checked: rows.some(selectable) && rows.filter(selectable).every((r) => ADM.picked.has(r.pid)) });
  all.onchange = () => { for (const r of rows.filter(selectable)) { if (all.checked) ADM.picked.add(r.pid); else ADM.picked.delete(r.pid); } admDrawSessions(box, token, load); };
  const tbody = el('tbody', null, rows.length ? rows.map((r) => admSessionRow(r, () => admDrawSessions(box, token, load))) :
    el('tr', null, el('td', { colspan: '10', class: 'subtle', style: 'text-align: center; padding: 28px', text: d.sessions.length ? 'No sessions match the filters.' : 'No other sessions.' })));

  clear(box, el('div', { class: 'card', style: 'margin-bottom: 14px' }, stats),
    el('div', { class: 'card' },
      el('div', { class: 'toolbar' }, filter, stSel, chk('Background processes', 'workers', 'Autovacuum, WAL writer, replication… (shown, never ended from here)'),
        chk('Stale only', 'staleOnly'), el('div', { class: 'grow' }), autoSel, refresh),
      el('div', { class: 'toolbar' },
        el('span', { class: 'subtle' }, 'Stale when idle in transaction for ≥'), num('staleTx', 5, 'Idle in transaction minutes'),
        el('span', { class: 'subtle' }, 'min or idle for ≥'), num('staleIdle', 30, 'Idle minutes'), el('span', { class: 'subtle', text: 'min' }),
        el('div', { class: 'grow' }), pickStale, cancelBtn, endBtn),
      caps.can_end_others ? null : el('div', { class: 'alert info', style: 'margin: 12px 18px 0' }, icon('info'),
        el('div', { class: 'body', text: "This login can cancel or end only its own sessions (and those of roles it belongs to). Others' boxes are greyed out." })),
      el('div', { class: 'table-wrap' }, el('table', { class: 't' },
        el('thead', null, el('tr', null, el('th', { style: 'width: 34px' }, all), ...['PID', 'User', 'Database', 'Application', 'Client', 'State', 'For', 'Wait', 'Query'].map((h) => el('th', { text: h })))),
        tbody)),
      el('div', { class: 'subtle', style: 'padding: 10px 18px', text: `Updated ${new Date(d.now).toLocaleTimeString()} · the app's own connection is not listed` })));
}

function admSessionRow(r, redraw) {
  const st = admStale(r);
  const can = r.client && r.can_signal;
  const cb = el('input', { type: 'checkbox', checked: ADM.picked.has(r.pid), disabled: !can, 'aria-label': `Select session ${r.pid}`,
    title: can ? null : !r.client ? `${r.backend_type}: not ended from here` : r.superuser_session ? 'A superuser session: only a superuser may end it' : 'This login may not end this session',
    onchange: (e) => { if (e.target.checked) ADM.picked.add(r.pid); else ADM.picked.delete(r.pid); redraw(); } });
  const stateBadge = !r.client ? el('span', { class: 'badge', text: r.backend_type })
    : el('span', { class: 'badge ' + (r.state === 'active' ? 'ok' : st === 'tx' || (r.state || '').startsWith('idle in transaction') ? 'danger' : st ? 'warn' : ''), text: r.state || '—' });
  const q = (r.query || '').replace(/\s+/g, ' ').trim();
  const qCell = q ? el('button', { type: 'button', class: 'mono', title: 'Show the full query',
    style: 'all: unset; cursor: pointer; font-family: var(--mono); font-size: 12px; display: block; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
    onclick: () => admShowQuery(r) }, q) : el('span', { class: 'subtle', text: '—' });
  const forSecs = r.state === 'active' ? r.query_secs : r.state_secs;
  return el('tr', { class: st ? 'large' : '', style: st === 'tx' ? 'box-shadow: inset 3px 0 0 var(--danger)' : st ? 'box-shadow: inset 3px 0 0 var(--warn)' : null },
    el('td', null, cb),
    el('td', { class: 'mono', text: String(r.pid) }),
    el('td', null, r.user || el('span', { class: 'subtle', text: '—' }), r.superuser_session ? el('span', { class: 'badge accent', style: 'margin-left: 6px', title: 'Superuser' }, icon('shield')) : null),
    el('td', { text: r.database || '—' }),
    el('td', null, r.app || el('span', { class: 'subtle', text: '—' }), r.this_app ? el('span', { class: 'badge info', style: 'margin-left: 6px', text: 'this app', title: 'Opened by PG Backup Studio (this or another PC)' }) : null),
    el('td', { class: 'mono', text: r.client_addr || (r.client ? 'local socket' : '—') }),
    el('td', null, stateBadge, st ? el('span', { class: 'badge ' + (st === 'tx' ? 'danger' : 'warn'), style: 'margin-left: 6px', text: 'stale' }) : null),
    el('td', { text: fmt.dur(forSecs), title: r.xact_secs !== null ? `Transaction open for ${fmt.dur(r.xact_secs)}` : null }),
    el('td', { class: 'subtle', text: r.wait_event ? `${r.wait_event_type}: ${r.wait_event}` : '—' }),
    el('td', null, qCell));
}

function admShowQuery(r) {
  const close = el('button', { class: 'btn', type: 'button' }, 'Close');
  const m = modal({ title: `Session ${r.pid}`, iconName: 'code', wide: true, sub: `${r.user || '—'} · ${r.database || '—'} · ${r.state || r.backend_type}`,
    body: [
      el('div', { class: 'subtle', style: 'margin-bottom: 8px', text: `${r.state === 'active' ? 'Running for' : 'Last query; state for'} ${fmt.dur(r.state === 'active' ? r.query_secs : r.state_secs)} · connected ${fmt.date(r.backend_start)}` }),
      el('pre', { class: 'mono', style: 'white-space: pre-wrap; word-break: break-word; max-height: 50vh; overflow: auto; margin: 0; padding: 12px; background: var(--surface-2); border-radius: var(--radius-sm); font-size: 12.5px', text: r.query || '' }),
    ],
    actions: [close] });
  close.onclick = () => m.close();
}

async function admSignal(action, picked, load) {
  if (!picked.length) return;
  const list = el('div', { style: 'max-height: 220px; overflow: auto; margin-top: 10px; display: grid; gap: 4px' },
    picked.slice(0, 50).map((r) => el('div', { class: 'mono', style: 'font-size: 12px', text: `${r.pid} · ${r.user || '—'}@${r.database || '—'} · ${r.state || ''}${r.state_secs !== null ? ' ' + fmt.dur(r.state_secs) : ''}` })),
    picked.length > 50 ? el('div', { class: 'subtle', text: `… and ${picked.length - 50} more` }) : null);
  const end = action === 'terminate';
  const yes = await confirmBox({ title: end ? `End ${fmt.plural(picked.length, 'session')}?` : `Cancel the query in ${fmt.plural(picked.length, 'session')}?`,
    danger: end, confirmLabel: end ? 'End sessions' : 'Cancel queries',
    message: end ? 'They are disconnected; any open transaction is rolled back. Applications may reconnect.' : 'The running statement is stopped with an error; the connections stay open.',
    extra: list });
  if (!yes) return;
  try {
    const r = await api('POST', `/api/servers/${enc(state.route.sid)}/admin/sessions/${action}`, { pids: picked.map((p) => p.pid) });
    ADM.picked.clear();
    const verb = end ? 'Ended' : 'Cancelled the query in';
    const failed = r.failed.map((f) => `${f.pid}: ${f.reason}`);
    toast(`${verb} ${fmt.plural(r.done.length, 'session')}.` + (failed.length ? ` Not done for ${failed.length}: ${failed.slice(0, 3).join('; ')}${failed.length > 3 ? '…' : ''}` : ''),
      failed.length ? (r.done.length ? 'warn' : 'err') : 'ok');
  } catch (e) { toastError(e); }
  await load(true);
}

/* ---------------------------------------------------------------- roles & users */

async function admRoles(box, token) {
  const sid = state.route.sid;
  if (!ADM.roles || ADM.roles.sid !== sid) loadingView(box, 'Loading roles…');
  try {
    const d = await api('GET', `/api/servers/${enc(sid)}/admin/roles`);
    if (stale(token) || ADM.sub !== 'roles') return;
    ADM.roles = Object.assign(d, { sid });
  } catch (e) {
    if (!stale(token)) errorCard(box, e, () => admRoles(box, token));
    return;
  }
  admDrawRoles(box, token);
}

function admDrawRoles(box, token) {
  const d = ADM.roles, caps = d.capabilities;
  const reload = () => admRoles(box, token);
  const q = ADM.roleFilter.toLowerCase();
  const rows = d.roles.filter((r) => (ADM.builtin || !r.builtin) && (!q || r.name.toLowerCase().includes(q)
    || r.member_of.some((m) => m.name.toLowerCase().includes(q)) || (r.comment || '').toLowerCase().includes(q)));
  const own = d.roles.filter((r) => !r.builtin);
  const filter = el('input', { class: 'input', placeholder: 'Filter roles', value: ADM.roleFilter, style: 'max-width: 260px', 'aria-label': 'Filter roles' });
  filter.oninput = () => { ADM.roleFilter = filter.value; admDrawRoles(box, token); const f = box.querySelector('[aria-label="Filter roles"]'); f.focus(); f.setSelectionRange(f.value.length, f.value.length); };
  const builtin = el('label', { class: 'check' }, el('input', { type: 'checkbox', checked: ADM.builtin, onchange: (e) => { ADM.builtin = e.target.checked; admDrawRoles(box, token); } }), 'Built-in pg_ roles');
  const canCreate = caps.can_manage_roles;
  const why = canCreate ? null : 'This login needs CREATEROLE or superuser';
  const flagBadges = (r) => [r.superuser ? el('span', { class: 'badge accent' }, icon('shield'), 'superuser') : null,
    r.createdb ? el('span', { class: 'badge', text: 'create DB' }) : null, r.createrole ? el('span', { class: 'badge', text: 'create role' }) : null,
    r.replication ? el('span', { class: 'badge', text: 'replication' }) : null, r.bypassrls ? el('span', { class: 'badge', text: 'bypass RLS' }) : null,
    !r.inherit ? el('span', { class: 'badge warn', text: 'no inherit', title: "Does not get its roles' privileges automatically" }) : null];
  const expired = (r) => r.valid_until && new Date(r.valid_until) < new Date();
  const tr = (r) => el('tr', null,
    el('td', null, el('div', { class: 'row', style: 'gap: 6px' }, el('b', { text: r.name }), r.is_me ? el('span', { class: 'badge info', text: 'this login' }) : null),
      r.comment ? el('div', { class: 'subtle', text: r.comment }) : null),
    el('td', null, r.builtin ? el('span', { class: 'badge', text: 'built-in' }) : el('span', { class: 'badge ' + (r.login ? 'ok' : ''), text: r.login ? 'user' : 'role' })),
    el('td', null, el('div', { class: 'row', style: 'gap: 4px; flex-wrap: wrap' }, flagBadges(r))),
    el('td', null, el('div', { class: 'row', style: 'gap: 4px; flex-wrap: wrap' }, r.member_of.length ? r.member_of.map((m) =>
      el('span', { class: 'badge', title: m.admin ? 'With admin option: may grant it to others' : null, text: m.name + (m.admin ? ' ★' : '') })) : el('span', { class: 'subtle', text: '—' }))),
    el('td', { class: 'subtle', text: r.members.length ? fmt.plural(r.members.length, 'member') : '—', title: r.members.join(', ') || null }),
    el('td', { text: r.connlimit < 0 ? '—' : String(r.connlimit), class: r.connlimit < 0 ? 'subtle' : null }),
    el('td', null, r.valid_until ? el('span', { class: 'badge ' + (expired(r) ? 'danger' : ''), text: (expired(r) ? 'expired ' : '') + new Date(r.valid_until).toLocaleDateString() }) : el('span', { class: 'subtle', text: '—' })),
    el('td', { class: 'subtle', text: r.sessions ? String(r.sessions) : '—' }),
    el('td', { style: 'text-align: right; white-space: nowrap' },
      el('button', { class: 'btn sm', type: 'button', disabled: !r.can_manage, title: r.can_manage ? 'Edit, password, memberships' : r.builtin ? 'Built-in roles cannot be changed' : 'This login may not change this role',
        onclick: () => admRoleModal(r, reload) }, icon('edit'), 'Edit'),
      ' ',
      el('button', { class: 'btn sm', type: 'button', style: 'color: var(--danger)', disabled: !r.can_manage || r.is_me, title: r.is_me ? 'The login the app uses cannot drop itself' : null,
        onclick: () => admDropRole(r, reload) }, icon('trash'))));
  clear(box,
    el('div', { class: 'card', style: 'margin-bottom: 14px' }, el('div', { class: 'stats' },
      statBox('Users', fmt.num(own.filter((r) => r.login).length), 'roles that can log in'),
      statBox('Roles', fmt.num(own.filter((r) => !r.login).length), 'groups to grant'),
      statBox('Superusers', fmt.num(own.filter((r) => r.superuser).length), 'bypass every check'),
      statBox('Expired passwords', fmt.num(own.filter(expired).length), 'VALID UNTIL in the past'))),
    el('div', { class: 'card' },
      el('div', { class: 'toolbar' }, filter, builtin, el('div', { class: 'grow' }),
        el('button', { class: 'btn', type: 'button', disabled: !canCreate, title: why, onclick: () => admRoleModal(null, reload, false) }, icon('key'), 'New role'),
        el('button', { class: 'btn primary', type: 'button', disabled: !canCreate, title: why, onclick: () => admRoleModal(null, reload, true) }, icon('plus'), 'New user')),
      el('div', { class: 'table-wrap' }, el('table', { class: 't' },
        el('thead', null, el('tr', null, ['Name', 'Type', 'Attributes', 'Member of', 'Members', 'Conn. limit', 'Password valid until', 'Sessions', ''].map((h) => el('th', { text: h })))),
        el('tbody', null, rows.length ? rows.map(tr) : el('tr', null, el('td', { colspan: '9', class: 'subtle', style: 'text-align: center; padding: 28px', text: 'No roles match.' })))))));
}

function admGenPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.!@#%';
  const a = new Uint32Array(20);
  crypto.getRandomValues(a);
  return Array.from(a, (n) => chars[n % chars.length]).join('');
}

/** Create (r null; `asUser` picks the default type) or edit a role. Review the SQL, then run it. */
function admRoleModal(r, reload, asUser) {
  const editing = !!r;
  const caps = ADM.roles.capabilities;
  const v = r || { name: '', login: !!asUser, superuser: false, createdb: false, createrole: false, replication: false, bypassrls: false,
    inherit: true, connlimit: -1, valid_until: null, member_of: [], comment: '' };
  const f = {
    name: el('input', { class: 'input mono', value: v.name, autocomplete: 'off', spellcheck: 'false' }),
    pw: el('input', { class: 'input mono', type: 'password', autocomplete: 'new-password', placeholder: editing ? 'Leave empty to keep the current password' : '' }),
    pw2: el('input', { class: 'input mono', type: 'password', autocomplete: 'new-password', placeholder: 'Type it again' }),
    clearPw: el('input', { type: 'checkbox' }),
    connlimit: el('input', { class: 'input', inputmode: 'numeric', value: v.connlimit < 0 ? '' : String(v.connlimit), placeholder: 'No limit' }),
    until: el('input', { class: 'input', type: 'date', value: v.valid_until ? v.valid_until.slice(0, 10) : '' }),
    comment: el('input', { class: 'input', value: v.comment || '', placeholder: 'Optional note, e.g. who uses it' }),
  };
  let login = v.login;
  const typeSeg = el('div', { class: 'seg' });
  const pwBox = el('div');
  const drawType = () => {
    clear(typeSeg, [[true, 'User (can log in)'], [false, 'Role / group']].map(([k, label]) =>
      el('button', { type: 'button', class: login === k ? 'on' : '', onclick: () => { login = k; drawType(); } }, label)));
    pwBox.hidden = !login && !editing;
  };
  const flags = {};
  const flag = (key, label, hint, hidden) => {
    flags[key] = el('input', { type: 'checkbox', checked: !!v[key] });
    return hidden ? null : el('label', { class: 'check' }, flags[key], el('span', null, el('span', { class: 't', text: label }), el('span', { class: 'd', text: hint })));
  };
  const show = el('button', { class: 'btn sm', type: 'button', onclick: () => { const t = f.pw.type === 'password' ? 'text' : 'password'; f.pw.type = t; f.pw2.type = t; } }, 'Show');
  const gen = el('button', { class: 'btn sm', type: 'button', onclick: () => { const p = admGenPassword(); f.pw.value = p; f.pw2.value = p; f.pw.type = 'text'; f.pw2.type = 'text'; f.clearPw.checked = false; } }, icon('key'), 'Generate');
  clear(pwBox, el('div', { class: 'form-grid' },
    el('div', { class: 'field' }, el('label', null, 'Password'), el('div', { class: 'row' }, f.pw, show, gen)),
    el('div', { class: 'field' }, el('label', null, 'Confirm password'), f.pw2),
    el('div', { class: 'field span-2 hint', text: 'Sent to PostgreSQL only as a SCRAM-SHA-256 hash made on this PC. The app never saves it; copy a generated one now.' }),
    editing ? el('label', { class: 'check span-2' }, f.clearPw, el('span', null, el('span', { class: 't', text: 'Remove the password' }), el('span', { class: 'd', text: 'Password logins stop working for this role.' }))) : null));

  // membership picker: every other role; ticked = member, star = with admin option
  const memberMap = new Map(v.member_of.map((m) => [m.name, m.admin]));
  const others = ADM.roles.roles.filter((x) => x.name !== v.name);
  const memFilter = el('input', { class: 'input', placeholder: 'Find a role to grant', style: 'margin-bottom: 8px' });
  const memList = el('div', { style: 'display: grid; gap: 4px; max-height: 210px; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px' });
  const drawMembers = () => {
    const qq = memFilter.value.toLowerCase();
    const list = others.filter((x) => (!qq || x.name.toLowerCase().includes(qq)) && (ADM.builtin || !x.builtin || memberMap.has(x.name)));
    clear(memList, list.length ? list.map((x) => {
      const on = memberMap.has(x.name);
      const adminBox = el('input', { type: 'checkbox', checked: !!memberMap.get(x.name), disabled: !on, onchange: (e) => { memberMap.set(x.name, e.target.checked); } });
      return el('div', { class: 'row', style: 'gap: 10px' },
        el('label', { class: 'check grow' }, el('input', { type: 'checkbox', checked: on, onchange: (e) => { if (e.target.checked) memberMap.set(x.name, false); else memberMap.delete(x.name); drawMembers(); } }),
          el('span', null, x.name, ' ', el('span', { class: 'subtle', text: x.builtin ? 'built-in' : x.login ? 'user' : 'role' }))),
        el('label', { class: 'check subtle', title: 'With admin option: may grant this role to others' }, adminBox, 'admin'));
    }) : el('div', { class: 'subtle', text: 'No roles match. Tick "Built-in pg_ roles" in the list to see pg_read_all_data and the like.' }));
  };
  memFilter.oninput = drawMembers;
  drawMembers();

  const spec = () => {
    const s = {};
    const name = f.name.value;   // sent as typed: the server explains leading/trailing spaces
    if (!editing) s.name = name; else if (name !== r.name) s.new_name = name;
    if (!editing || login !== r.login) s.login = login;
    for (const k of Object.keys(flags)) if (!editing || flags[k].checked !== !!r[k]) s[k] = flags[k].checked;
    const limit = f.connlimit.value.trim() === '' ? -1 : f.connlimit.value.trim();
    if (!editing || String(limit) !== String(r.connlimit)) s.connlimit = limit;
    const until = f.until.value || null;
    if (!editing ? until : until !== (r.valid_until ? r.valid_until.slice(0, 10) : null)) s.valid_until = until;
    if (f.pw.value) s.password = f.pw.value; else if (editing && f.clearPw.checked) s.clear_password = true;
    const mem = [...memberMap].map(([name2, admin2]) => ({ name: name2, admin: admin2 }));
    const before = JSON.stringify([...(r ? r.member_of : [])].sort((a, b) => a.name.localeCompare(b.name)));
    if (!editing ? mem.length : JSON.stringify([...mem].sort((a, b) => a.name.localeCompare(b.name))) !== before) s.member_of = mem;
    const c = f.comment.value.trim();
    if (!editing ? c : c !== (r.comment || '')) s.comment = c;
    return s;
  };
  const review = el('button', { class: 'btn primary', type: 'button' }, editing ? 'Review changes' : (login ? 'Review & create user' : 'Review & create role'));
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
  drawType();
  const m = modal({ title: editing ? `Edit ${r.name}` : 'New user or role', iconName: 'key', wide: true,
    sub: editing ? (r.login ? 'User (can log in)' : 'Role / group') : 'A user is a role that can log in; a role groups privileges you grant to users',
    body: [
      el('div', { class: 'form-grid' },
        el('div', { class: 'field' }, el('label', null, 'Name'), f.name),
        el('div', { class: 'field' }, el('label', null, 'Type'), typeSeg)),
      pwBox,
      el('div', { class: 'form-grid' },
        el('div', { class: 'field' }, el('label', null, 'Connection limit'), f.connlimit),
        el('div', { class: 'field' }, el('label', null, 'Password valid until'), f.until, el('div', { class: 'hint', text: 'Empty = never expires.' })),
        el('div', { class: 'field span-2' }, el('label', null, 'Attributes'),
          el('div', { style: 'display: grid; grid-template-columns: 1fr 1fr; gap: 8px' },
            flag('createdb', 'Create databases', 'CREATEDB'), flag('createrole', 'Create roles', 'CREATEROLE: can create and manage other roles'),
            flag('inherit', 'Inherit privileges', "Gets its roles' privileges automatically (usual)"), flag('replication', 'Replication', 'For streaming replication / backup tools'),
            flag('bypassrls', 'Bypass row-level security', 'BYPASSRLS'), flag('superuser', 'Superuser', 'Bypasses every permission check', !caps.superuser))),
        el('div', { class: 'field span-2' }, el('label', null, 'Member of (roles granted to it)'), memFilter, memList),
        el('div', { class: 'field span-2' }, el('label', null, 'Comment'), f.comment)),
    ],
    actions: [cancel, review] });
  cancel.onclick = () => m.close();
  review.onclick = async () => {
    m.error('');
    if (!f.name.value) { m.error('Enter a name.'); return; }
    if (f.pw.value !== f.pw2.value) { m.error('The two passwords are different.'); return; }
    if (!editing && login && !f.pw.value && !(await confirmBox({ title: 'Create a user without a password?', confirmLabel: 'Create without password',
      message: 'It cannot log in with a password until one is set (other methods, e.g. peer or certificate, may still work).' }))) return;
    const s = spec();
    if (editing && !Object.keys(s).length) { m.error('Nothing has changed.'); return; }
    busy(review, true, 'Preparing…');
    let sqlText;
    try {
      sqlText = (await api('POST', `/api/servers/${enc(state.route.sid)}/admin/roles/preview`, editing ? { spec: s, name_existing: r.name } : { spec: s })).sql;
    } catch (e) { busy(review, false); m.error(e.message); return; }
    busy(review, false);
    const ok = await confirmBox({ title: editing ? `Apply changes to ${r.name}?` : `Create ${s.name}?`, confirmLabel: editing ? 'Apply' : 'Create',
      message: 'This SQL runs in one transaction: all of it or none.',
      extra: el('pre', { class: 'mono', style: 'white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow: auto; margin: 10px 0 0; padding: 12px; background: var(--surface-2); border-radius: var(--radius-sm); font-size: 12px', text: sqlText.join('\n') }) });
    if (!ok) return;
    busy(review, true, editing ? 'Applying…' : 'Creating…');
    try {
      if (editing) await api('PATCH', `/api/servers/${enc(state.route.sid)}/admin/roles/${enc(r.name)}`, s);
      else await api('POST', `/api/servers/${enc(state.route.sid)}/admin/roles`, s);
      m.close();
      toast(editing ? `${s.new_name || r.name} updated.` : `${s.name} created.`, 'ok');
      reload();
    } catch (e) { busy(review, false); m.error(e.message); }
  };
}

async function admDropRole(r, reload) {
  const yes = await confirmBox({ title: `Drop ${r.login ? 'user' : 'role'} ${r.name}?`, danger: true, confirmLabel: 'Drop role', typed: r.name,
    message: (r.sessions ? `It has ${fmt.plural(r.sessions, 'open session')}; they keep running until they disconnect. ` : '') +
      'PostgreSQL refuses if it still owns objects or has privileges in any database; the message then says where.' });
  if (!yes) return;
  try {
    await api('DELETE', `/api/servers/${enc(state.route.sid)}/admin/roles/${enc(r.name)}`, { confirm: r.name });
    toast(`${r.name} dropped.`, 'ok');
    reload();
  } catch (e) { toastError(e, 'Not dropped'); }
}
