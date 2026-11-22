/* PG Backup Studio: the per-server Admin tab (decisions 011, 015) — Users, Groups, Access (who may read, write,
 * delete or run what in a database, picked from dropdowns) and Sessions (find stale ones, cancel / end). Uses the
 * page's helpers (el, clear, icon, api, modal, busy, toast, toastError, confirmBox, errorCard, loadingView, stale,
 * statBox, fmt, server, state, enc, store, routeToken, menu) inside functions only. Text is always set through text
 * nodes. What is offered follows the login's rights (/admin/capabilities); the server is the final judge and its
 * refusals are shown as they come.
 */
'use strict';

const ADM = { sub: 'users', filter: '', stateFilter: 'all', workers: false, staleOnly: false, auto: 0, picked: new Set(),
  roleFilter: '', builtin: false, timer: null, data: null, roles: null,
  accRole: null, accDb: null, accFilter: '', acc: null, accCur: null, accOpen: new Set(), accCustom: new Set(), dbs: null };
const ADM_STATES = [['all', 'All states'], ['active', 'Active'], ['idle', 'Idle'], ['idle in transaction', 'Idle in transaction'],
  ['idle in transaction (aborted)', 'Idle in transaction (aborted)'], ['fastpath function call', 'Fastpath'], ['disabled', 'Disabled']];
const ADM_SUBS = [['users', 'Users', 'user'], ['groups', 'Groups', 'users'], ['access', 'Access', 'lock'], ['sessions', 'Sessions', 'activity']];

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

/** Pending access changes not applied yet? Ask before they are thrown away. */
async function admLeaveAccess() {
  if (!ADM.acc || !admAccChanges().length) return true;
  return confirmBox({ title: 'Discard the access changes?', danger: true, confirmLabel: 'Discard',
    message: `${fmt.plural(admAccChanges().length, 'change')} for ${ADM.acc.role.name} in ${ADM.acc.database.name} are not applied yet.` });
}

async function viewAdmin(view, token) {
  admStopTimer();
  ADM.picked.clear();
  const sid = state.route.sid;
  if (ADM.sid !== sid) { Object.assign(ADM, { sid, roles: null, data: null, acc: null, accCur: null, accRole: null, accDb: null, dbs: null }); }
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
  const seg = el('div', { class: 'adm-subs', role: 'tablist' });
  const drawSeg = () => clear(seg, ADM_SUBS.map(([k, label, ic]) => {
    const n = ADM.roles && ADM.roles.sid === sid ? (k === 'users' ? admUsersOf().length : k === 'groups' ? admGroupsOf().length : null) : null;
    return el('button', { type: 'button', role: 'tab', 'aria-selected': ADM.sub === k ? 'true' : 'false', class: ADM.sub === k ? 'on' : '',
      onclick: async () => { if (ADM.sub === k) return; if (ADM.sub === 'access' && !(await admLeaveAccess())) return; ADM.sub = k; drawSeg(); show(); } },
    icon(ic), label, n !== null ? el('span', { class: 'n', text: String(n) }) : null);
  }));
  ADM.drawSeg = drawSeg;
  ADM.go = (sub) => { ADM.sub = sub; drawSeg(); show(); };
  const rights = [caps.superuser ? 'superuser' : null, !caps.superuser && caps.createrole ? 'can create users' : null,
    !caps.superuser && caps.signal_backend ? "can end other users' sessions" : null].filter(Boolean);
  const note = caps.can_manage_roles || caps.can_end_others ? null : el('div', { class: 'alert info', style: 'margin-bottom: 14px' }, icon('info'),
    el('div', { class: 'body' }, el('b', { text: 'Read-only for this login' }),
      `${caps.user} is not a superuser and cannot create roles, so it can look but not change users, groups or other people's sessions. Save the server with an admin login to manage them.`));
  clear(view, el('div', { class: 'row', style: 'margin-bottom: 16px; flex-wrap: wrap; gap: 12px' }, seg, el('div', { class: 'grow' }),
    el('span', { class: 'subtle' }, 'Signed in as ', el('b', { class: 'mono', text: caps.user }), rights.length ? ` · ${rights.join(' · ')}` : ' · regular role')),
  note, body);
  const show = () => {
    admStopTimer();
    ({ users: admUsers, groups: admGroups, access: admAccess, sessions: admSessions })[ADM.sub](body, token);
  };
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

/* ---------------------------------------------------------------- users & groups */

/** Users = roles that can log in; groups = roles that cannot (privileges are granted to them, users join them). */
function admUsersOf() { return (ADM.roles ? ADM.roles.roles : []).filter((r) => !r.builtin && r.login); }
function admGroupsOf() { return (ADM.roles ? ADM.roles.roles : []).filter((r) => !r.builtin && !r.login); }
function admRole(name) { return (ADM.roles ? ADM.roles.roles : []).find((r) => r.name === name) || null; }

function admAvatar(r, size) {
  const group = r && !r.login;
  return el('span', { class: 'avatar' + (group ? ' group' : '') + (size ? ' ' + size : ''), 'aria-hidden': 'true' },
    group ? icon('users') : (r ? r.name : '?').replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2) || '?');
}

async function admLoadRoles(box, token, sub) {
  const sid = state.route.sid;
  if (!ADM.roles || ADM.roles.sid !== sid) loadingView(box, 'Loading users and groups…');
  try {
    const d = await api('GET', `/api/servers/${enc(sid)}/admin/roles`);
    if (stale(token) || (sub && ADM.sub !== sub)) return false;
    ADM.roles = Object.assign(d, { sid });
    if (ADM.drawSeg) ADM.drawSeg();
    return true;
  } catch (e) {
    if (!stale(token)) errorCard(box, e, () => admLoadRoles(box, token, sub));
    return false;
  }
}

async function admUsers(box, token) { if (await admLoadRoles(box, token, 'users')) admDrawPeople(box, token, 'users'); }
async function admGroups(box, token) { if (await admLoadRoles(box, token, 'groups')) admDrawPeople(box, token, 'groups'); }

function admOpenAccess(name) {
  ADM.accRole = name;
  ADM.acc = null; ADM.accCur = null;
  ADM.go('access');
}

function admDrawPeople(box, token, kind) {
  const users = kind === 'users';
  const caps = ADM.roles.capabilities;
  const reload = () => (users ? admUsers : admGroups)(box, token);
  const all = users ? admUsersOf() : admGroupsOf();
  const q = ADM.roleFilter.toLowerCase();
  const rows = all.filter((r) => !q || r.name.toLowerCase().includes(q) || (r.comment || '').toLowerCase().includes(q)
    || r.member_of.some((m) => m.name.toLowerCase().includes(q)) || r.members.some((m) => m.toLowerCase().includes(q)));
  const canCreate = caps.can_manage_roles;
  const why = canCreate ? null : 'This login needs CREATEROLE or superuser';
  const expired = (r) => r.valid_until && new Date(r.valid_until) < new Date();

  const search = el('input', { class: 'input', placeholder: users ? 'Search users or groups they are in' : 'Search groups or members', value: ADM.roleFilter, 'aria-label': 'Search' });
  search.oninput = () => { ADM.roleFilter = search.value; admDrawPeople(box, token, kind); const f = box.querySelector('[aria-label="Search"]'); f.focus(); f.setSelectionRange(f.value.length, f.value.length); };
  const addBtn = el('button', { class: 'btn primary', type: 'button', disabled: !canCreate, title: why,
    onclick: () => (users ? admUserModal : admGroupModal)(null, reload) }, icon('plus'), users ? 'New user' : 'New group');

  const chips = (names, max, cls) => {
    if (!names.length) return el('span', { class: 'subtle', text: '—' });
    return el('div', { class: 'row', style: 'gap: 4px; flex-wrap: wrap' }, names.slice(0, max).map((n) => el('span', { class: 'badge ' + (cls || ''), text: n })),
      names.length > max ? el('span', { class: 'badge', title: names.slice(max).join(', '), text: `+${names.length - max}` }) : null);
  };
  const actions = (r) => el('div', { class: 'actions' },
    el('button', { class: 'btn sm', type: 'button', title: `What ${r.name} may read, change or run`, onclick: () => admOpenAccess(r.name) }, icon('lock'), 'Access'),
    el('button', { class: 'btn sm', type: 'button', disabled: !r.can_manage, title: r.can_manage ? null : 'This login may not change it',
      onclick: () => (r.login ? admUserModal : admGroupModal)(r, reload) }, icon('edit'), 'Edit'),
    el('button', { class: 'btn sm ghost icon-only', type: 'button', 'aria-label': 'More', onclick: (e) => menu(e.currentTarget, [
      { icon: 'trash', label: users ? 'Delete user' : 'Delete group', danger: true, disabled: !r.can_manage || r.is_me,
        title: r.is_me ? 'The login the app uses cannot delete itself' : null, action: () => admDropRole(r, reload) }]) }, icon('more')));
  const status = (r) => r.superuser ? el('span', { class: 'badge accent' }, icon('shield'), 'Superuser')
    : expired(r) ? el('span', { class: 'badge danger', text: 'Password expired' })
      : el('span', { class: 'badge ok', text: 'Active' });

  let table;
  if (users) {
    table = el('table', { class: 't' },
      el('thead', null, el('tr', null, ['User', 'Groups', 'Status', 'Password expires', 'Sessions', ''].map((h) => el('th', { text: h })))),
      el('tbody', null, rows.map((r) => el('tr', null,
        el('td', null, el('div', { class: 'person' }, admAvatar(r),
          el('div', { style: 'min-width: 0' }, el('div', { class: 'nm' }, r.name, r.is_me ? el('span', { class: 'badge info', style: 'margin-left: 6px', text: 'you' }) : null),
            r.comment ? el('div', { class: 'sub', text: r.comment }) : null))),
        el('td', null, chips(r.member_of.map((m) => m.name), 4, 'ok')),
        el('td', null, status(r)),
        el('td', { class: r.valid_until ? null : 'subtle', text: r.valid_until ? new Date(r.valid_until).toLocaleDateString() : 'Never' }),
        el('td', { class: 'subtle', text: r.sessions ? String(r.sessions) : '—' }),
        el('td', null, actions(r))))));
  } else {
    table = el('table', { class: 't' },
      el('thead', null, el('tr', null, ['Group', 'Members', 'Inside groups', ''].map((h) => el('th', { text: h })))),
      el('tbody', null, rows.map((r) => el('tr', null,
        el('td', null, el('div', { class: 'person' }, admAvatar(r),
          el('div', { style: 'min-width: 0' }, el('div', { class: 'nm', text: r.name }),
            el('div', { class: 'sub', text: r.comment || fmt.plural(r.members.length, 'member') })))),
        el('td', null, chips(r.members, 6)),
        el('td', null, chips(r.member_of.map((m) => m.name), 3, 'ok')),
        el('td', null, actions(r))))));
  }
  const empty = el('div', { class: 'empty' }, el('div', { class: 'ic' }, icon(users ? 'user' : 'users')),
    el('h3', { text: q ? 'Nothing matches' : users ? 'No users yet' : 'No groups yet' }),
    el('p', { text: q ? 'Try another search.' : users ? 'A user is someone (or an application) that logs in to this server.'
      : 'A group bundles access: give the group read or write access once, then add users to it.' }),
    q ? null : el('button', { class: 'btn primary', type: 'button', disabled: !canCreate, title: why, onclick: () => (users ? admUserModal : admGroupModal)(null, reload) },
      icon('plus'), users ? 'Create the first user' : 'Create the first group'));

  clear(box,
    users || all.length ? null : el('div', { class: 'alert info', style: 'margin-bottom: 14px' }, icon('info'), el('div', { class: 'body' },
      el('b', { text: 'Tip: give access to groups, not to each user' }),
      'For example make a group "readers" with read access to the database, then add users to it. When someone leaves, remove them from the group.')),
    el('div', { class: 'card' },
      el('div', { class: 'toolbar' }, el('div', { class: 'search' }, icon('search'), search), el('div', { class: 'grow' }),
        el('span', { class: 'subtle', text: fmt.plural(all.length, users ? 'user' : 'group') }), addBtn),
      rows.length ? el('div', { class: 'table-wrap' }, table) : empty));
}

function admGenPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.!@#%';
  const a = new Uint32Array(20);
  crypto.getRandomValues(a);
  return Array.from(a, (n) => chars[n % chars.length]).join('');
}

/** A row of toggle chips; `set` (a Set or Map) is changed in place. */
function admChipPicker(items, has, toggle, emptyText) {
  const box = el('div', { class: 'chips' });
  const draw = () => clear(box, items.length ? items.map((it) => {
    const on = has(it.value);
    return el('button', { type: 'button', class: 'chip' + (on ? ' on' : ''), title: it.title || null, 'aria-pressed': on ? 'true' : 'false',
      onclick: () => { toggle(it.value, !on); draw(); } }, on ? icon('check') : null, it.label);
  }) : el('span', { class: 'subtle', text: emptyText || 'Nothing to choose.' }));
  draw();
  box.redraw = draw;
  return box;
}

function admFlagBoxes(v, flags, superOk) {
  const flag = (key, label, hint) => {
    flags[key] = el('input', { type: 'checkbox', checked: !!v[key] });
    return el('label', { class: 'check' }, flags[key], el('span', null, el('span', { class: 't', text: label }), el('span', { class: 'd', text: hint })));
  };
  return el('div', { style: 'display: grid; grid-template-columns: 1fr 1fr; gap: 10px' },
    flag('createdb', 'Can create databases', 'CREATEDB'),
    flag('createrole', 'Can create users and groups', 'CREATEROLE'),
    flag('inherit', 'Gets its groups’ access automatically', 'INHERIT (usual; off = must SET ROLE)'),
    flag('replication', 'Replication', 'For streaming replication and backup tools'),
    flag('bypassrls', 'Bypass row-level security', 'BYPASSRLS'),
    superOk ? flag('superuser', 'Superuser', 'Can do everything; skips every permission check') : null);
}

const ADM_BUILTIN_HINTS = { pg_read_all_data: 'Read every table everywhere', pg_write_all_data: 'Change every table everywhere',
  pg_monitor: 'See monitoring views', pg_signal_backend: 'Cancel or end other sessions', pg_read_all_stats: 'See all statistics',
  pg_database_owner: 'Owner of the current database', pg_create_subscription: 'Create subscriptions', pg_maintain: 'VACUUM, ANALYZE, REINDEX everything' };

/** Create (r null) or edit a user: name, password, groups; the rest under "More options". */
function admUserModal(r, reload) {
  const editing = !!r;
  const caps = ADM.roles.capabilities;
  const v = r || { name: '', login: true, superuser: false, createdb: false, createrole: false, replication: false, bypassrls: false,
    inherit: true, connlimit: -1, valid_until: null, member_of: [], comment: '' };
  const f = {
    name: el('input', { class: 'input mono', value: v.name, autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. alice or report_app' }),
    pw: el('input', { class: 'input mono', type: 'password', autocomplete: 'new-password', placeholder: editing ? 'Leave empty to keep the current one' : 'Type or generate a password' }),
    pw2: el('input', { class: 'input mono', type: 'password', autocomplete: 'new-password', placeholder: 'Type it again' }),
    clearPw: el('input', { type: 'checkbox' }),
    login: el('input', { type: 'checkbox', checked: v.login }),
    connlimit: el('input', { class: 'input', inputmode: 'numeric', value: v.connlimit < 0 ? '' : String(v.connlimit), placeholder: 'No limit' }),
    until: el('input', { class: 'input', type: 'date', value: v.valid_until ? v.valid_until.slice(0, 10) : '' }),
    comment: el('input', { class: 'input', value: v.comment || '', placeholder: 'Optional, e.g. "Finance team" or "Nightly report job"' }),
  };
  const memberMap = new Map(v.member_of.map((m) => [m.name, m.admin]));
  const toggle = (name, on) => { if (on) memberMap.set(name, memberMap.get(name) || false); else memberMap.delete(name); };
  const groups = admGroupsOf().filter((g) => g.name !== v.name);
  const groupPicker = admChipPicker(groups.map((g) => ({ value: g.name, label: g.name, title: g.comment || fmt.plural(g.members.length, 'member') })),
    (n) => memberMap.has(n), toggle, 'No groups yet. Create one on the Groups tab to share access between users.');
  const builtins = ADM.roles.roles.filter((x) => x.builtin && (ADM_BUILTIN_HINTS[x.name] || memberMap.has(x.name)));
  const builtinPicker = admChipPicker(builtins.map((x) => ({ value: x.name, label: x.name, title: ADM_BUILTIN_HINTS[x.name] || null })),
    (n) => memberMap.has(n), toggle);
  const flags = {};
  const show = el('button', { class: 'btn sm', type: 'button', onclick: () => { const t = f.pw.type === 'password' ? 'text' : 'password'; f.pw.type = t; f.pw2.type = t; } }, 'Show');
  const gen = el('button', { class: 'btn sm', type: 'button', onclick: () => { const p = admGenPassword(); f.pw.value = p; f.pw2.value = p; f.pw.type = 'text'; f.pw2.type = 'text'; f.clearPw.checked = false; } }, icon('key'), 'Generate');
  const copy = el('button', { class: 'btn sm', type: 'button', title: 'Copy the password', onclick: async () => {
    if (!f.pw.value) return; try { await navigator.clipboard.writeText(f.pw.value); toast('Password copied.', 'ok'); } catch (e) { toastError(e); } } }, icon('copy'));

  const spec = () => {
    const s = {};
    if (!editing) s.name = f.name.value; else if (f.name.value !== r.name) s.new_name = f.name.value;
    if (!editing || f.login.checked !== r.login) s.login = f.login.checked;
    for (const k of Object.keys(flags)) if (!editing || flags[k].checked !== !!r[k]) s[k] = flags[k].checked;
    const limit = f.connlimit.value.trim() === '' ? -1 : f.connlimit.value.trim();
    if (!editing || String(limit) !== String(r.connlimit)) s.connlimit = limit;
    const until = f.until.value || null;
    if (!editing ? until : until !== (r.valid_until ? r.valid_until.slice(0, 10) : null)) s.valid_until = until;
    if (f.pw.value) s.password = f.pw.value; else if (editing && f.clearPw.checked) s.clear_password = true;
    const mem = [...memberMap].map(([name, admin]) => ({ name, admin }));
    const key = (l) => JSON.stringify([...l].sort((a, b) => a.name.localeCompare(b.name)));
    if (!editing ? mem.length : key(mem) !== key(r.member_of)) s.member_of = mem;
    const c = f.comment.value.trim();
    if (!editing ? c : c !== (r.comment || '')) s.comment = c;
    return s;
  };
  const save = el('button', { class: 'btn primary', type: 'button' }, editing ? 'Save changes' : 'Create user');
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
  const m = modal({ title: editing ? `Edit user ${r.name}` : 'New user', iconName: 'user', wide: true,
    sub: editing ? 'Change the password, groups or options' : 'Someone (or an application) that logs in to this server',
    body: [
      el('div', { class: 'form-grid' },
        el('div', { class: 'field span-2' }, el('label', null, 'User name'), f.name),
        el('div', { class: 'field' }, el('label', null, 'Password'), el('div', { class: 'row', style: 'gap: 6px' }, f.pw, show, gen, copy)),
        el('div', { class: 'field' }, el('label', null, 'Confirm password'), f.pw2),
        el('div', { class: 'field span-2 hint', text: 'Sent to PostgreSQL only as a SCRAM-SHA-256 hash made on this PC; the app never saves it. Copy a generated password now.' }),
        el('div', { class: 'field span-2' }, el('label', null, 'Groups'), groupPicker,
          el('div', { class: 'hint', text: 'The user gets all the access of every group it is in.' }))),
      el('details', { class: 'adv', style: 'margin-top: 16px' }, el('summary', null, icon('chevron'), 'More options'),
        el('div', { class: 'form-grid' },
          el('div', { class: 'field' }, el('label', null, 'Password expires on'), f.until, el('div', { class: 'hint', text: 'Empty = never.' })),
          el('div', { class: 'field' }, el('label', null, 'Max. connections at once'), f.connlimit),
          el('label', { class: 'check span-2' }, f.login, el('span', null, el('span', { class: 't', text: 'Can log in' }),
            el('span', { class: 'd', text: 'Untick to block logins; it then shows under Groups.' }))),
          editing ? el('label', { class: 'check span-2' }, f.clearPw, el('span', null, el('span', { class: 't', text: 'Remove the password' }),
            el('span', { class: 'd', text: 'Password logins stop working.' }))) : null,
          el('div', { class: 'field span-2' }, el('label', null, 'Special permissions'), admFlagBoxes(v, flags, caps.superuser)),
          builtins.length ? el('div', { class: 'field span-2' }, el('label', null, 'Built-in PostgreSQL roles'), builtinPicker,
            el('div', { class: 'hint', text: 'Server-wide shortcuts, e.g. pg_read_all_data = read every table in every database.' })) : null,
          el('div', { class: 'field span-2' }, el('label', null, 'Note'), f.comment))),
    ],
    actions: [cancel, save] });
  cancel.onclick = () => m.close();
  save.onclick = async () => {
    m.error('');
    if (!f.name.value) { m.error('Enter a user name.'); return; }
    if (f.pw.value !== f.pw2.value) { m.error('The two passwords are different.'); return; }
    if (!editing && !f.pw.value && !(await confirmBox({ title: 'Create the user without a password?', confirmLabel: 'Create without password',
      message: 'It cannot log in with a password until one is set.' }))) return;
    const s = spec();
    if (editing && !Object.keys(s).length) { m.error('Nothing has changed.'); return; }
    await admSaveRole({ m, btn: save, s, r, reload, kind: 'user' });
  };
}

/** Create (r null) or edit a group: name, description, members. */
function admGroupModal(r, reload) {
  const editing = !!r;
  const caps = ADM.roles.capabilities;
  const v = r || { name: '', login: false, superuser: false, createdb: false, createrole: false, replication: false, bypassrls: false,
    inherit: true, connlimit: -1, valid_until: null, member_of: [], members: [], comment: '' };
  const f = {
    name: el('input', { class: 'input mono', value: v.name, autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. readers, editors, finance' }),
    comment: el('input', { class: 'input', value: v.comment || '', placeholder: 'What is it for? e.g. "Read-only access for analysts"' }),
    find: el('input', { class: 'input', placeholder: 'Find a user', style: 'margin-bottom: 8px; max-width: 260px' }),
  };
  const members = new Set(v.members);
  const memberOf = new Map(v.member_of.map((x) => [x.name, x.admin]));
  const users = admUsersOf().filter((u) => u.name !== v.name);
  const otherGroups = admGroupsOf().filter((g) => g.name !== v.name);
  const memberItems = () => {
    const qq = f.find.value.toLowerCase();
    return [...users, ...otherGroups.filter((g) => members.has(g.name))].filter((x) => !qq || x.name.toLowerCase().includes(qq))
      .map((x) => ({ value: x.name, label: x.login ? x.name : `${x.name} (group)` }));
  };
  const memberBox = el('div');
  const drawMembers = () => clear(memberBox, admChipPicker(memberItems(), (n) => members.has(n), (n, on) => { if (on) members.add(n); else members.delete(n); count(); },
    users.length ? 'No users match.' : 'No users yet.'));
  const countEl = el('span', { class: 'subtle' });
  const count = () => { countEl.textContent = members.size ? `${fmt.plural(members.size, 'member')} selected` : 'No members yet'; };
  f.find.oninput = drawMembers;
  drawMembers(); count();
  const parentPicker = admChipPicker(otherGroups.filter((g) => !members.has(g.name)).map((g) => ({ value: g.name, label: g.name })),
    (n) => memberOf.has(n), (n, on) => { if (on) memberOf.set(n, memberOf.get(n) || false); else memberOf.delete(n); }, 'No other groups.');
  const flags = {};
  const spec = () => {
    const s = {};
    if (!editing) { s.name = f.name.value; s.login = false; } else if (f.name.value !== r.name) s.new_name = f.name.value;
    for (const k of Object.keys(flags)) if (!editing ? flags[k].checked !== (k === 'inherit') : flags[k].checked !== !!r[k]) s[k] = flags[k].checked;
    const mem = [...members].sort();
    if (!editing ? mem.length : JSON.stringify(mem) !== JSON.stringify([...r.members].sort())) s.members = mem;
    const parents = [...memberOf].map(([name, admin]) => ({ name, admin }));
    const key = (l) => JSON.stringify([...l].sort((a, b) => a.name.localeCompare(b.name)));
    if (!editing ? parents.length : key(parents) !== key(r.member_of)) s.member_of = parents;
    const c = f.comment.value.trim();
    if (!editing ? c : c !== (r.comment || '')) s.comment = c;
    return s;
  };
  const save = el('button', { class: 'btn primary', type: 'button' }, editing ? 'Save changes' : 'Create group');
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
  const m = modal({ title: editing ? `Edit group ${r.name}` : 'New group', iconName: 'users', wide: true,
    sub: 'Give access to the group once; every member gets it',
    body: [
      el('div', { class: 'form-grid' },
        el('div', { class: 'field' }, el('label', null, 'Group name'), f.name),
        el('div', { class: 'field' }, el('label', null, 'Description'), f.comment),
        el('div', { class: 'field span-2' }, el('label', { class: 'row', style: 'justify-content: space-between' }, 'Members', countEl),
          users.length > 12 ? f.find : null, memberBox)),
      el('details', { class: 'adv', style: 'margin-top: 16px' }, el('summary', null, icon('chevron'), 'More options'),
        el('div', { class: 'form-grid' },
          el('div', { class: 'field span-2' }, el('label', null, 'Put this group inside other groups'), parentPicker,
            el('div', { class: 'hint', text: 'Members then also get the access of those groups.' })),
          el('div', { class: 'field span-2' }, el('label', null, 'Special permissions'), admFlagBoxes(v, flags, caps.superuser)))),
    ],
    actions: [cancel, save] });
  cancel.onclick = () => m.close();
  save.onclick = async () => {
    m.error('');
    if (!f.name.value) { m.error('Enter a group name.'); return; }
    const s = spec();
    if (editing && !Object.keys(s).length) { m.error('Nothing has changed.'); return; }
    await admSaveRole({ m, btn: save, s, r, reload, kind: 'group' });
  };
}

/** Plain-language lines for a user / group change. */
function admRoleSummary(s, r, kind) {
  const out = [];
  const name = s.name || (r && r.name);
  if (!r) out.push(`Create ${kind} ${name}`);
  if (s.new_name) out.push(`Rename ${r.name} to ${s.new_name}`);
  if (s.password) out.push(r ? 'Set a new password' : 'Set its password');
  if (s.clear_password) out.push('Remove the password');
  if ('login' in s && r) out.push(s.login ? 'Allow logging in' : 'Block logging in');
  const before = new Set(r ? r.member_of.map((x) => x.name) : []);
  if (s.member_of) {
    const now = new Set(s.member_of.map((x) => x.name));
    const add = [...now].filter((x) => !before.has(x)), rem = [...before].filter((x) => !now.has(x));
    if (add.length) out.push(`Add to ${add.join(', ')}`);
    if (rem.length) out.push(`Remove from ${rem.join(', ')}`);
  }
  if (s.members) {
    const was = new Set(r ? r.members : []), now = new Set(s.members);
    const add = [...now].filter((x) => !was.has(x)), rem = [...was].filter((x) => !now.has(x));
    if (add.length) out.push(`Add members: ${add.join(', ')}`);
    if (rem.length) out.push(`Remove members: ${rem.join(', ')}`);
  }
  const labels = { createdb: 'create databases', createrole: 'create users and groups', inherit: 'get group access automatically',
    replication: 'replication', bypassrls: 'bypass row-level security', superuser: 'superuser' };
  for (const k of Object.keys(labels)) if (k in s && (r || s[k] !== (k === 'inherit'))) out.push(`${s[k] ? 'Allow' : 'No longer allow'}: ${labels[k]}`);
  if ('valid_until' in s && (r || s.valid_until)) out.push(s.valid_until ? `Password expires on ${s.valid_until}` : 'Password never expires');
  if ('connlimit' in s && (r || String(s.connlimit) !== '-1')) out.push(String(s.connlimit) === '-1' ? 'No connection limit' : `At most ${s.connlimit} connections`);
  if ('comment' in s) out.push(s.comment ? `Note: ${s.comment}` : 'Remove the note');
  return out;
}

/** Preview the SQL, confirm in plain words (SQL under "Show SQL"), run it; after creating, offer to set access. */
async function admSaveRole({ m, btn, s, r, reload, kind }) {
  const editing = !!r;
  busy(btn, true, 'Checking…');
  let sqlText;
  try {
    sqlText = (await api('POST', `/api/servers/${enc(state.route.sid)}/admin/roles/preview`, editing ? { spec: s, name_existing: r.name } : { spec: s })).sql;
  } catch (e) { busy(btn, false); m.error(e.message); return; }
  busy(btn, false);
  const lines = admRoleSummary(s, r, kind);
  const ok = await confirmBox({ title: editing ? `Save changes to ${r.name}?` : `Create ${kind} ${s.name}?`, confirmLabel: editing ? 'Save' : `Create ${kind}`,
    message: 'This will:',
    extra: [el('ul', { class: 'plain-list' }, lines.map((l) => el('li', { text: l }))),
      el('details', { class: 'adv', style: 'margin-top: 12px' }, el('summary', null, icon('chevron'), 'Show SQL'),
        el('pre', { class: 'mono', style: 'white-space: pre-wrap; word-break: break-word; max-height: 32vh; overflow: auto; margin: 0; padding: 12px; background: var(--surface-2); border-radius: var(--radius-sm); font-size: 12px', text: sqlText.join('\n') }))] });
  if (!ok) return;
  busy(btn, true, editing ? 'Saving…' : 'Creating…');
  try {
    if (editing) await api('PATCH', `/api/servers/${enc(state.route.sid)}/admin/roles/${enc(r.name)}`, s);
    else await api('POST', `/api/servers/${enc(state.route.sid)}/admin/roles`, s);
  } catch (e) { busy(btn, false); m.error(e.message); return; }
  m.close();
  toast(editing ? `${s.new_name || r.name} saved.` : `${kind === 'user' ? 'User' : 'Group'} ${s.name} created.`, 'ok');
  if (!editing && (kind === 'group' || !(s.member_of || []).length)) {
    const go = await confirmBox({ title: `Give ${s.name} access now?`, confirmLabel: 'Choose access',
      message: kind === 'group' ? 'Pick which databases and tables the group may read, change or delete. Its members get the same.'
        : 'It is not in any group, so it cannot read any table yet. Pick what it may read, change or delete, or add it to a group later.' });
    if (go) { ADM.roles = null; admOpenAccess(s.name); return; }
  }
  reload();
}

async function admDropRole(r, reload) {
  const what = r.login ? 'user' : 'group';
  const yes = await confirmBox({ title: `Delete ${what} ${r.name}?`, danger: true, confirmLabel: `Delete ${what}`, typed: r.name,
    message: (r.sessions ? `It has ${fmt.plural(r.sessions, 'open session')}; they keep running until they disconnect. ` : '') +
      (r.members.length ? `Its ${fmt.plural(r.members.length, 'member')} lose the access it gave them. ` : '') +
      'PostgreSQL refuses if it still owns tables or has access in a database; the message then says where.' });
  if (!yes) return;
  try {
    await api('DELETE', `/api/servers/${enc(state.route.sid)}/admin/roles/${enc(r.name)}`, { confirm: r.name });
    toast(`${r.name} deleted.`, 'ok');
    reload();
  } catch (e) { toastError(e, 'Not deleted'); }
}

/* ---------------------------------------------------------------- access (decision 015) */

const ADM_PRIV_LABEL = { SELECT: 'Read', INSERT: 'Add rows', UPDATE: 'Edit rows', DELETE: 'Delete rows', TRUNCATE: 'Empty table',
  REFERENCES: 'Foreign keys', TRIGGER: 'Triggers' };
const ADM_PRIVS = Object.keys(ADM_PRIV_LABEL);
const ADM_LEVELS = [
  ['none', 'No access', []],
  ['read', 'Read only', ['SELECT']],
  ['write', 'Read & write', ['SELECT', 'INSERT', 'UPDATE']],
  ['modify', 'Read, write & delete', ['SELECT', 'INSERT', 'UPDATE', 'DELETE']],
  ['all', 'Full control', ADM_PRIVS],
];
const ADM_LEVEL_HINT = { none: 'Cannot see the rows', read: 'SELECT', write: 'SELECT, INSERT, UPDATE', modify: 'SELECT, INSERT, UPDATE, DELETE',
  all: 'Everything incl. TRUNCATE, REFERENCES, TRIGGER' };

function admNorm(privs) { const s = new Set(privs); return ADM_PRIVS.filter((p) => s.has(p)); }
function admSame(a, b) { return admNorm(a).join() === admNorm(b).join(); }
function admLevelOf(privs) { const l = ADM_LEVELS.find(([, , p]) => admSame(p, privs)); return l ? l[0] : 'custom'; }
function admTKey(schema, name) { return schema + '\u0000' + name; }

/** The editable copy of what the role has now (direct grants). */
function admAccInit(d) {
  const cur = { db: [...d.database.direct], schema: {}, table: {}, bulk: {} };
  for (const s of d.schemas) {
    cur.schema[s.name] = { create: s.direct.includes('CREATE'), exec: s.routines ? (s.routines_direct === s.routines ? true : s.routines_direct === 0 ? false : null) : null,
      future: [...s.future] };
  }
  for (const t of d.tables) cur.table[admTKey(t.schema, t.name)] = [...t.direct];
  return cur;
}

/** The changes the UI sends: compared with what was read. */
function admAccChanges() {
  const d = ADM.acc, cur = ADM.accCur;
  if (!d || !cur) return [];
  const out = [];
  if (cur.db.join() !== d.database.direct.join()) out.push({ kind: 'database', privs: cur.db });
  const tablesBy = admTablesBy(d);
  for (const s of d.schemas) {
    const c = cur.schema[s.name], o = admAccInit({ database: d.database, schemas: [s], tables: [] }).schema[s.name];
    const bulk = cur.bulk[s.name];
    if (bulk) out.push({ kind: 'all_tables', schema: s.name, privs: bulk, label: `All tables in ${s.name}: ${admLevelLabel(bulk)}` });
    for (const t of tablesBy[s.name] || []) {
      if (t.is_owner) continue;
      const now = cur.table[admTKey(t.schema, t.name)];
      const base = bulk || t.direct;
      if (!admSame(now, base)) out.push({ kind: 'table', schema: s.name, name: t.name, privs: now, label: `${s.name}.${t.name}: ${admLevelLabel(now)}` });
    }
    if (c.exec !== null && c.exec !== o.exec) out.push({ kind: 'functions', schema: s.name, execute: c.exec, label: `${s.name}: ${c.exec ? 'may run' : 'may not run'} functions` });
    if (c.create !== o.create) out.push({ kind: 'schema_create', schema: s.name, create: c.create, label: `${s.name}: ${c.create ? 'may' : 'may not'} create tables` });
    if (!admSame(c.future, o.future)) out.push({ kind: 'future_tables', schema: s.name, privs: c.future,
      label: `${s.name}, tables created later: ${admLevelLabel(c.future)}` });
  }
  if (out.length && out[0].kind === 'database') out[0].label = `Database ${d.database.name}: ${cur.db.length ? cur.db.map(admDbLabel).join(', ') : 'no own rights'}`;
  return out;
}

function admLevelLabel(privs) {
  const l = ADM_LEVELS.find(([, , p]) => admSame(p, privs));
  return l ? l[1] : admNorm(privs).map((p) => ADM_PRIV_LABEL[p]).join(', ');
}
function admDbLabel(p) { return { CONNECT: 'connect', CREATE: 'create schemas', TEMPORARY: 'temporary tables' }[p] || p; }

function admTablesBy(d) {
  if (d._by) return d._by;
  const by = {};
  for (const t of d.tables) (by[t.schema] = by[t.schema] || []).push(t);
  d._by = by;
  return by;
}

/** The level <select>: preset levels, Custom…, and Mixed when the tables differ. */
function admLevelSelect({ privs, mixed, changed, disabled, onPick, label }) {
  const lv = mixed ? 'mixed' : admLevelOf(privs);
  const sel = el('select', { class: 'input level' + (changed ? ' changed' : ''), 'data-level': lv, disabled, 'aria-label': label },
    ADM_LEVELS.map(([k, text]) => el('option', { value: k, selected: lv === k, title: ADM_LEVEL_HINT[k] }, text)),
    el('option', { value: 'custom', selected: lv === 'custom' }, lv === 'custom' ? `Custom: ${admNorm(privs).map((p) => ADM_PRIV_LABEL[p]).join(', ')}` : 'Custom…'),
    mixed ? el('option', { value: 'mixed', selected: true, disabled: true }, 'Mixed (differs per table)') : null);
  sel.onchange = () => onPick(sel.value);
  return sel;
}

function admPrivChips(privs, onChange) {
  const set = new Set(privs);
  return el('div', { class: 'priv-chips' }, ADM_PRIVS.map((p) => el('button', { type: 'button', class: 'chip' + (set.has(p) ? ' on' : ''), title: p,
    'aria-pressed': set.has(p) ? 'true' : 'false', onclick: () => { if (set.has(p)) set.delete(p); else set.add(p); onChange(admNorm([...set])); } },
  set.has(p) ? icon('check') : null, ADM_PRIV_LABEL[p])));
}

async function admAccess(box, token) {
  const sid = state.route.sid;
  if (!(await admLoadRoles(box, token, 'access'))) return;
  if (!ADM.dbs || ADM.dbs.sid !== sid) {
    try {
      const rows = await api('GET', `/api/servers/${enc(sid)}/databases`);
      if (stale(token) || ADM.sub !== 'access') return;
      ADM.dbs = Object.assign(rows.filter((r) => r.allow_connections !== false), { sid });
    } catch (e) { if (!stale(token)) errorCard(box, e, () => admAccess(box, token)); return; }
  }
  const people = [...admUsersOf(), ...admGroupsOf()];
  if (!ADM.accRole || !admRole(ADM.accRole)) ADM.accRole = (admGroupsOf()[0] || admUsersOf().find((u) => !u.superuser) || people[0] || {}).name || null;
  if (!ADM.accDb || !ADM.dbs.some((x) => x.name === ADM.accDb)) {
    const remembered = store('admin.accessDb.' + sid);
    ADM.accDb = (ADM.dbs.find((x) => x.name === remembered) || ADM.dbs.find((x) => !['postgres'].includes(x.name)) || ADM.dbs[0] || {}).name || null;
  }
  const left = el('div', { class: 'card' });
  const right = el('div');
  clear(box, el('div', { class: 'adm-split' }, left, right));
  admDrawWho(left, right, token);
  admLoadAccess(right, token);
}

function admDrawWho(left, right, token) {
  const q = ADM.accFilter.toLowerCase();
  const pick = async (name) => {
    if (name === ADM.accRole) return;
    if (!(await admLeaveAccess())) return;
    ADM.accRole = name; ADM.acc = null; ADM.accCur = null;
    admDrawWho(left, right, token);
    admLoadAccess(right, token);
  };
  const item = (r) => el('button', { type: 'button', class: r.name === ADM.accRole ? 'on' : '', onclick: () => pick(r.name), 'aria-current': r.name === ADM.accRole ? 'true' : null },
    el('div', { class: 'person' }, admAvatar(r, 'sm'), el('div', { style: 'min-width: 0' }, el('div', { class: 'nm', text: r.name }),
      el('div', { class: 'sub', text: r.superuser ? 'superuser' : r.login ? (r.member_of.length ? `in ${r.member_of.map((m) => m.name).join(', ')}` : 'user') : fmt.plural(r.members.length, 'member') }))));
  const match = (r) => !q || r.name.toLowerCase().includes(q);
  const groups = admGroupsOf().filter(match), users = admUsersOf().filter(match);
  const search = el('input', { class: 'input', placeholder: 'Find a user or group', value: ADM.accFilter, 'aria-label': 'Find a user or group' });
  search.oninput = () => { ADM.accFilter = search.value; admDrawWho(left, right, token); const f = left.querySelector('input'); f.focus(); f.setSelectionRange(f.value.length, f.value.length); };
  clear(left, el('div', { style: 'padding: 12px 12px 4px' }, el('div', { class: 'label', style: 'margin-bottom: 6px', text: 'Who' }), search),
    el('div', { class: 'who-list' },
      groups.length ? el('div', { class: 'grp', text: 'Groups' }) : null, groups.map(item),
      users.length ? el('div', { class: 'grp', text: 'Users' }) : null, users.map(item),
      !groups.length && !users.length ? el('div', { class: 'subtle', style: 'padding: 12px 8px', text: q ? 'No match.' : 'No users or groups yet.' }) : null));
}

async function admLoadAccess(right, token) {
  const sid = state.route.sid;
  if (!ADM.accRole) {
    clear(right, el('div', { class: 'card' }, el('div', { class: 'empty' }, el('div', { class: 'ic' }, icon('users')), el('h3', { text: 'No users or groups yet' }),
      el('p', { text: 'Create a group (e.g. "readers") or a user first, then choose here what it may access.' }),
      el('button', { class: 'btn primary', type: 'button', onclick: () => ADM.go('groups') }, icon('plus'), 'Go to Groups'))));
    return;
  }
  if (!ADM.accDb) { clear(right, el('div', { class: 'card' }, el('div', { class: 'empty' }, el('h3', { text: 'No database to connect to' })))); return; }
  if (!ADM.acc || ADM.acc.role.name !== ADM.accRole || ADM.acc.database.name !== ADM.accDb) {
    loadingView(right, `Reading what ${ADM.accRole} may do in ${ADM.accDb}…`);
    try {
      const d = await api('GET', `/api/servers/${enc(sid)}/admin/access?db=${enc(ADM.accDb)}&role=${enc(ADM.accRole)}`);
      if (stale(token) || ADM.sub !== 'access' || d.role.name !== ADM.accRole) return;
      ADM.acc = d; ADM.accCur = admAccInit(d); ADM.accOpen.clear(); ADM.accCustom.clear();
    } catch (e) { if (!stale(token)) errorCard(right, e, () => { ADM.acc = null; admLoadAccess(right, token); }); return; }
  }
  admDrawAccess(right, token);
}

function admDrawAccess(right, token) {
  const d = ADM.acc, cur = ADM.accCur, role = d.role;
  const r = admRole(role.name) || role;
  const tablesBy = admTablesBy(d);
  const bar = el('div');
  const cards = el('div');
  const redrawBar = () => admDrawBar(bar, right, token);

  const dbSel = el('select', { class: 'input', style: 'width: auto; min-width: 200px', 'aria-label': 'Database' },
    ADM.dbs.map((x) => el('option', { value: x.name, selected: x.name === ADM.accDb }, x.name)));
  dbSel.onchange = async () => {
    if (!(await admLeaveAccess())) { dbSel.value = ADM.accDb; return; }
    ADM.accDb = dbSel.value; store('admin.accessDb.' + state.route.sid, ADM.accDb); ADM.acc = null;
    admLoadAccess(right, token);
  };

  // quick set: every table in every schema at once
  const quick = el('select', { class: 'input', style: 'width: auto', 'aria-label': 'Set every table' },
    el('option', { value: '', text: 'Set all tables to…' }), ADM_LEVELS.map(([k, text]) => el('option', { value: k, text })));
  quick.onchange = () => {
    const lv = ADM_LEVELS.find(([k]) => k === quick.value);
    if (!lv) return;
    for (const s of d.schemas) if ((tablesBy[s.name] || []).length) admSetBulk(s.name, lv[2]);
    quick.value = '';
    drawCards(); redrawBar();
  };

  const header = el('div', { class: 'card', style: 'margin-bottom: 12px' },
    el('div', { class: 'acc-head' },
      el('div', { class: 'person', style: 'gap: 12px' }, admAvatar(r, 'lg'), el('div', null,
        el('h4', null, role.name, el('span', { class: 'badge ' + (role.login ? 'info' : 'ok'), text: role.login ? 'user' : 'group' })),
        el('div', { class: 'subtle', style: 'font-size: 12.5px' }, role.login
          ? (role.member_of.length ? `Also gets the access of: ${role.member_of.join(', ')}` : 'Not in any group')
          : `${fmt.plural((r.members || []).length, 'member')} get${(r.members || []).length === 1 ? 's' : ''} this access`))),
      el('div', { class: 'grow' }),
      el('div', { class: 'field' }, el('label', null, 'Database'), dbSel)));

  const notes = [
    role.superuser ? el('div', { class: 'alert warn', style: 'margin-bottom: 12px' }, icon('shield'), el('div', { class: 'body' }, el('b', { text: `${role.name} is a superuser` }),
      'It can do everything in every database; nothing chosen here limits it.')) : null,
    !d.capabilities.superuser ? el('div', { class: 'alert info', style: 'margin-bottom: 12px' }, icon('info'), el('div', { class: 'body' },
      `${d.capabilities.user} is not a superuser: it can only give access to tables it owns (or was given with grant option). PostgreSQL refuses the rest and says which.`)) : null,
    d.truncated ? el('div', { class: 'alert warn', style: 'margin-bottom: 12px' }, icon('alert'), el('div', { class: 'body', text: 'This database has more than 5,000 tables; only the first 5,000 are listed one by one. Schema-wide settings still cover all of them.' })) : null,
    role.login && !role.member_of.length && !role.superuser ? el('div', { class: 'alert info', style: 'margin-bottom: 12px' }, icon('info'), el('div', { class: 'body' },
      el('b', { text: 'Tip' }), 'Giving access to a group and adding users to it is easier to manage than giving it to each user. ',
      el('a', { href: '#', onclick: (e) => { e.preventDefault(); ADM.go('groups'); } }, 'Manage groups'))) : null,
  ];

  const dbCard = el('div');
  const drawDb = () => {
    const eff = new Set(d.database.effective), has = new Set(cur.db);
    const row = (p, label, hint) => {
      const viaOther = eff.has(p) && !d.database.direct.includes(p);
      const cb = el('input', { type: 'checkbox', checked: has.has(p) || viaOther, disabled: viaOther || d.database.is_owner,
        onchange: (e) => { if (e.target.checked) has.add(p); else has.delete(p); cur.db = ['CONNECT', 'CREATE', 'TEMPORARY'].filter((x) => has.has(x)); drawDb(); redrawBar(); } });
      return el('label', { class: 'check toggle-row' }, el('span', null, el('span', { class: 't', text: label }),
        el('span', { class: 'd', text: d.database.is_owner ? 'Owner of the database' : viaOther ? (d.database.public.includes(p) ? 'Allowed for everyone (PUBLIC)' : 'Allowed through a group') : hint })), cb);
    };
    clear(dbCard, el('div', { class: 'acc-card' },
      el('div', { class: 'acc-head' }, el('h4', null, icon('db'), d.database.name), el('span', { class: 'subtle', text: `owner ${d.database.owner}` })),
      el('div', { style: 'padding: 0 16px 6px' },
        row('CONNECT', 'Can connect to this database', 'Needed for anything else'),
        row('CREATE', 'Can create schemas', 'CREATE on the database'),
        row('TEMPORARY', 'Can create temporary tables', 'TEMPORARY'))));
  };

  const drawCards = () => clear(cards, d.schemas.length ? d.schemas.map((s) => admSchemaCard(s, tablesBy[s.name] || [], redrawBar))
    : el('div', { class: 'card' }, el('div', { class: 'empty' }, el('h3', { text: 'No schemas' }), el('p', { text: 'This database has no user schemas yet.' }))));

  drawDb();
  drawCards();
  clear(right, header, notes,
    el('div', { class: 'row', style: 'margin: 4px 0 10px; gap: 10px; flex-wrap: wrap' },
      el('div', { class: 'legend' }, ...ADM_LEVELS.slice(1).map(([k, text]) => el('span', { title: ADM_LEVEL_HINT[k] },
        el('i', { style: `background: var(--${{ read: 'info', write: 'ok', modify: 'warn', all: 'danger' }[k]})` }), text))),
      el('div', { class: 'grow' }), quick),
    dbCard, cards, bar);
  redrawBar();
}

function admSetBulk(schema, privs) {
  const cur = ADM.accCur;
  cur.bulk[schema] = admNorm(privs);
  for (const t of admTablesBy(ADM.acc)[schema] || []) if (!t.is_owner) cur.table[admTKey(schema, t.name)] = admNorm(privs);
  const sc = cur.schema[schema];
  if (sc.futureOn) sc.future = admNorm(privs);
  ADM.accCustom.delete('t:' + schema);
}

function admSchemaCard(s, tables, redrawBar) {
  const card = el('div', { class: 'acc-card' });
  const cur = ADM.accCur;
  const draw = () => {
    const sc = cur.schema[s.name];
    const own = tables.filter((t) => !t.is_owner);
    const sets = own.map((t) => cur.table[admTKey(s.name, t.name)]);
    const uniform = sets.length && sets.every((x) => admSame(x, sets[0]));
    const now = uniform ? sets[0] : [];
    const orig = own.map((t) => t.direct);
    const tablesChanged = own.some((t, i) => !admSame(sets[i], orig[i]));
    const customKey = 's:' + s.name;
    if (sc.futureOn === undefined) sc.futureOn = sc.future.length > 0;

    const pickLevel = (k) => {
      if (k === 'custom') { ADM.accCustom.add(customKey); if (!uniform) admSetBulk(s.name, []); draw(); redrawBar(); return; }
      ADM.accCustom.delete(customKey);
      const lv = ADM_LEVELS.find(([x]) => x === k);
      if (lv) admSetBulk(s.name, lv[2]);
      draw(); redrawBar();
    };
    const tableSel = s.is_owner && !own.length ? el('span', { class: 'badge ok', text: 'Owner: full control' })
      : !tables.length ? el('span', { class: 'subtle', text: 'No tables yet' })
        : admLevelSelect({ privs: now, mixed: !uniform, changed: tablesChanged, label: `Tables in ${s.name}`, onPick: pickLevel });
    const custom = (ADM.accCustom.has(customKey) || (uniform && own.length && admLevelOf(now) === 'custom')) && own.length
      ? admPrivChips(now, (p) => { admSetBulk(s.name, p); ADM.accCustom.add(customKey); draw(); redrawBar(); }) : null;

    const execSel = !s.routines ? el('span', { class: 'subtle', text: 'No functions' })
      : el('select', { class: 'input' + (sc.exec !== (s.routines_direct === s.routines ? true : s.routines_direct === 0 ? false : null) ? ' changed' : ''), 'aria-label': `Functions in ${s.name}`,
        onchange: (e) => { sc.exec = e.target.value === 'yes'; draw(); redrawBar(); } },
      el('option', { value: 'no', selected: sc.exec === false }, 'Cannot run'),
      el('option', { value: 'yes', selected: sc.exec === true }, `Can run all ${s.routines}`),
      sc.exec === null ? el('option', { value: 'some', selected: true, disabled: true }, `Some (${s.routines_direct} of ${s.routines})`) : null);
    const execNote = s.routines && sc.exec !== true && s.routines_effective > s.routines_direct
      ? el('div', { class: 'eff', text: `${s.routines_effective} of ${s.routines} runnable anyway (PUBLIC or a group)` }) : null;

    const createSel = s.is_owner ? el('span', { class: 'badge ok', text: 'Owner' })
      : el('select', { class: 'input' + (sc.create !== s.direct.includes('CREATE') ? ' changed' : ''), 'aria-label': `Create in ${s.name}`,
        onchange: (e) => { sc.create = e.target.value === 'yes'; draw(); redrawBar(); } },
      el('option', { value: 'no', selected: !sc.create }, 'Cannot create'),
      el('option', { value: 'yes', selected: sc.create }, 'Can create tables'));
    const createNote = !s.is_owner && !sc.create && s.eff_create ? el('div', { class: 'eff', text: 'Allowed anyway (PUBLIC or a group)' }) : null;

    const future = el('label', { class: 'check', style: 'margin-top: 6px', title: `ALTER DEFAULT PRIVILEGES FOR ROLE ${s.owner}: tables ${s.owner} creates later get the same access` },
      el('input', { type: 'checkbox', checked: sc.futureOn, disabled: !uniform && !sc.futureOn, onchange: (e) => {
        sc.futureOn = e.target.checked; sc.future = e.target.checked ? admNorm(now) : []; draw(); redrawBar(); } }),
      el('span', { class: 'd', style: 'font-size: 12px' }, sc.futureOn && !admSame(sc.future, now) ? `New tables too (${admLevelLabel(sc.future)})` : 'Also tables created later'));

    const open = ADM.accOpen.has(s.name);
    const more = tables.length ? el('button', { type: 'button', class: 'acc-more' + (open ? ' open' : ''), 'aria-expanded': open ? 'true' : 'false',
      onclick: () => { if (open) ADM.accOpen.delete(s.name); else ADM.accOpen.add(s.name); draw(); } },
    icon('chevron'), open ? 'Hide tables' : `Set table by table (${fmt.plural(tables.length, 'table')})`,
    !uniform ? el('span', { class: 'badge accent', style: 'margin-left: 6px', text: 'differs per table' }) : null) : null;

    clear(card,
      el('div', { class: 'acc-head' }, el('h4', null, icon('folder'), s.name),
        el('span', { class: 'subtle', text: `owner ${s.owner}` }), s.is_owner ? el('span', { class: 'badge ok', text: `${ADM.acc.role.name} owns it` }) : null),
      el('div', { class: 'acc-grid' },
        el('div', { class: 'field' }, el('label', null, icon('db'), 'Tables & views'), tableSel, custom, tables.length && !s.is_owner ? future : null),
        el('div', { class: 'field' }, el('label', null, icon('play'), 'Functions'), execSel, execNote),
        el('div', { class: 'field' }, el('label', null, icon('plus'), 'Create new tables'), createSel, createNote)),
      more,
      open ? admTableList(s, tables, () => { draw(); redrawBar(); }) : null);
  };
  draw();
  return card;
}

function admTableList(s, tables, changed) {
  const cur = ADM.accCur;
  const wrap = el('div', { class: 'acc-tables' });
  const filter = el('input', { class: 'input', placeholder: 'Filter tables', style: 'max-width: 240px', 'aria-label': `Filter tables in ${s.name}` });
  const body = el('tbody');
  const kindLabel = { v: 'view', m: 'materialized view', f: 'foreign table', p: 'partitioned' };
  const drawRows = () => {
    const q = filter.value.toLowerCase();
    const rows = tables.filter((t) => !q || t.name.toLowerCase().includes(q));
    clear(body, rows.slice(0, 500).map((t) => {
      const key = admTKey(s.name, t.name);
      const now = cur.table[key];
      const ck = 't:' + key;
      const extra = admNorm(t.effective.filter((p) => !now.includes(p)));
      const sel = t.is_owner ? el('span', { class: 'badge ok', text: 'Owner: full control' }) : admLevelSelect({ privs: now, changed: !admSame(now, t.direct),
        label: `Access to ${t.name}`, onPick: (k) => {
          if (k === 'custom') ADM.accCustom.add(ck); else { ADM.accCustom.delete(ck); const lv = ADM_LEVELS.find(([x]) => x === k); if (lv) cur.table[key] = admNorm(lv[2]); }
          delete cur.bulk[s.name]; changed();
        } });
      return el('tr', null,
        el('td', null, el('span', { class: 'mono', text: t.name }), kindLabel[t.kind] ? el('span', { class: 'badge', style: 'margin-left: 6px', text: kindLabel[t.kind] }) : null),
        el('td', { style: 'width: 46%' }, sel,
          !t.is_owner && (ADM.accCustom.has(ck) || admLevelOf(now) === 'custom') ? admPrivChips(now, (p) => { cur.table[key] = p; ADM.accCustom.add(ck); delete cur.bulk[s.name]; changed(); }) : null,
          !t.is_owner && extra.length ? el('div', { class: 'eff', text: `Also has ${extra.map((p) => ADM_PRIV_LABEL[p]).join(', ')} through a group or PUBLIC` }) : null));
    }), rows.length > 500 ? el('tr', null, el('td', { colspan: '2', class: 'subtle', text: `… ${rows.length - 500} more; filter to find one.` })) : null,
    !rows.length ? el('tr', null, el('td', { colspan: '2', class: 'subtle', text: 'No tables match.' })) : null);
  };
  filter.oninput = drawRows;
  drawRows();
  return clear(wrap, tables.length > 12 ? el('div', { class: 'toolbar' }, filter) : null,
    el('div', { class: 'table-wrap' }, el('table', { class: 't' }, el('thead', null, el('tr', null, el('th', { text: 'Table' }), el('th', { text: 'Access' }))), body)));
}

function admDrawBar(bar, right, token) {
  const changes = admAccChanges();
  if (!changes.length) { clear(bar); return; }
  const discard = el('button', { class: 'btn', type: 'button', onclick: () => { ADM.accCur = admAccInit(ADM.acc); ADM.accCustom.clear(); admDrawAccess(right, token); } }, 'Discard');
  const apply = el('button', { class: 'btn primary', type: 'button', onclick: () => admApplyAccess(apply, right, token) }, icon('check'), 'Review & apply');
  clear(bar, el('div', { class: 'changes-bar', role: 'status' }, el('span', { class: 'dot' }),
    el('div', { class: 'grow' }, el('b', { text: `${fmt.plural(changes.length, 'change')} not applied yet` }),
      el('div', { class: 'subtle', style: 'font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap', text: changes.slice(0, 3).map((c) => c.label).join(' · ') + (changes.length > 3 ? ' …' : '') })),
    discard, apply));
}

async function admApplyAccess(btn, right, token) {
  const d = ADM.acc;
  const changes = admAccChanges();
  const payload = { db: d.database.name, role: d.role.name, changes: changes.map(({ label, ...c }) => c) };
  busy(btn, true, 'Checking…');
  let sqlText;
  try { sqlText = (await api('POST', `/api/servers/${enc(state.route.sid)}/admin/access/preview`, payload)).sql; } catch (e) { busy(btn, false); toastError(e, 'Could not prepare'); return; }
  busy(btn, false);
  if (!sqlText.length) { toast('Nothing to change: it already has that access.', 'info'); ADM.acc = null; admLoadAccess(right, token); return; }
  const ok = await confirmBox({ title: `Change the access of ${d.role.name}?`, confirmLabel: 'Apply',
    message: `In database ${d.database.name}, all at once (all or nothing):`,
    extra: [el('ul', { class: 'plain-list' }, changes.slice(0, 40).map((c) => el('li', { text: c.label })), changes.length > 40 ? el('li', { text: `… and ${changes.length - 40} more` }) : null),
      el('details', { class: 'adv', style: 'margin-top: 12px' }, el('summary', null, icon('chevron'), `Show SQL (${fmt.plural(sqlText.length, 'statement')})`),
        el('pre', { class: 'mono', style: 'white-space: pre-wrap; word-break: break-word; max-height: 32vh; overflow: auto; margin: 0; padding: 12px; background: var(--surface-2); border-radius: var(--radius-sm); font-size: 12px', text: sqlText.join('\n') }))] });
  if (!ok) return;
  busy(btn, true, 'Applying…');
  try {
    await api('POST', `/api/servers/${enc(state.route.sid)}/admin/access`, payload);
    toast(`Access of ${d.role.name} in ${d.database.name} updated.`, 'ok');
    ADM.acc = null;
    if (!stale(token)) admLoadAccess(right, token);
  } catch (e) { busy(btn, false); toastError(e, 'Not changed'); }
}
