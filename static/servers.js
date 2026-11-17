/* PG Backup Studio: export saved servers to a .json file and import such a file on another PC (Settings → Servers).
 * Uses the page's helpers (el, icon, api, modal, busy, toast, toastError, loadServers, renderSideLinks, route, state)
 * inside functions only. Text is always set through text nodes.
 */
'use strict';

/** Settings field: "Export…" and "Import…" for the saved servers. */
function serversField(closeSettings) {
  const file = el('input', { type: 'file', accept: '.json,application/json', style: 'display:none' });
  file.onchange = () => { const f = file.files[0]; file.value = ''; if (f) importServersFile(f, closeSettings); };
  const exp = el('button', { class: 'btn sm', type: 'button', disabled: !state.servers.length, onclick: exportServersModal },
    icon('backup'), 'Export…');
  const imp = el('button', { class: 'btn sm', type: 'button', onclick: () => file.click() }, icon('restore'), 'Import…');
  return el('div', { class: 'field' }, el('label', null, 'Servers'),
    el('div', { class: 'row' }, el('span', { class: 'subtle grow', text: `${state.servers.length} saved` }), exp, imp, file),
    el('div', { class: 'hint', text: 'Export saves server connections to a .json file; Import adds the servers of such a file, e.g. on another PC.' }));
}

function exportServersModal() {
  const picked = new Set(state.servers.map((s) => s.id));
  const pw = el('input', { type: 'checkbox' });
  const warn = el('div', { class: 'hint', style: 'color: var(--warn)', hidden: true,
    text: 'The file will contain those passwords in plain text. Keep it private and delete it after importing.' });
  pw.onchange = () => { warn.hidden = !pw.checked; };
  const go = el('button', { class: 'btn primary', type: 'button' }, icon('backup'), 'Export');
  const cancel = el('button', { class: 'btn', type: 'button' }, 'Cancel');
  const count = () => { go.disabled = !picked.size; };
  const rows = state.servers.map((s) => el('label', { class: 'check' },
    el('input', { type: 'checkbox', checked: true, onchange: (e) => { if (e.target.checked) picked.add(s.id); else picked.delete(s.id); count(); } }),
    el('span', { text: s.name }), el('span', { class: 'mono subtle', text: ` ${s.user}@${s.host}:${s.port}` })));
  const m = modal({ title: 'Export servers', iconName: 'backup', sub: 'Save connections to a file you can import on another PC',
    body: [
      el('div', { class: 'field' }, el('label', null, 'Servers'), el('div', { style: 'display:grid; gap:6px; max-height:260px; overflow:auto' }, rows)),
      el('div', { class: 'field' }, el('label', { class: 'check' }, pw, 'Include saved passwords'), warn,
        el('div', { class: 'hint', text: 'Without passwords, the other PC asks for each password when you first connect.' })),
    ],
    actions: [cancel, go] });
  cancel.onclick = () => m.close();
  go.onclick = async () => {
    busy(go, true, 'Exporting…');
    m.error('');
    try {
      const doc = await api('POST', '/api/servers/export', { ids: Array.from(picked), include_passwords: pw.checked });
      const day = new Date().toISOString().slice(0, 10);
      wsGridDownload(`pg-backup-studio-servers-${day}.json`, JSON.stringify(doc, null, 2) + '\n', 'application/json');
      m.close();
      toast(`Exported ${doc.servers.length} server${doc.servers.length === 1 ? '' : 's'}${pw.checked ? ' with passwords' : ''}.`, 'ok');
    } catch (e) { busy(go, false); m.error(e.message); }
  };
}

async function importServersFile(f, closeSettings) {
  let doc;
  try {
    if (f.size > 5 * 1024 * 1024) throw new Error('This file is too large to be a server export.');
    doc = JSON.parse(await f.text());
  } catch (e) {
    toast(e instanceof SyntaxError ? `${f.name} is not a JSON file.` : e.message, 'err', 'Not imported');
    return;
  }
  try {
    const r = await api('POST', '/api/servers/import', { document: doc });
    const parts = [];
    if (r.added.length) parts.push(`Added ${r.added.length}: ${r.added.join(', ')}.`);
    if (r.skipped.length) parts.push(`Already saved, skipped ${r.skipped.length}: ${r.skipped.join(', ')}.`);
    toast(parts.join(' ') || 'The file has no servers.', r.added.length ? 'ok' : 'info', 'Servers imported');
    if (r.added.length) {
      closeSettings();
      await loadServers();
      renderSideLinks();
      route();
    }
  } catch (e) { toastError(e, 'Not imported'); }
}
