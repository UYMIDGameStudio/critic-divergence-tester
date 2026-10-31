// User input stays separate from authoritative review state and approvals.
let draftContext = null;
let draftTimer = null;
let draftWrites = Promise.resolve();
const fieldDefaults = new WeakMap();
function setCorrectionEditorOpen(open, focus = false) {
  const editor = document.getElementById('correction-editor');
  const button = document.getElementById('show-correction');
  if (!editor || !button) return;
  editor.classList.toggle('hidden', !open);
  button.classList.toggle('hidden', open);
  button.setAttribute('aria-expanded', String(open));
  if (focus) (open ? document.getElementById('correction') : button).focus();
}
function editableFields() {
  return [
    ...root.querySelectorAll(
      'input[id]:not([type=file]),textarea[id],select[id]',
    ),
  ].filter((element) => !element.readOnly);
}
function fieldKey(element) {
  const id = element.id;
  if (['ai-response', 'binding-mode'].includes(id)) {
    return id + '::' + (selectedRequest()?.request_id || 'unassigned');
  }
  const action = state.selected?.revision_workspace?.actions?.find((item) =>
    id.endsWith('-' + item.action_id),
  );
  if (action)
    return id + '::' + (action.operation_decision_sha256 || 'unconfirmed');
  if (id.startsWith('external-')) {
    return (
      id +
      '::' +
      (state.selected?.revision_workspace?.revision?.revision_id ||
        'unassigned')
    );
  }
  return id;
}
function elementValue(element) {
  return element.type === 'checkbox' ? element.checked : element.value;
}
function assignField(element, value) {
  if (element.type === 'checkbox') element.checked = value === true;
  else if (
    typeof value === 'string' &&
    (element.tagName !== 'SELECT' ||
      [...element.options].some((option) => option.value === value))
  )
    element.value = value;
}
function loadDraft() {
  const selected = state.selected;
  if (!selected) {
    draftContext = null;
    return;
  }
  const saved = selected.ui_draft;
  if (
    draftContext?.directory === selected.directory &&
    draftContext.scope === saved.scope &&
    (draftContext.sequence > draftContext.savedSequence || draftContext.pending)
  )
    return;
  draftContext = {
    directory: selected.directory,
    scope: saved.scope,
    revision: saved.revision,
    fields: { ...saved.fields },
    scroll: saved.scroll || 0,
    sequence: 0,
    savedSequence: 0,
    pending: 0,
    error: saved.error || null,
    legacy: saved.legacy || null,
  };
}
function restoreDraftFields() {
  if (!draftContext) return;
  // Task selection determines the keys of its response and binding controls.
  const fields = editableFields().sort(
    (a, b) => (b.id === 'ai-request') - (a.id === 'ai-request'),
  );
  for (const element of fields) {
    if (!fieldDefaults.has(element))
      fieldDefaults.set(element, elementValue(element));
    const key = fieldKey(element);
    element.dataset.draftKey = key;
    assignField(
      element,
      Object.hasOwn(draftContext.fields, key)
        ? draftContext.fields[key]
        : fieldDefaults.get(element),
    );
  }
  const editorOpen = draftContext.fields['correction-editor-open'];
  setCorrectionEditorOpen(typeof editorOpen === 'boolean' ? editorOpen
    : Boolean(document.getElementById('correction')?.value));
  filterFindings();
  restoreSourceReaderState();
  draftStatus();
}
function captureDraftFields() {
  if (!draftContext) return;
  let changed = captureSourceReaderState();
  const editor = document.getElementById('correction-editor');
  if (editor) {
    const open = !editor.classList.contains('hidden'), key = 'correction-editor-open';
    if ((open || Object.hasOwn(draftContext.fields, key)) && draftContext.fields[key] !== open) {
      draftContext.fields[key] = open;
      changed = true;
    }
  }
  for (const element of editableFields()) {
    const key = element.dataset.draftKey || fieldKey(element);
    const value = elementValue(element);
    // Unedited defaults belong to the current server view, not to the draft.
    if (
      !Object.hasOwn(draftContext.fields, key) &&
      value === fieldDefaults.get(element)
    )
      continue;
    if (
      !Object.hasOwn(draftContext.fields, key) ||
      draftContext.fields[key] !== value
    ) {
      draftContext.fields[key] = value;
      changed = true;
    }
  }
  const scroll = Math.round(window.scrollY);
  if (draftContext.scroll !== scroll) changed = true;
  draftContext.scroll = scroll;
  if (changed) draftContext.sequence++;
}
function draftStatus() {
  const label = document.getElementById('draft-status');
  if (!label || !draftContext) return;
  label.textContent = draftContext.error
    ? tr('草稿未保存：') + (UI_MESSAGES[draftContext.error]?.[uiLocale] ?? draftContext.error)
    : draftContext.pending || draftContext.sequence > draftContext.savedSequence
      ? tr('正在保存…')
      : tr('草稿已保存到本机');
}
function scheduleDraft() {
  if (!draftContext || state.selected?.state.read_only) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveDraft().catch(() => {}), 700);
  draftStatus();
}
async function saveDraft() {
  clearTimeout(draftTimer);
  draftTimer = null;
  if (!draftContext || state.selected?.state.read_only) return;
  captureDraftFields();
  const context = draftContext;
  if (context.sequence === context.savedSequence && !context.pending) return;
  const fields = { ...context.fields },
    sequence = context.sequence,
    scroll = context.scroll;
  context.pending++;
  // A failed write keeps its unsaved sequence dirty and can be explicitly retried.
  const write = draftWrites
    .catch(() => {})
    .then(async () => {
      const result = await api('/api/draft', {
        project_directory: context.directory,
        scope: context.scope,
        revision: context.revision,
        fields,
        scroll,
      });
      context.revision = result.revision;
      context.savedSequence = Math.max(context.savedSequence, sequence);
      context.error = null;
      const selected = state.selected, saved = selected?.ui_draft;
      if (selected?.directory === context.directory && saved?.scope === context.scope
          && !selected.state?.read_only && !saved.error && saved.revision < result.revision) {
        // A language rerender can reuse this view without another state fetch.
        // Retain only acknowledged values, never later unsaved input or an older
        // project's delayed response, and do not replace a newer server view.
        selected.ui_draft = {...saved, revision: result.revision, fields: {...fields}, scroll, error: null};
      }
    });
  draftWrites = write;
  try {
    await write;
  } catch (error) {
    context.error = error.systemMessage || error.message;
    throw error;
  } finally {
    context.pending--;
    draftStatus();
  }
}
function downloadDraft(legacy = false) {
  captureDraftFields();
  if (!draftContext) return;
  const content = {
    project_directory: draftContext.directory,
    scope: draftContext.scope,
    fields: legacy ? draftContext.legacy : draftContext.fields,
  };
  const blob = new Blob([JSON.stringify(content, null, 2)], {
    type: 'application/json',
  });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = legacy ? tr('旧版表单草稿.json') : tr('当前表单草稿.json');
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
function onDraftInput(event) {
  if (!draftContext || !event.target.matches('input,textarea,select')) return;
  captureDraftFields();
  if (event.target.id === 'ai-request') restoreDraftFields();
  scheduleDraft();
}
root.addEventListener('input', onDraftInput);
root.addEventListener('change', onDraftInput);
window.addEventListener('beforeunload', (event) => {
  if (
    mutationPending ||
    draftContext?.pending ||
    (draftContext && draftContext.sequence > draftContext.savedSequence)
  ) {
    event.preventDefault();
    event.returnValue = '';
  }
});
