document
  .getElementById('ui-language')
  .addEventListener('change', changeLanguage);
// One controller handles navigation, actions and recovery.
let mutationPending = false;
let lastDraftProject = null;
const receiptedActions = new Set([
  'confirm_extraction',
  'confirm_context',
  'retry_extraction',
  'run_local_prechecks',
  'prepare_ai_audits',
  'import_ai_audit',
  'prepare_adversarial_review',
  'prepare_adversarial_assessment',
  'import_adversarial_response',
  'export_ai_reviews',
  'decide_finding',
  'correct_finding_location',
  'decide_finding_batch',
  'import_revision_draft',
  'prepare_bridge',
  'propose_revision_hunk',
  'set_revision_action_operation',
  'decide_revision_hunk',
  'finalize_revision',
  'import_external_recheck',
  'decide_external_resolution',
  'start_followup_round',
  'export',
]);
function render() {
  renderLanguageHeader();
  clearTimeout(draftTimer);
  draftTimer = null;
  critics = state.selected?.review_critics || reviewConfig.critics;
  loadDraft();
  if (state.selected) project();
  else home();
  bind();
  bindRevision();
  bindAdversarial();
  bindImportControls();
  bindDelivery();
  bindWorkflowNavigation();
  document
    .getElementById('export-ai-reviews')
    ?.addEventListener('click', () => act('export_ai_reviews'));
  restoreDraftFields();
  if (state.selected?.directory !== lastDraftProject) {
    lastDraftProject = state.selected?.directory;
    requestAnimationFrame(() => window.scrollTo(0, draftContext?.scroll || 0));
  }
}
async function runMutation(label, operation) {
  if (mutationPending) return;
  mutationPending = true;
  root.classList.add('busy');
  root.inert = true;
  root.setAttribute('aria-busy', 'true');
  const started = Date.now();
  const progress = setInterval(
    () =>
      operationStatus(
        ui`${label}正在处理（${Math.floor((Date.now() - started) / 1000)} 秒），请保留页面…`,
        'working',
      ),
    1000,
  );
  try {
    await saveDraft();
    operationStatus(ui`${label}正在处理，请稍候…`, 'working');
    state = await operation();
    if (state.closed) {
      draftContext = null;
      root.innerHTML = tr(
        '<div class="card"><h2>工作台已关闭</h2><p>项目和草稿已保存在本机，可以关闭此页面。</p></div>',
      );
    } else render();
    operationStatus(
      state.operation_uncertain ? state.notice : ui`${label}已完成。`,
      state.operation_uncertain ? 'failure' : 'success',
    );
  } catch (error) {
    // Preserve visible input on failures, including saves from another tab.
    operationStatus(ui`${label}失败：${error.message}`, 'failure');
    errorMessage(error.message);
  } finally {
    clearInterval(progress);
    mutationPending = false;
    root.inert = false;
    root.setAttribute('aria-busy', 'false');
    root.classList.remove('busy');
  }
}
async function act(action, data = {}) {
  const body = {
    action,
    data,
    request_id: crypto.randomUUID(),
    project_directory: state.selected?.directory,
    document_scope: state.selected?.ui_draft?.scope,
  };
  return runMutation(tr(actionLabels[action] || '操作'), async () => {
    try {
      return await api('/api/action', body);
    } catch (error) {
      if (!error.transportFailure && !error.uncertainMutation) throw error;
      // The same receipt ID is safe to replay after an interrupted response.
      if (receiptedActions.has(action)) {
        try {
          return await api('/api/action', body);
        } catch (retryError) {
          if (!retryError.transportFailure && !retryError.uncertainMutation)
            throw retryError;
        }
      }
      const refreshed = await api('/api/state');
      refreshed.notice = tr(
        '上一步响应中断；已重新读取项目状态，请核对操作是否完成。',
      );
      refreshed.operation_uncertain = true;
      return refreshed;
    }
  });
}
async function openProject(directory) {
  return runMutation(tr('打开项目'), () => api('/api/open', { directory }));
}
async function uploadFile() {
  const file = document.getElementById('file').files[0];
  if (!file) return errorMessage(tr('请选择文件'));
  const title = document.getElementById('title').value;
  const encoding = document.getElementById('import-encoding').value;
  const ocr_language = document.getElementById('import-ocr-language').value;
  return runMutation(tr('导入文档'), async () => {
    if (file.size > 30 * 1024 * 1024)
      throw Error(tr('浏览器导入支持 30 MiB 以内文件'));
    let binary = '';
    for (const byte of new Uint8Array(await file.arrayBuffer()))
      binary += String.fromCharCode(byte);
    return api('/api/upload', {
      filename: file.name,
      title,
      content_base64: btoa(binary),
      encoding,
      ocr_language,
    });
  });
}
async function removeProject(directory) {
  if (confirm(tr('删除前会自动创建可恢复备份。确定删除这个项目吗？'))) {
    await act('delete_project', { directory });
  }
}
async function quitStudio() {
  return runMutation(tr('退出工作台'), () => api('/api/shutdown', {}));
}
api('/api/state')
  .then((value) => {
    state = value;
    if (value.ui_language === 'en' || value.ui_language === 'zh-Hant') uiLocale = value.ui_language;
    render();
  })
  .catch((error) => {
    root.innerHTML = `<div class="card error">${esc(error.message)}</div>`;
  });
