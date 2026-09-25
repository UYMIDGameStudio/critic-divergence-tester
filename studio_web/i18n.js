// Only interface literals and explicitly selected system labels are translated.
// Interpolated document text, filenames, user input and evidence remain intact.
const UI_MESSAGES = __UI_MESSAGES__;
let uiLocale = 'zh-Hant';
let preferenceWrites = Promise.resolve();
try {
  const saved = localStorage.getItem('studio-ui-language');
  if (saved === 'en' || saved === 'zh-Hant') uiLocale = saved;
} catch (_) {}
const messageKeys = Object.keys(UI_MESSAGES).sort(
  (a, b) => b.length - a.length,
);
const messagePattern = new RegExp(
  messageKeys
    .map((key) => key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|') || '(?!)',
  'g',
);

function tr(literal) {
  return String(literal ?? '').replace(
    messagePattern,
    (key) => UI_MESSAGES[key][uiLocale],
  );
}

function ui(parts, ...values) {
  return parts
    .map(
      (part, index) => tr(part) + (index < values.length ? values[index] : ''),
    )
    .join('');
}

function renderLanguageHeader() {
  document.documentElement.lang = uiLocale;
  document.title =
    uiLocale === 'en'
      ? 'Document & Argument Review Studio'
      : '文書與論證審查工作台';
  document.getElementById('studio-title').textContent = document.title;
  document.getElementById('studio-description').textContent =
    uiLocale === 'en'
      ? 'Import, inspect, review and approve each change. Your original files stay on this computer.'
      : '匯入並確認內容，完成審查後逐項批准修改。原始檔案完整保留在本機。';
  document.getElementById('ui-language').value = uiLocale;
}

function changeLanguage(event) {
  if (mutationPending) {
    event.target.value = uiLocale;
    return;
  }
  captureDraftFields();
  // Preserve selected upload files and home form input across a language change.
  const inputs = [
    ...root.querySelectorAll('input[id],textarea[id],select[id]'),
  ].map((element) => ({
    id: element.id,
    value: elementValue(element),
    files: element.type === 'file' ? element.files : null,
  }));
  uiLocale = event.target.value;
  try {
    localStorage.setItem('studio-ui-language', uiLocale);
  } catch (_) {}
  const language = uiLocale;
  preferenceWrites = preferenceWrites.catch(() => {}).then(() =>
    api('/api/preferences', {language})).catch(error => {
      operationStatus(uiLocale === 'en' ? 'The language changed for this page, but the preference could not be saved.' : '此頁語言已切換，但偏好設定未能保存。', 'failure');
    });
  renderLanguageHeader();
  render();
  // Completed operation notices belong to the old locale. Keep failures visible,
  // but replace an obsolete success toast with the current language change.
  if (operationBox.classList.contains('success')) {
    operationStatus(uiLocale === 'en' ? 'Interface language changed.' : '介面語言已切換。', 'success');
  }
  for (const saved of inputs) {
    const element = document.getElementById(saved.id);
    if (!element) continue;
    if (saved.files) element.files = saved.files;
    else assignField(element, saved.value);
  }
  scheduleDraft();
}

function serviceDetails(message) {
  if (!message) return '';
  // A service diagnostic may contain user filenames or quotations. Keep its
  // original content visible on demand instead of translating those values.
  return `<details class="card"><summary>${uiLocale === 'en' ? 'Operation details' : '操作詳情'}</summary><div class="quote">${esc(message)}</div></details>`;
}
