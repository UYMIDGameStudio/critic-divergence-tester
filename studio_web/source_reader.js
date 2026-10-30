// Current extracted source stays available independently of the Finding count.
function sourceReaderNavigation(blocks) {
  const headings = blocks.filter(block => block.kind === 'heading' && block.text?.trim());
  const pages = new Map();
  for (const block of blocks) {
    const page = block.location?.page;
    if (Number.isInteger(page) && page > 0 && !pages.has(page)) pages.set(page, block.block_id);
  }
  const headingList = headings.length ? ui`<details class="source-outline"><summary>已识别标题 · ${headings.length}</summary><nav aria-label="${esc(tr('已识别标题'))}"><ol>${headings.map(block => {
    const level = Number.isInteger(block.level) && block.level > 0 ? Math.min(block.level, 9) : 1;
    const title = block.text.replace(/\s+/g, ' ').trim();
    const label = title.length > 160 ? title.slice(0, 160) + '…' : title;
    return `<li style="--source-depth:${level - 1}"><a href="#source-${esc(block.block_id)}" title="${esc(title)}">${esc(label)}</a></li>`;
  }).join('')}</ol></nav></details>` : ui`<p class="muted source-navigation-help">未识别出章节标题，可搜索正文或定位编号。</p>`;
  const pageList = pages.size ? ui`<details class="source-pages"><summary>来源页码 · ${pages.size}</summary><nav aria-label="${esc(tr('来源页码'))}">${[...pages].map(([page, id]) => `<a href="#source-${esc(id)}" class="pill">${page}</a>`).join('')}</nav></details>` : '';
  return `<div class="source-reader-navigation">${headingList}${pageList}</div>`;
}

function sourceReaderPane(view, standalone = false) {
  const blocks = view.extraction?.blocks || [];
  return ui`<section class="${standalone ? '' : 'card '}pane manuscript-pane"><div class="source-reader-toolbar"><h3>原文与定位</h3><div class="source-reader-controls"><input id="workspace-search" type="search" aria-label="${esc(tr('搜索原文'))}" placeholder="搜索原文"><button type="button" id="clear-source-search" class="secondary" disabled>清除搜索</button></div><p id="source-search-status" class="muted source-reader-status" role="status" aria-live="polite">${esc(tr('搜索结果'))} · ${blocks.length} / ${blocks.length}</p>${sourceReaderNavigation(blocks)}</div>${blocks.map(block => {
    const heading = block.kind === 'heading';
    const level = Number.isInteger(block.level) && block.level > 0 && block.level <= 9 ? block.level : null;
    return `<div class="source-block${heading ? ' source-kind-heading' : ''}" id="source-${esc(block.block_id)}"><small>${esc(block.block_id)} · page ${esc(block.location?.page || '-')}</small><div class="source-text"${heading && level ? ` role="heading" aria-level="${level}"` : ''}>${pdfDisplayText(sourceBlockText(block))}</div></div>`;
  }).join('')}</section>`;
}

function standaloneSourceReader(view) {
  if (view.findings?.length || !view.extraction?.available || view.extraction.reviewable === false
      || ['blocked', 'unconfirmed', 'replacement_required'].includes(view.state.extraction_state)) return '';
  return ui`<details class="card standalone-reader" id="manuscript-reader"><summary>原文阅读与章节导航</summary>${sourceReaderPane(view, true)}</details>`;
}

function filterWorkspace() {
  const search = document.getElementById('workspace-search');
  const pane = root.querySelector('.manuscript-pane');
  if (!search || !pane) return;
  const query = search.value.toLowerCase();
  const blocks = [...pane.querySelectorAll('.source-block')];
  let visible = 0;
  for (const block of blocks) {
    block.hidden = !sourceMatchesQuery(block.textContent, query);
    if (!block.hidden) visible++;
  }
  const status = document.getElementById('source-search-status');
  if (status) status.textContent = `${tr('搜索结果')} · ${visible} / ${blocks.length}`
    + (!visible && query ? ` · ${tr('没有匹配内容，请更换关键词或清除搜索。')}` : '');
  const clear = document.getElementById('clear-source-search');
  if (clear) clear.disabled = !search.value;
}

function captureSourceReaderState() {
  if (!draftContext || state.selected?.state.read_only) return false;
  const fields = draftContext.fields;
  const reader = document.getElementById('manuscript-reader');
  const pane = root.querySelector('.manuscript-pane');
  const values = {};
  if (reader && (reader.open || Object.hasOwn(fields, 'source-reader-open')))
    values['source-reader-open'] = reader.open;
  if (pane?.getClientRects().length) {
    const scroll = String(Math.min(1_000_000_000, Math.max(0, Math.round(pane.scrollTop))));
    if (scroll !== '0' || Object.hasOwn(fields, 'source-reader-scroll')) values['source-reader-scroll'] = scroll;
  }
  let changed = false;
  for (const [key, value] of Object.entries(values)) {
    if (fields[key] !== value) {fields[key] = value; changed = true;}
  }
  return changed;
}

function rememberSourceReader() {
  if (captureSourceReaderState()) {
    draftContext.sequence++;
    scheduleDraft();
  }
}

function applySourceReaderScroll(pane) {
  if (!root.contains(pane) || !pane.getClientRects().length) return;
  const value = draftContext?.fields['source-reader-scroll'];
  if (typeof value === 'string' && /^\d{1,10}$/.test(value) && Number(value) <= 1_000_000_000)
    pane.scrollTop = Math.min(Number(value), Math.max(0, pane.scrollHeight - pane.clientHeight));
}

function restoreSourceReaderState() {
  const reader = document.getElementById('manuscript-reader');
  const value = draftContext?.fields['source-reader-open'];
  if (reader && typeof value === 'boolean') reader.open = value;
  const pane = root.querySelector('.manuscript-pane');
  if (pane) requestAnimationFrame(() => applySourceReaderScroll(pane));
  filterWorkspace();
}

function bindSourceReader() {
  document.getElementById('clear-source-search')?.addEventListener('click', () => {
    const search = document.getElementById('workspace-search');
    search.value = '';
    search.dispatchEvent(new Event('input', {bubbles: true}));
    search.focus();
  });
  const pane = root.querySelector('.manuscript-pane');
  pane?.addEventListener('scroll', () => {if (root.contains(pane)) rememberSourceReader();}, {passive: true});
  const reader = document.getElementById('manuscript-reader');
  reader?.addEventListener('toggle', () => {
    if (!root.contains(reader)) return;
    if (reader.open && !reader.dataset.sourceNavigation && pane) applySourceReaderScroll(pane);
    delete reader.dataset.sourceNavigation;
    rememberSourceReader();
  });
}

function focusSourceTarget(target) {
  if (!target?.classList.contains('source-block') || !root.contains(target)) return false;
  const pane = target.closest('.manuscript-pane');
  pane?.querySelectorAll('.source-reader-navigation details[open]').forEach(details => {details.open = false;});
  const search = target.classList.contains('revised-source-block') ? null : document.getElementById('workspace-search');
  if (target.hidden && search) {
    search.value = '';
    search.dispatchEvent(new Event('input', {bubbles: true}));
  }
  let ancestor = target;
  while (ancestor && ancestor !== root) {
    ancestor.hidden = false;
    ancestor.classList.remove('hidden');
    if (ancestor.tagName === 'DETAILS') {
      if (ancestor.id === 'manuscript-reader' && !ancestor.open) ancestor.dataset.sourceNavigation = 'true';
      ancestor.open = true;
    }
    ancestor = ancestor.parentElement;
  }
  target.tabIndex = -1;
  target.focus({preventScroll: true});
  if (pane) {
    pane.scrollIntoView({block: 'center'});
    const offset = target.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
    const toolbar = pane.querySelector('.source-reader-toolbar');
    const reserved = toolbar ? toolbar.getBoundingClientRect().bottom - pane.getBoundingClientRect().top : 0;
    pane.scrollTop = Math.max(0, offset - reserved - 12);
  } else target.scrollIntoView({block: 'center'});
  rememberSourceReader();
  return true;
}

function revealSource(event) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const fragment = event.currentTarget.getAttribute('href').slice(1);
  if (focusSourceTarget(document.getElementById(fragment))) {
    event.preventDefault();
    const href = event.currentTarget.getAttribute('href');
    if (location.hash !== href) history.pushState(null, '', href);
  }
}

function revealSourceFragment() {
  try {
    return focusSourceTarget(document.getElementById(decodeURIComponent(location.hash.slice(1))));
  } catch (_) {return false;}
}
