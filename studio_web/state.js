const TOKEN = __TOKEN__,
  root = document.getElementById('app'),
  operationBox = document.getElementById('operation-status');
let state = null;
const reviewConfig = __REVIEW_CONFIG__;
let critics = reviewConfig.critics;
const actionLabels = {
  run_local_prechecks: '本地确定性预检',
  prepare_ai_audits: 'AI 审查协议生成',
  import_ai_audit: 'AI 审查导入',
  prepare_adversarial_review: '独立辩护任务生成',
  prepare_adversarial_assessment: '证据复核任务生成',
  import_adversarial_response: '深审响应导入',
  confirm_extraction: '识别确认',
  confirm_context: '上下文确认',
  prepare_bridge: '修改计划生成',
  finalize_revision: '修改稿生成与复审',
  export: '导出',
};
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[
        c
      ],
  );

// Presentation only: evidence and editable manuscript text remain unchanged.
function sourceBlockText(block) {
  const text = String(block.text ?? ''), attrs = block.attrs || {};
  const marker = typeof attrs.list_marker === 'string'
    && (attrs.ordered ? /^(?:-?\p{Nd}+[.)]|[A-Za-z]{1,64}\.)$/u : /^[-*+]$/u).test(attrs.list_marker)
    ? attrs.list_marker : '';
  if (!marker) return text;
  const start = block.kind === 'list_item' || attrs.list_item_start === true;
  const continuation = attrs.list_continuation === true && !start;
  if (!start && !continuation) return text;
  const depth = Number.isInteger(attrs.list_depth) ? Math.max(0, Math.min(8, attrs.list_depth)) : 0;
  const label = continuation ? `↳ ${marker} (${uiLocale === 'en' ? 'continued' : '續文'})` : marker;
  return '  '.repeat(depth) + label + ' ' + text;
}

// Reader preference only: never change extracted text, evidence or locations.
let preservePdfLineBreaks = false;
function hasFragmentedPdfLines(text) {
  const lines = String(text ?? '').split(/\r\n|\r|\n/);
  const nonempty = lines.map(line => line.trim()).filter(Boolean);
  return nonempty.length >= 12
    && nonempty.filter(line => !/\s/u.test(line) && /[A-Za-z\u00c0-\u02af\u0370-\u052f]/u.test(line)).length / nonempty.length >= 0.75
    && lines.filter(line => /^[\t ]+$/.test(line)).length >= Math.max(3, Math.ceil(nonempty.length / 4));
}
function isPypdfReader(view = state?.selected) {
  // Older views omit the parser field. The shared warning code alone is not
  // sufficient: its original backend message must explicitly identify pypdf.
  const extraction = view?.extraction;
  const parser = extraction?.parser_name ?? extraction?.metadata?.parser_name;
  const pypdf = parser ? parser === 'pypdf' : (extraction?.warnings || []).some(warning =>
    warning.code === 'pdf-coordinates-unavailable' && /\bpypdf\b/i.test(warning.message || ''));
  return /\.pdf$/i.test(view?.project?.source?.name || '')
    && ['text', 'mixed'].includes(extraction?.metadata?.pdf_kind)
    && extraction.metadata.coordinates_available === false && pypdf;
}
function sourceMatchesQuery(text, query) {
  // Normalize a search copy only; stored strings and evidence remain verbatim.
  const normalize = value => String(value ?? '').replace(/\s+/gu, ' ').trim().toLowerCase();
  return normalize(text).includes(normalize(query));
}
function pdfDisplayText(text) {
  const escaped = esc(text);
  return isPypdfReader() && hasFragmentedPdfLines(text)
    ? `<span class="pdf-fragmented-text">${escaped}</span>` : escaped;
}
function pdfReaderControl(view) {
  if (!isPypdfReader(view) || !(view.extraction.blocks || []).some(block => hasFragmentedPdfLines(block.text))) return '';
  const notice = uiLocale === 'en'
    ? 'Detected word-by-word PDF line breaks. Readable spacing changes display only; source text and quotations stay unchanged.'
    : '偵測到 PDF 逐詞換行。可讀顯示只調整畫面空白；原文與引文保持不變。';
  return `<div class="pdf-reader-control"><small class="muted">${notice}</small> <button type="button" class="secondary" id="pdf-line-breaks-toggle" aria-pressed="${preservePdfLineBreaks}">${pdfReaderToggleLabel()}</button></div>`;
}
function pdfReaderToggleLabel() {
  return preservePdfLineBreaks
    ? (uiLocale === 'en' ? 'Use readable spacing' : '使用可讀間距')
    : (uiLocale === 'en' ? 'Show original extracted breaks' : '顯示原始擷取換行');
}
function bindPdfReader() {
  root.classList.toggle('pdf-original-breaks', preservePdfLineBreaks);
  document.getElementById('pdf-line-breaks-toggle')?.addEventListener('click', event => {
    preservePdfLineBreaks = !preservePdfLineBreaks;
    root.classList.toggle('pdf-original-breaks', preservePdfLineBreaks);
    event.currentTarget.setAttribute('aria-pressed', String(preservePdfLineBreaks));
    event.currentTarget.textContent = pdfReaderToggleLabel();
  });
}
