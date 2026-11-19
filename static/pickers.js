/* PG Backup Studio: Browse buttons for folders and backup files (decision 014).
 * The desktop window shows its own dialog; in a browser the local server shows the operating system's dialog
 * (`state.dialogs` is false only where none exists, e.g. Linux without zenity / kdialog). Uses el, icon, api, toastError,
 * state from the main script; nothing runs at load time. */
'use strict';

/** `input` with a Browse… button that opens a native folder dialog; the input alone when no dialog exists. */
function folderField(input) {
  if (!state.dialogs) return input;
  input.style.flex = '1';
  const b = el('button', { class: 'btn', type: 'button' }, icon('folder'), 'Browse…');
  b.onclick = () => pickInto(b, input, (start) => api('POST', '/api/pick-folder', { start }));
  return el('div', { class: 'row' }, input, b);
}

/** The restore source "File on this PC": path input + Browse… (starts in the folder of the typed file, else the backup folder). */
function backupFileField(input) {
  if (!state.dialogs) return input;
  input.style.flex = '1';
  const b = el('button', { class: 'btn', type: 'button' }, icon('file'), 'Browse…');
  b.onclick = () => pickInto(b, input, (start) => api('POST', '/api/pick-file', { start }));
  return el('div', { class: 'row' }, input, b);
}

async function pickInto(btn, input, ask) {
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    const r = await ask(input.value.trim());
    if (r.path) {
      input.value = r.path;
      input.dispatchEvent(new Event('input'));
      input.dispatchEvent(new Event('change'));
    }
  } catch (e) { toastError(e, 'No dialog'); } finally { btn.disabled = false; }
}
