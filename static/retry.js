/* PG Backup Studio: retry a failed, cancelled or interrupted backup or restore (decision 014).
 * Used by the Activity cards and the History rows of the main script (el, icon, api, toast, toastError, modal, busy,
 * pollJobs, go, enc, state); nothing runs at load time. */
'use strict';

const RETRY_STATUSES = ['failed', 'cancelled', 'interrupted'];
const NETWORK_ERROR = /connection|network|timed? ?out|timeout|could not connect|server closed|terminated abnormally|ssl syscall|eof detected|unreachable|refused|reset by peer|no route|could not receive|could not send|broken pipe/i;

/** `x`: a job snapshot or a History entry. History entries made before 0.13 lack `retry` and cannot be retried. */
function canRetry(x, fromHistory) {
  return (x.kind === 'backup' || x.kind === 'restore') && RETRY_STATUSES.includes(x.status) && (!fromHistory || !!x.retry);
}

function looksLikeNetwork(x) {
  return x.status === 'interrupted' || (!!x.error && NETWORK_ERROR.test(x.error));
}

/** Retry button for a job card / History row; null when the job cannot be retried. */
function retryButton(x, fromHistory) {
  if (!canRetry(x, fromHistory)) return null;
  const title = `Retry: start this ${x.kind} again with the same settings`;
  const b = el('button', { class: 'btn sm' + (looksLikeNetwork(x) ? ' primary' : '') + (fromHistory ? ' icon-only' : ''), type: 'button',
    title, 'aria-label': title }, icon('refresh'), fromHistory ? null : 'Retry');
  b.onclick = (e) => { e.stopPropagation(); retryJob(x, b); };
  return b;
}

/** Shown under a failed job whose error looks like a lost connection. */
function networkHint(j) {
  if (!canRetry(j) || !looksLikeNetwork(j)) return null;
  return el('div', { class: 'alert warn' }, icon('alert'), el('div', { class: 'body' },
    el('b', { text: j.status === 'interrupted' ? 'The app ended during this job' : 'The connection to the server was lost' }),
    `Check the network or VPN, then press Retry to start the ${j.kind} again with the same settings.`,
    j.kind === 'backup' ? ' A new backup file is written; the unfinished one was removed.' : ''));
}

async function retryJob(x, btn) {
  let target;
  if (x.kind === 'restore') {
    target = await retryRestoreTarget(x);
    if (!target) return;
  }
  if (btn) busy(btn, true, 'Starting…');
  try {
    const res = await api('POST', `/api/history/${enc(x.id)}/retry`, { server_id: x.server && x.server.id, target });
    toast(`${x.kind === 'backup' ? 'Backup of' : 'Restore into'} ${x.database} started again.`, 'ok');
    state.openJob = res.job_id;
    if (x.kind === 'restore' && x.server) delete state.dbs[x.server.id];
    await pollJobs();
    go(`#/activity/${enc(res.job_id)}`);
  } catch (e) {
    toastError(e, 'Not retried');
  } finally {
    if (btn && btn.isConnected) busy(btn, false);
  }
}

/** The failed attempt may have left the database half restored: drop and recreate it (typed confirmation), or restore
 * into it as it is. Resolves to the target for the request, or null when cancelled. */
function retryRestoreTarget(x) {
  const db = x.database;
  return new Promise((resolve) => {
    let done = false;
    let mode = x.target_mode === 'existing' ? 'existing' : 'recreate';
    const ok = el('button', { class: 'btn primary', type: 'button' }, icon('refresh'), 'Retry');
    const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
    const input = el('input', { class: 'input mono', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Type the database name to confirm' });
    const typed = el('div', { class: 'field' }, el('label', null, 'Type ', el('span', { class: 'mono', text: db }), ' to confirm'), input);
    const cards = el('div', { class: 'radio-cards', style: 'grid-template-columns: 1fr' });
    const draw = () => {
      clear(cards, [
        ['recreate', `Drop and recreate ${db}`, 'Removes everything in it, then restores the file from the start. Recommended.'],
        ['existing', `Restore into ${db} as it is`, 'Keeps what the failed attempt restored; objects that already exist give "already exists" errors.'],
      ].map(([k, t, d]) => el('button', { type: 'button', class: 'radio-card' + (mode === k ? ' on' : ''), onclick: () => { mode = k; draw(); } },
        el('span', { class: 't', text: t }), el('span', { class: 'd', text: d }))));
      typed.style.display = mode === 'recreate' ? '' : 'none';
      ok.className = 'btn ' + (mode === 'recreate' ? 'danger' : 'primary');
      ok.disabled = mode === 'recreate' && input.value !== db;
    };
    input.addEventListener('input', draw);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !ok.disabled) ok.click(); });
    const m = modal({ title: `Retry the restore into ${db}?`, iconName: 'refresh', wide: true,
      body: [el('div', { class: 'subtle', text: `The file is ${x.file || 'the same backup file'}. The failed attempt may have left ${db} partly restored.` }), cards, typed],
      actions: [cancel, ok], onClose: () => { if (!done) resolve(null); } });
    draw();
    cancel.onclick = () => m.close();
    ok.onclick = () => {
      done = true;
      m.close();
      resolve(mode === 'recreate' ? { mode: 'recreate', confirm: db } : { mode: 'existing', allow_non_empty: true });
    };
  });
}
