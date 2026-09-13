const TOKEN = __TOKEN__,
  el = document.getElementById('app');
let state;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
})[c]);
async function api(path, body) {
  markResearchMutation(1);
  document.body.inert = true;
  try {
    let r;
    try {
      r = await fetch(path, {
        method: body ? 'POST' : 'GET',
        headers: {
          'Content-Type': 'application/json',
          'X-Argument-Workbench-Token': TOKEN,
          ...(body && state?.request_context ? {'X-Argument-Project-Context': state.request_context} : {})
        },
        body: body ? JSON.stringify(body) : undefined
      });
    } catch (cause) {
      const e = Error(tr('无法连接本地服务，请确认本次启动的页面仍然有效'));
      e.transportFailure = true;
      throw e;
    }
    let j;
    try {
      j = await r.json();
    } catch (cause) {
      const e = Error(tr('本地服务返回了无法识别的响应'));
      e.transportFailure = true;
      throw e;
    }
    if (!r.ok) {
      const e = Error(j.error ? tr(j.error) : ui`请求失败（${r.status}）`);
      e.uncertainMutation = !!body && r.status >= 500;
      throw e;
    }
    return j;
  } finally {
    markResearchMutation(-1);
    document.body.inert = researchMutationPending > 0;
  }
}
async function showMutationError(e) {
  const uncertain = e.transportFailure || e.uncertainMutation;
  if (uncertain) {
    try {
      state = await api('/api/state');
      render();
    } catch (ignore) {}
  }
  const message = uncertain ? tr('本地服务连接中断或发生内部错误；页面已尝试刷新。操作可能已经完成，请先检查当前状态，不要直接重复提交。') : e.message;
  const x = document.getElementById('err');
  if (x) x.textContent = message;else alert(message);
}
async function act(action, data = {}) {
  try {
    state = await api('/api/action', {
      action,
      data
    });
    render();
  } catch (e) {
    await showMutationError(e);
  }
}
function copyText(value) {
  navigator.clipboard.writeText(value).catch(() => {});
}
function errors(attempt) {
  return attempt && !attempt.valid ? ui`<div class="error"><b>这次返回未通过校验，原始内容已保留。</b><ul>${attempt.errors.map(e => ui`<li>${esc(e)}</li>`).join('')}</ul>${attempt.repair_prompt ? tr('<button class="secondary" id="copyRepair">复制修复提示词</button>') : ''}</div>` : '';
}
function promptPaste(title, prompt, attempt, action) {
  return ui`<div class="card"><h2>${esc(title)}</h2><p>复制提示词，到任意 AI 运行，再把完整返回粘贴回来。</p><p><button id="copyPrompt">复制提示词</button></p><textarea id="response" placeholder="在这里粘贴 AI 返回">${esc(attempt && !attempt.valid ? attempt.raw : '')}</textarea><p><button id="submitResponse">校验并保存返回</button></p>${errors(attempt)}<div id="err" class="error"></div></div>`;
}
function home() {
  el.innerHTML = ui`<div class="card"><h2>新建项目</h2><p class="muted">选择 Markdown/TXT 原稿。文件只保存在本机。</p><label>项目标题（可选）</label><input id="title" type="text"><label>原稿</label><input id="file" type="file" accept=".md,.txt,text/plain,text/markdown"><label for="encoding">文本编码</label><select id="encoding">${researchEncodingOptions()}</select><p class="muted">若编码存在歧义，请选择原文件编码。</p><p><button id="create">导入为不可变 V1</button></p><div id="err" class="error"></div></div>${state.projects.length ? ui`<div class="card"><h2>打开已有项目</h2>${state.projects.map(p => ui`<p class="row"><button class="secondary open" data-dir="${esc(p.path.split(/[\\/]/).pop())}">打开</button><span>${esc(p.title)} · ${esc(p.current_version || tr('校验失败'))}</span></p>`).join('')}</div>` : ''}`;
  document.getElementById('create').onclick = async () => {
    try {
      const f = document.getElementById('file').files[0];
      if (!f) throw Error(tr('请选择稿件'));
      const encoding = document.getElementById('encoding').value,
        title = document.getElementById('title').value;
      state = await api('/api/projects', {
        filename: f.name,
        content_base64: await fileBase64(f),
        encoding,
        title
      });
      render();
    } catch (e) {
      await showMutationError(e);
    }
  };
  document.querySelectorAll('.open').forEach(b => b.onclick = async () => {
    try {
      state = await api('/api/open', {
        directory: b.dataset.dir
      });
      render();
    } catch (e) {
      await showMutationError(e);
    }
  });
}
function bindPrompt(prompt, attempt, action) {
  document.getElementById('copyPrompt').onclick = () => copyText(prompt);
  document.getElementById('submitResponse').onclick = () => act(action, {
    response: document.getElementById('response').value
  });
  const repair = document.getElementById('copyRepair');
  if (repair) repair.onclick = () => copyText(attempt.repair_prompt);
}
function resolutionCard(result) {
  const decision = result.human_decision;
  return ui`<div class="card"><span class="pill">${esc(result.finding_id)}</span><p class="muted">模型建议</p><h3>${esc(systemLabel(result.proposed_status))}</h3><p>${esc(result.reason)}</p>${result.evidence_quotes?.length ? ui`<p class="muted">复查原文依据</p>${result.evidence_quotes.map(quote => `<div class="quote">${esc(quote)}</div>`).join('')}` : ''}${result.uncertainties?.length ? ui`<p class="warning">未确认：${esc(result.uncertainties.join('；'))}</p>` : ''}${decision ? ui`<p class="muted">已保存人工结论：${esc(systemLabel(decision.final_status))}</p>` : ''}<label for="status-${esc(result.finding_id)}">最终状态</label><select id="status-${esc(result.finding_id)}" required><option value="" ${decision ? '' : 'selected'}>${tr('请选择最终状态')}</option>${['resolved', 'partially_resolved', 'unresolved', 'not_evaluated'].map(value => `<option value="${value}" ${decision?.final_status === value ? 'selected' : ''}>${esc(systemLabel(value))}</option>`).join('')}</select><label for="rreason-${esc(result.finding_id)}">你的确认理由</label><input id="rreason-${esc(result.finding_id)}" type="text" value="${esc(decision?.reason || '')}" required><p class="muted">请选择最终状态并填写理由，模型建议不会自动成为人工结论。</p><button class="resolution" data-id="${esc(result.finding_id)}" disabled>保存人工结论</button></div>`;
}
function bindResolutionControls() {
  document.querySelectorAll('.resolution').forEach(button => {
    const id = button.dataset.id;
    const status = document.getElementById('status-' + id);
    const reason = document.getElementById('rreason-' + id);
    const update = () => { button.disabled = !status.value || !reason.value.trim(); };
    status.onchange = update;
    reason.oninput = update;
    button.onclick = () => {
      update();
      if (button.disabled) return;
      act('decide_resolution', {finding_id: id, status: status.value, reason: reason.value});
    };
    update();
    // Language switching restores field values after render() returns.
    queueMicrotask(update);
  });
}
function render() {
  if (!state.selected) {
    home();
    return;
  }
  const p = state.selected;
  let body = ui`<div class="card"><div class="muted">当前项目 · ${esc(p.current_version)}</div><h2>${esc(p.title)}</h2><p>${esc(p.source_name)} · <code>${esc(p.source_sha256.slice(0, 12))}</code></p>${p.professional_available ? tr('<p><a href="/professional">进入专业研究视图（IR、Lens、Citation、lineage）</a></p>') : ''}</div><div class="card next"><div class="muted">唯一下一步</div><h2>${esc(systemLabel(p.next_action))}</h2><p>V1 永久保留；模型只能提案，决定权在你。</p></div>`;
  if (p.stage === 'read_only') body += ui`<div class="card error"><h2>修改链校验失败</h2><p>项目已强制进入只读状态。修复下列完整性问题前，所有写入操作都会被拒绝。</p><ul>${p.errors.map(e => ui`<li>${esc(e)}</li>`).join('')}</ul></div>`;else if (p.stage === 'review_material') body += ui`<div class="card"><h2>导入现有审查报告</h2><p class="muted">支持任意格式。原始报告会永久归档。</p><textarea id="report" placeholder="粘贴 AI 审查报告"></textarea><p><button id="importReport">导入并生成原子化提示词</button></p><div id="err" class="error"></div></div>`;else if (p.stage === 'atomization_prepare') body += ui`<div class="card"><h2>继续分析审查报告</h2><p>原始报告已保存。继续准备提示词即可恢复此步骤。</p><button id="prepareAtomization">生成报告原子化提示词</button><div id="err" class="error"></div></div>`;else if (p.stage === 'atomization_result') body += promptPaste(tr('把报告拆成可核验的发现'), p.atomization_prompt, p.atomization_attempt, 'collect_atomization');else if (p.stage === 'findings_confirm') body += ui`<div class="card"><h2>逐条确认发现</h2><p>UNVERIFIED 不会被当成事实。可以直接修正定位、标准和建议动作。</p></div>${p.findings.map(f => ui`<div class="card"><div class="row"><span class="pill">${esc(f.finding_id)}</span><span class="pill">${esc(f.claim_id)}</span><span class="pill">${esc(systemLabel(f.evidence_level))}</span></div><label>问题</label><textarea id="assert-${esc(f.finding_id)}">${esc(f.assertion)}</textarea><div class="grid"><div><label>原文定位</label><textarea id="quote-${esc(f.finding_id)}">${esc(f.manuscript_quote || '')}</textarea></div><div><label>审查标准</label><textarea id="criterion-${esc(f.finding_id)}">${esc(f.criterion)}</textarea></div></div><label>建议动作</label><textarea id="action-${esc(f.finding_id)}">${esc(f.suggested_action)}</textarea><label>你的理由</label><input id="reason-${esc(f.finding_id)}" type="text"><div class="row"><button class="finding" data-id="${esc(f.finding_id)}" data-decision="accept">接受处理</button><button class="finding danger" data-id="${esc(f.finding_id)}" data-decision="reject">拒绝</button><button class="finding secondary" data-id="${esc(f.finding_id)}" data-decision="defer">暂缓</button>${f.decision ? ui`<span>当前：${esc(systemLabel(f.decision))}</span>` : ''}</div></div>`).join('')}<div id="err" class="error"></div>`;else if (p.stage === 'no_revision') body += ui`<div class="card"><h2>${p.completion_kind === 'no_findings' ? tr('本轮没有发现') : tr('本轮没有选中修改项')}</h2><p>${p.completion_kind === 'no_findings' ? tr('可以保留零 finding 结果并合法结束，不创建伪造的 V2。') : tr('所有 finding 均已拒绝或暂缓，可以保留决定链并结束本轮。')}</p><label>完成理由</label><input id="noRevisionReason" type="text"><p><button id="completeNoRevision">确认并生成审计包</button></p><div id="err" class="error"></div></div>`;else if (p.stage === 'revision_prepare') body += ui`<div class="card"><h2>只为已接受的问题生成方案</h2><p>拒绝和暂缓的发现不会进入提示词。</p><button id="prepareRevision">生成受约束修改提示词</button><div id="err" class="error"></div></div>`;else if (p.stage === 'revision_result') body += promptPaste(tr('获取受约束修改提案'), p.revision_prompt, p.revision_attempt, 'collect_revision');else if (p.stage === 'hunk_review') body += ui`<div class="card"><h2>逐项审批 diff</h2><p>每一项都显示 Finding、Action、理由和不确定项。</p></div>${p.regeneration_prompt ? ui`<div class="card next"><h2>重新生成指定项</h2><p>复制下面的定向提示词。新提案通过校验后，因 proposal hash 已变化，所有 hunk 都必须重新审批。</p><button id="copyRegen">复制定向提示词</button><textarea id="regenResponse" placeholder="粘贴完整的新提案"></textarea><p><button id="submitRegen">校验新提案</button></p></div>` : ''}${p.hunks.map(h => ui`<div class="card hunk"><div>${h.finding_ids.map(x => ui`<span class="pill">Finding ${esc(x)}</span>`).join('')}${h.action_ids.map(x => ui`<span class="pill">Action ${esc(x)}</span>`).join('')}</div><div class="grid"><div><h3>原文</h3><div class="quote original">${esc(h.original_quote || ui`插入锚点：${h.insertion_anchor}`)}</div></div><div><h3>建议</h3><textarea class="replacement" id="edit-${esc(h.change_id)}">${esc(h.replacement_text)}</textarea></div></div><p>${esc(h.reason)}</p>${h.uncertainties.length ? ui`<p class="warning">未确认：${esc(h.uncertainties.join('；'))}</p>` : ''}${h.fact_change ? ui`<p class="warning">事实/引文变化，需核验：${esc(h.verification_note)}</p>` : ''}<label>决定理由</label><input id="hreason-${esc(h.change_id)}" type="text"><div class="row"><button class="hunkDecision" data-id="${esc(h.change_id)}" data-decision="accept">接受</button><button class="hunkDecision danger" data-id="${esc(h.change_id)}" data-decision="reject">拒绝</button><button class="hunkDecision secondary" data-id="${esc(h.change_id)}" data-decision="edit">编辑后接受</button><button class="hunkDecision secondary" data-id="${esc(h.change_id)}" data-decision="regenerate">重新生成此项</button>${h.decision ? ui`<span>当前：${esc(systemLabel(h.decision.decision))}</span>` : ''}</div></div>`).join('')}<div id="err" class="error"></div>`;else if (p.stage === 'apply_revision') body += ui`<div class="card"><h2>生成不可变 V2</h2><p>只应用已批准的 hunks；拒绝项绝不会进入 V2。哈希或范围冲突会安全停止。</p><button id="applyRevision">确定生成 V2</button><div id="err" class="error"></div></div>`;else if (p.stage === 'resolution_prepare') body += ui`<div class="card"><h2>复查 V2</h2><p>复用每条 finding 的原始审查标准，不因“文字变了”就宣称已解决。</p><button id="prepareResolution">生成复查提示词</button><div id="err" class="error"></div></div>`;else if (p.stage === 'resolution_result') body += promptPaste(tr('用原标准复查 V2'), p.resolution_prompt, p.resolution_attempt, 'collect_resolution');else if (p.stage === 'resolution_confirm') body += ui`<div class="card"><h2>确认复查结论</h2></div>${p.resolution_results.map(resolutionCard).join('')}<div id="err" class="error"></div>`;else if (p.stage === 'export') body += ui`<div class="card"><h2>导出文章与审计记录</h2><button id="export">生成导出包</button><div id="err" class="error"></div></div>`;else if (p.stage === 'complete') body += ui`<div class="card"><h2>闭环完成</h2><p>${p.completion ? tr('原稿、无修改结论和完整审计记录已生成；没有创建 V2。') : tr('V2、修订清单和完整审计记录已生成。')}</p><p><code>${esc(p.export_path)}</code></p></div>`;
  el.innerHTML = body + ui`<div class="card"><p class="muted">本地存储：${esc(state.storage_path)}</p></div>`;
  if (p.stage === 'review_material') document.getElementById('importReport').onclick = () => act('import_report', {
    report: document.getElementById('report').value,
    source_name: 'pasted-report.md'
  });
  if (p.stage === 'atomization_prepare') document.getElementById('prepareAtomization').onclick = () => act('prepare_atomization');
  if (p.stage === 'atomization_result') bindPrompt(p.atomization_prompt, p.atomization_attempt, 'collect_atomization');
  if (p.stage === 'findings_confirm') document.querySelectorAll('.finding').forEach(b => b.onclick = () => {
    const id = b.dataset.id,
      decision = b.dataset.decision;
    act('decide_finding', {
      finding_id: id,
      decision,
      reason: document.getElementById('reason-' + id).value,
      action_text: document.getElementById('action-' + id).value,
      corrections: {
        assertion: document.getElementById('assert-' + id).value,
        manuscript_quote: document.getElementById('quote-' + id).value || null,
        criterion: document.getElementById('criterion-' + id).value,
        suggested_action: document.getElementById('action-' + id).value
      }
    });
  });
  if (p.stage === 'no_revision') document.getElementById('completeNoRevision').onclick = () => act('complete_without_revision', {
    reason: document.getElementById('noRevisionReason').value
  });
  if (p.stage === 'revision_prepare') document.getElementById('prepareRevision').onclick = () => act('prepare_revision');
  if (p.stage === 'revision_result') bindPrompt(p.revision_prompt, p.revision_attempt, 'collect_revision');
  if (p.stage === 'hunk_review') {
    document.querySelectorAll('.hunkDecision').forEach(b => b.onclick = () => {
      const id = b.dataset.id,
        d = b.dataset.decision;
      act('decide_hunk', {
        change_id: id,
        decision: d,
        reason: document.getElementById('hreason-' + id).value,
        edited_text: d === 'edit' ? document.getElementById('edit-' + id).value : null
      });
    });
    if (p.regeneration_prompt) {
      document.getElementById('copyRegen').onclick = () => copyText(p.regeneration_prompt);
      document.getElementById('submitRegen').onclick = () => act('collect_revision', {
        response: document.getElementById('regenResponse').value
      });
    }
  }
  if (p.stage === 'apply_revision') document.getElementById('applyRevision').onclick = () => act('apply_revision');
  if (p.stage === 'resolution_prepare') document.getElementById('prepareResolution').onclick = () => act('prepare_resolution');
  if (p.stage === 'resolution_result') bindPrompt(p.resolution_prompt, p.resolution_attempt, 'collect_resolution');
  if (p.stage === 'resolution_confirm') bindResolutionControls();
  if (p.stage === 'export') document.getElementById('export').onclick = () => act('export');
}
function renderResearchView() {
  if (state) render();
}
applyResearchLanguage();
api('/api/state').then(x => {
  state = x;
  render();
}).catch(e => el.innerHTML = ui`<div class="card error">${esc(e.message)}</div>`);
