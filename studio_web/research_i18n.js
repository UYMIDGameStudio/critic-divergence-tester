// Translate only interface literals. Interpolated manuscripts, prompts, model
// responses, evidence and user drafts are never passed through this dictionary.
const RESEARCH_MESSAGES = __RESEARCH_MESSAGES__;
let researchLocale = 'zh-Hant';
let researchMutationPending = 0;
try {
  const saved = localStorage.getItem('studio-ui-language');
  if (saved === 'en' || saved === 'zh-Hant') researchLocale = saved;
} catch (_) {}
const researchPattern = new RegExp(Object.keys(RESEARCH_MESSAGES.text).sort((a, b) => b.length - a.length).map(key => key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') || '(?!)', 'g');
function tr(literal) {
  return String(literal ?? '').replace(researchPattern, key => RESEARCH_MESSAGES.text[key][researchLocale]);
}
function ui(parts, ...values) {
  return parts.map((part, index) => tr(part) + (index < values.length ? values[index] : '')).join('');
}
function systemLabel(value) {
  const text = String(value ?? '');
  return RESEARCH_MESSAGES.codes[text]?.[researchLocale] || RESEARCH_MESSAGES.text[text]?.[researchLocale] || text;
}
function applyResearchLanguage() {
  document.documentElement.lang = researchLocale;
  document.querySelectorAll('[data-research-text]').forEach(node => {
    node.textContent = tr(node.dataset.researchText);
  });
  document.querySelectorAll('#research-language,[data-research-language]').forEach(language => {
    language.value = researchLocale;
    language.setAttribute('aria-label', researchLocale === 'en' ? 'Interface language' : '介面語言');
    language.onchange = changeResearchLanguage;
  });
}
function markResearchMutation(delta) {
  researchMutationPending += delta;
  document.querySelectorAll('#research-language,[data-research-language]').forEach(language => {
    language.disabled = researchMutationPending > 0;
  });
}
function changeResearchLanguage(event) {
  if (researchMutationPending) {
    event.target.value = researchLocale;
    return;
  }
  const fields = [...document.querySelectorAll('input[id],textarea[id],select[id]')].filter(node => node.id !== 'research-language').map(node => ({
    node,
    id: node.id,
    value: node.value,
    checked: node.checked,
    start: node.selectionStart,
    end: node.selectionEnd
  }));
  const activeId = document.activeElement?.id;
  const scrolls = [...document.querySelectorAll('.pane')].map((node, index) => ({
    id: node.id, index, offset: node.scrollTop
  }));
  researchLocale = event.target.value;
  try {
    localStorage.setItem('studio-ui-language', researchLocale);
  } catch (_) {}
  applyResearchLanguage();
  renderResearchView();
  for (const saved of fields) {
    const replacement = document.getElementById(saved.id);
    if (!replacement) continue;
    if (saved.node.type === 'file') {
      // Retain the actual input element and FileList; never recreate a file
      // through text decoding or rely on assigning a browser FileList.
      if (replacement !== saved.node) replacement.replaceWith(saved.node);
    } else {
      replacement.value = saved.value;
      replacement.checked = saved.checked;
      if (saved.start != null && replacement.setSelectionRange) {
        replacement.setSelectionRange(saved.start, saved.end);
      }
    }
  }
  scrolls.forEach(({id, index, offset}) => {
    const node = id ? document.getElementById(id) : document.querySelectorAll('.pane')[index];
    if (node) node.scrollTop = offset;
  });
  if (activeId && activeId !== 'research-language') document.getElementById(activeId)?.focus({
    preventScroll: true
  });
}
async function fileBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}
function researchEncodingOptions() {
  return [['auto', '自动识别编码'], ['utf-8', 'UTF-8'], ['utf-8-sig', 'UTF-8 BOM'], ['utf-16', 'UTF-16'], ['utf-16-le', 'UTF-16 LE'], ['utf-16-be', 'UTF-16 BE'], ['utf-32', 'UTF-32'], ['utf-32-le', 'UTF-32 LE'], ['utf-32-be', 'UTF-32 BE'], ['gb18030', 'GB18030'], ['gbk', 'GBK'], ['gb2312', 'GB2312'], ['big5', 'Big5'], ['big5hkscs', 'Big5 HKSCS'], ['cp950', 'CP950'], ['cp1252', 'Windows-1252'], ['iso8859-1', 'ISO-8859-1'], ['iso8859-15', 'ISO-8859-15'], ['shift_jis', 'Shift-JIS'], ['cp932', 'Windows-932'], ['euc_jp', 'EUC-JP'], ['iso2022_jp', 'ISO-2022-JP'], ['cp1251', 'Windows-1251'], ['koi8-r', 'KOI8-R'], ['iso8859-5', 'ISO-8859-5']].map(([value, label]) => `<option value="${value}">${tr(label)}</option>`).join('');
}
