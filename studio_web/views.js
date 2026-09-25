function researchLibrary() {
  if (!state.unified) return '';
  return ui`<div class="card"><h2>专业论证图谱与历史项目</h2><p class="muted">新稿审查支持文书和学术稿件。专业项目保留主张、审查标准、引用核验和版本关系；建立论证图谱需先完成主张提取与确认。</p>${(state.research_projects || []).map((p) => ui`<div class="project-row"><span>${esc(p.title)}</span><button class="secondary open-research" data-dir="${esc(p.directory)}" ${p.invalid ? 'disabled' : ''}>打开专业研究项目</button></div>`).join('')}<p><a href="/research/">打开专业研究与历史项目</a></p></div>`;
}
function dependencyCard() {
  const missing = (state.dependencies || []).filter((item) => !item.available);
  return ui`<div class="card"><h2>环境自检</h2><p class="muted">Python 适配器可一键安装并重新自检；如果当前项目因缺少组件而识别失败，修复后会自动重新识别。系统级 OCR 组件会给出人工安装说明。</p>${missing.length ? ui`<p><button id="repair-all">一键修复可自动修复项</button> <span id="repair-progress" class="muted"></span></p>` : tr('<p class="ok">可选环境组件均已就绪。</p>')}${(state.dependencies || []).map((item) => `<div class="dependency"><span class="${item.available ? 'ok' : 'warning'}"><b>${esc(item.name)}</b>：${item.available ? tr('可用') : tr('缺失')} · ${esc(tr(item.purpose))}${item.repair_hint ? `<small><br>${esc(tr(item.repair_hint))}</small>` : ''}</span>${!item.available && item.repairable ? ui`<button class="secondary repair-one" data-name="${esc(item.repair_key)}">修复</button>` : ''}</div>`).join('')}</div>`;
}
function home() {
  root.innerHTML = ui`${serviceDetails(state.notice)}${state.unified ? ui`<div class="card"><h2>选择这次的任务</h2><div class="row"><a id="start-new-review" href="#new-review">新稿审查</a><a id="start-report-revision" href="/research/">已有审查报告，直接修稿</a></div><p class="muted">已有报告修稿支持 Markdown/TXT 原稿；模型提出修改，你逐项批准并复查。</p></div>` : ''}<div class="card next" id="new-review"><h2>新稿审查</h2><p>支持 Word、PDF、Markdown/TXT、RTF、HTML、CSV/TSV、ODT、Excel 和 PowerPoint。旧版 Office/WPS 需要本机 LibreOffice，扫描 PDF 需要 OCR 组件。原文件会完整保留。</p><label>项目标题（可选）</label><input id="title" type="text"><label>文件</label><input id="file" type="file" accept="${esc(reviewConfig.extensions.join(','))}">${encodingSelector('import-encoding')}<details><summary>扫描文件识别选项</summary>${ocrLanguageSelector('import-ocr-language')}</details><p class="muted">支持英文、简体中文、繁体中文、德语、法语、日语、俄语和拉丁语。编码选项适用于文本类文件。</p><p><button id="upload">开始识别</button></p><div id="err" class="error"></div></div>${state.projects.length ? ui`<div class="card"><h2>本地项目</h2>${state.projects.map((project) => ui`<div class="project-row"><span><b>${esc(project.title)}</b><br><small>${esc(project.source_name || project.directory)}</small></span><span><button class="secondary open-project" data-dir="${esc(project.directory)}">打开</button> <button class="danger delete-project" data-dir="${esc(project.directory)}">删除</button></span></div>`).join('')}</div>` : ''}${researchLibrary()}${dependencyCard()}`;
}
function workflow(view) {
  const localDone = view.workflow.some(step => step.key === 'local' && step.status === 'completed');
  const noIssues = localDone && !view.findings.length;
  const steps = view.workflow.map(step => ({...step,
    optional: step.key === 'ai' && !view.ai_requests.some(request => request.completed)
      || noIssues && ['adjudication', 'bridge'].includes(step.key)}));
  const next = steps.find(step => step.status !== 'completed' && !step.optional);
  return `<div class="card workflow-card"><nav class="workflow" aria-label="Review steps">${steps.map(step =>
    `<button class="step ${esc(step.status)} ${next?.key === step.key ? 'current' : ''}" data-stage="${step.key}">${esc(tr(step.label))}<b>${step.optional ? tr('可选') : esc(tr(step.detail))}</b></button>`).join('')}</nav>
    <div class="row workflow-next"><b>${next ? esc(tr(next.label)) : tr('本轮流程已完成')}</b>${next ?
    `<button id="continue-workflow" data-stage="${next.key}">${uiLocale === 'en' ? 'Continue' : '繼續'}</button>` : ''}</div></div>`;
}
function extractionCard(view) {
  const e = view.extraction,
    st = view.state,
    blocked =
      !e.available ||
      ['blocked', 'unconfirmed', 'replacement_required'].includes(
        st.extraction_state,
      );
  if (!blocked) return '';
  const blocks = e.blocks || [];
  return ui`<div class="card next"><h2>确认识别结果</h2><p>请先查看正文和定位；这是正式审查的质量门。</p>${(e.warnings || []).map((w) => `<p class="${['critical', 'high'].includes(w.severity) ? 'error' : 'warning'}">${esc(w.message)}</p>`).join('')}${(st.diagnostics || []).map((x) => `<p class="error">${esc(x)}</p>`).join('')}${extractionEncodingControl(view)}<h3>抽取内容与定位预览</h3><input id="source-search" type="text" placeholder="搜索正文或定位"><div id="source-preview" class="pane quote">${blocks.map((b) => `[${esc(b.location?.block_id || b.block_id)} · page ${esc(b.location?.page || '-')}] ${esc(b.text)}`).join('\n\n') || tr('暂时没有可预览正文')}</div><div class="row">${e.available ? tr('<button data-action="confirm-extraction">确认识别</button><button class="secondary" data-action="continue-extraction">带警告继续</button><button class="secondary" id="show-correction">修正识别文本</button>') : tr('<button id="retry-extraction">重新识别</button>')}<button class="danger" data-action="replace-file">更换文件</button></div><textarea id="correction" class="hidden" placeholder="提供修正后的完整审查文本"></textarea><div id="err" class="error"></div></div>`;
}
function contextCard(view) {
  const st = view.state,
    c = view.context || {};
  if (
    !['confirmed', 'confirmed_corrected', 'confirmed_with_warning'].includes(
      st.extraction_state,
    ) ||
    st.context_state === 'confirmed'
  )
    return '';
  return ui`<div class="card next"><h2>确认审查上下文</h2><p class="muted">字段不能为空；日期可填写 YYYY-MM-DD，未知时填写 unknown。</p><div class="grid"><div><label>审查类型</label><select id="review_profile"><option value="document">文书审查</option><option value="academic">学术审查</option><option value="mixed">文书 + 学术综合审查</option></select><label>学科（学术 / 综合模式适用）</label><select id="discipline">${Object.entries(
    reviewConfig.disciplines,
  )
    .map(([key, label]) => `<option value="${key}">${esc(tr(label))}</option>`)
    .join(
      '',
    )}</select><label>研究类型（学术 / 综合模式适用）</label><select id="research_type">${Object.entries(
    reviewConfig.research_types,
  )
    .map(([key, label]) => `<option value="${key}">${esc(tr(label))}</option>`)
    .join(
      '',
    )}</select><label>文档类型</label><input id="document_type" value="${esc(c.document_type || c.model_suggestion || tr('专业文档'))}"><label>适用地区/司法辖区（不适用填 unknown）</label><input id="jurisdiction"><label>拟生效 / 投稿日期</label><input id="effective_date" placeholder="2026-09-01 或 unknown"><label>发布者 / 作者机构</label><input id="publisher_type"><label>目标受众</label><input id="audience"></div><div><label>发布状态</label><select id="publication_status"><option value="internal-draft">内部草案</option><option value="external-formal">对外正式文件</option></select>${[
    tr('minors:未成年人'),
    tr('fees:收费'),
    tr('sponsorship:赞助'),
    tr('contract:合同'),
    tr('personal_information:个人信息'),
    tr('intellectual_property:知识产权'),
  ]
    .map((x) => {
      const [key, label] = x.split(':');
      return ui`<label><input id="involves_${key}" type="checkbox"> 涉及${label}</label>`;
    })
    .join(
      '',
    )}</div></div><label>核验材料说明（每行一项；只保存说明，不会自动下载 URL 或读取路径）</label><textarea id="user_provided_materials" placeholder="粘贴可供核验的原文摘录、页码、来源和访问说明；请勿放入敏感原始数据"></textarea><p><button id="confirm-context">确认上下文</button></p><div id="err" class="error"></div></div>`;
}
function reviewCards(view) {
  if (view.state.context_state !== 'confirmed') return '';
  const requests = view.ai_requests || [],
    done = requests.filter((x) => x.completed).length,
    localDone = (view.workflow || []).some(
      (x) => x.key === 'local' && x.status === 'completed',
    ),
    findingCount = view.finding_summary?.total || 0;
  return ui`<div class="grid"><div class="card"><h2>本地确定性预检</h2><p>词项覆盖和结构提示，不冒充 AI 专业判断。首次运行后会为同一批 critic 自动生成待执行的独立 AI 任务。</p>${localDone ? ui`<p class="ok">✓ 已完成本地预检，当前有 ${findingCount} 条 Finding。再次运行会追加一轮可审计记录。</p>` : ''}${Object.entries(
    critics,
  )
    .map(
      ([key, label]) =>
        `<label><input id="critic-${key}" class="critic" type="checkbox" value="${key}" checked> ${esc(tr(label))}</label>`,
    )
    .join(
      '',
    )}<p><button id="run-precheck">${localDone ? tr('重新运行选中的本地预检') : tr('运行预检并生成 AI 审查任务')}</button></p></div><div class="card"><h2>导出 / 导入独立 AI 审查 <span class="pill">${done}/${requests.length || Object.keys(critics).length} 已导入</span></h2><p>预检会自动生成缺失任务，但不会调用模型。默认由你把响应关联到当前选中的任务，应用负责绑定原件 SHA；严格模式只用于能够准确回显全部机器字段的模型。</p><div class="summary">${Object.entries(
    critics,
  )
    .map(([key, label]) => {
      const r = requests.find((x) => x.critic === key);
      return `<span class="pill">${r?.completed ? '✓' : '○'} ${esc(tr(label))}</span>`;
    })
    .join(
      '',
    )}</div><p><button class="secondary" id="export-ai-reviews" ${done ? '' : 'disabled'}>导出当前 AI 审查</button> <span class="muted">无需先裁决 Finding；导出物会明确标记“未经人工裁决”。</span></p><div class="grid"><div><label>模型来源（仅作声明）</label><input id="ai-provider" value="手动导入"><label>模型/版本（仅作声明）</label><input id="ai-model" value="未声明模型"><p><button id="prepare-ai">${requests.length ? tr('重新生成当前类型的独立协议') : tr('生成当前类型的独立协议')}</button> <button class="secondary" id="download-protocols" ${requests.length ? '' : 'disabled'}>下载全部协议 ZIP</button></p></div><div><label>当前任务</label><select id="ai-request">${requests.map((r) => `<option value="${esc(r.request_id)}">${r.completed ? '✓' : '○'} ${esc(tr(critics[r.critic] || r.critic))}</option>`).join('')}</select><div class="row"><button class="secondary" id="copy-protocol" ${requests.length ? '' : 'disabled'}>一键复制</button><button class="secondary" id="previous-request">上一份</button><button class="secondary" id="next-request">下一份</button></div><label>响应绑定方式</label><select id="binding-mode"><option value="manual_association">关联到当前任务（推荐）</option><option value="strict">严格回显校验（高级）</option></select><p class="muted">人工关联不会伪称模型回显了 SHA；原始响应和应用补充的绑定信息会分别留档。</p><label>模型原始 JSON 响应</label><textarea id="ai-response" placeholder="粘贴当前任务的 JSON"></textarea><button id="import-ai" ${requests.length ? '' : 'disabled'}>导入当前结果</button></div></div>${requests.map((r) => ui`<details><summary>${r.completed ? '✓' : '○'} ${esc(tr(critics[r.critic] || r.critic))} · ${esc(r.prompt_sha256.slice(0, 12))}</summary>${r.completed ? ui`<p class="muted">已保存在项目目录：${esc(r.storage_relative_path)}</p>` : ''}<button class="secondary copy-request" data-id="${esc(r.request_id)}">复制这份协议</button><div class="block">${esc(r.prompt)}</div></details>`).join('')}<div id="err" class="error"></div></div></div>`;
}
function closeReadingDetail(f) {
  const detail = f.check_data?.close_reading;
  if (!detail) return f.origin === 'model-derived'
    ? ui`<p class="muted">未提供上下文反证核对；请先检查作者是否已在其他段落回答此问题。</p>` : '';
  const roles = {context: tr('上下文'), support: tr('支持材料'), counterevidence: tr('反证材料'), qualification: tr('限定条件')};
  return ui`<details class="close-reading" open><summary>细读依据（模型判断）</summary><p><b>作者的实际主张：</b>${esc(detail.author_position)}</p><p><b>最强辩护：</b>${esc(detail.strongest_defense)}</p><p><b>仍然存在的问题：</b>${esc(detail.why_defense_fails)}</p><p><b>修正验收方法：</b>${esc(detail.repair_test)}</p>${(detail.context_evidence || []).map(anchor => `<div class="quote"><a href="#source-${esc(anchor.block_id)}">${esc(anchor.block_id)} · ${esc(roles[anchor.role] || anchor.role)}</a><p>${esc(anchor.quote)}</p></div>`).join('')}<p class="muted">引文已核对，判断是否成立仍需你确认。</p></details>`;
}

function findingCard(f) {
  return ui`<article class="card finding finding-card" data-critic="${esc(f.critic)}" data-severity="${esc(f.severity)}" data-status="${esc(f.status)}"><div class="row"><a href="#source-${esc(f.location.block_id)}" class="pill">${esc(f.location.block_id)} · page ${esc(f.location.page || '-')}</a><span class="pill">${esc(tr(critics[f.critic] || f.critic))}</span><span class="pill">${esc(f.severity)}</span><span class="pill">${esc(f.verification_state)}</span></div><div class="quote">${esc(f.evidence)}</div><p><b>问题：</b>${esc(f.issue)}</p><p><b>后果：</b>${esc(f.consequence)}</p><p><b>建议动作：</b>${esc(f.suggested_action)}</p>${closeReadingDetail(f)}${adversarialFindingDetail(f)}<details><summary>完整专业字段</summary><p><b>判断标准：</b>${esc(f.standard)}</p><p><b>外部依据：</b>${esc(f.external_basis?.source_name || tr('未提供'))} ${esc(f.external_basis?.locator || '')}</p><p><b>尚待确认：</b>${esc((f.uncertainties || []).join('；') || tr('无'))}</p><p><b>建议责任人：</b>${esc(f.suggested_owner || tr('未指定'))}</p><p><b>阻断发布/执行：</b>${f.blocks_release_or_execution ? tr('是') : tr('否')}</p><p><b>需要观察：</b>${esc(f.required_observation || tr('无'))}</p><p><b>竞争读法：</b>${esc((f.competing_readings || []).join('；') || tr('无'))}</p></details><label>人工决定理由</label><input id="reason-${esc(f.finding_id)}"><label>人工修正动作</label><textarea id="action-${esc(f.finding_id)}" placeholder="${esc(f.suggested_action)}"></textarea><div class="row"><button class="decision" data-id="${esc(f.finding_id)}" data-value="accept">接受</button><button class="decision secondary" data-id="${esc(f.finding_id)}" data-value="correct">修正后接受</button><button class="decision danger" data-id="${esc(f.finding_id)}" data-value="reject">拒绝</button><button class="decision secondary" data-id="${esc(f.finding_id)}" data-value="defer">暂缓</button><span>当前：${esc(f.status)}</span></div></article>`;
}
function externalRecheckWorkspace(ws) {
  const status = ws.external_recheck;
  if (!status || !status.requests?.length)
    return tr('<p class="ok">本轮没有需要外部模型复审的 Finding。</p>');
  const requests = status.requests
    .map(
      (request) =>
        ui`<details open><summary>${request.complete ? '✓' : '○'} ${esc(tr(critics[request.critic] || request.critic))}</summary><div class="row"><button class="secondary copy-external-recheck" data-critic="${esc(request.critic)}">复制复审协议</button><button class="secondary download" data-path="${esc(request.relative_path)}">下载协议</button></div><p class="muted">原请求 ${esc(request.original_request_id)} · 原模型 ${esc(request.original_provider)}/${esc(request.original_model)} · 原 AuditRun ${esc(request.original_audit_run_id)}</p><div class="block">${esc(request.prompt)}</div>${request.result ? ui`<p class="muted">本次复审声明：${esc(request.result.declared_model_metadata?.provider)}/${esc(request.result.declared_model_metadata?.model)}。模型提议不会自动成为最终决定。</p>${request.items.map((item) => `<article class="finding"><p><b>${esc(item.finding_id)}</b> · ${item.kind === 'new' ? tr('新 Finding') : tr('模型状态 ') + esc(item.state)}</p><p>${esc(item.reason)}</p><div class="quote">${esc(item.evidence)}</div>${item.kind === 'new' ? tr('<p class="warning">新 Finding 不能直接标记为已解决；它将进入下一轮 accept/correct/reject/defer 裁决。</p>') : ui`${item.human_decision ? ui`<p class="ok">人工 Resolution：${esc(item.human_decision.state)} · ${esc(item.human_decision.reason)}</p>` : ''}<label>人工 Resolution 理由</label><input id="external-reason-${esc(request.critic)}-${esc(item.finding_id)}"><div class="row"><button class="external-resolution" data-revision-id="${esc(status.revision_id)}" data-result-id="${esc(request.result.result_id)}" data-finding-id="${esc(item.finding_id)}" data-state="resolved">确认已解决</button><button class="external-resolution secondary" data-revision-id="${esc(status.revision_id)}" data-result-id="${esc(request.result.result_id)}" data-finding-id="${esc(item.finding_id)}" data-state="partially-resolved">确认部分解决</button><button class="external-resolution danger" data-revision-id="${esc(status.revision_id)}" data-result-id="${esc(request.result.result_id)}" data-finding-id="${esc(item.finding_id)}" data-state="unresolved">确认未解决</button></div>`}</article>`).join('')}` : ui`<label>本次复审 provider</label><input id="external-provider-${esc(request.critic)}"><label>本次复审 model/版本</label><input id="external-model-${esc(request.critic)}"><label>响应绑定方式</label><select id="external-binding-${esc(request.critic)}"><option value="strict">严格绑定（推荐）</option><option value="manual_association">人工关联（较弱审计）</option></select><label>外部复审原始 JSON 响应</label><textarea id="external-response-${esc(request.critic)}"></textarea><button class="import-external-recheck" data-revision-id="${esc(status.revision_id)}" data-critic="${esc(request.critic)}">导入复审结果</button>`}</details>`,
    )
    .join('');
  const followup = status.can_start_followup
    ? ui`<div class="card next"><h3>开始下一轮</h3><p>新 Finding 和人工确认仍未解决的旧 Finding 将成为修订稿版本上的 open Finding，重新进入裁决和修改。</p><button id="start-followup-round" data-revision-id="${esc(status.revision_id)}">进入下一轮 Finding 裁决</button></div>`
    : status.followup_started
      ? tr('<p class="ok">需继续处理的 Finding 已进入下一轮。</p>')
      : '';
  return ui`<div class="card ${status.complete ? 'ok' : 'next'}"><h2>外部 critic 复审与人工 Resolution</h2><p>${status.complete ? tr('外部 Resolution 已确认，需继续处理的问题已进入下一轮或本轮没有遗留项。') : tr('协议绑定原 critic 的完整 prompt、request、AuditRun 和 provider/model；请导入复审响应并逐项确认原 Finding。')}</p>${requests}${followup}</div>`;
}
function revisionWorkspace(view) {
  const ws = view.revision_workspace || {},
    actions = ws.actions || [],
    op = {
      replace_block: tr('替换文本块'),
      insert_before: tr('在块前插入'),
      insert_after: tr('在块后插入'),
      delete_block: tr('删除文本块'),
      replace_table_cell: tr('替换表格单元格'),
      append_section: tr('追加独立章节'),
      replace_range: tr('替换连续段落'),
    };
  if (!ws.plan) return '';
  if (ws.revision)
    return ui`<div class="card ok"><h2>修改稿已生成</h2><p>本地确定性 critic 已按稳定 check_id 复跑。外部模型 Finding 必须经过绑定原 critic 的复审、人工 Resolution，并把新问题送入下一轮。</p><button id="export-final">导出当前交付物</button></div>${externalRecheckWorkspace(ws)}`;
  const cards = actions
    .map((a) => {
      const header = ui`<div class="row"><span class="pill">${esc(a.block_id)}</span><span class="pill">工作组 ${esc(a.work_group_id)}</span><span class="pill">${a.finding_ids.length} 条 Finding</span>${a.operation ? ui`<span class="pill">已确认：${esc(op[a.operation])}</span>` : ui`<span class="pill warning">建议：${esc(op[a.operation_suggestion])}</span>`}${a.supported ? '' : tr('<span class="pill error">不能安全自动修改</span>')}</div>`;
      const reasons = `<div class="quote">${esc(a.before_text)}</div>${a.critic_reasons.map((r) => ui`<p><b>${esc(tr(critics[r.critic] || r.critic))}：</b>${esc(r.issue)}<br><span class="muted">批准的动作：${esc(r.approved_instruction)}</span></p>`).join('')}`;
      if (!a.supported)
        return `<article class="card revision-action-card" data-action-id="${esc(a.action_id)}">${header}${reasons}<p class="error">${esc(a.unsupported_reason)}</p></article>`;
      if (!a.operation)
        return ui`<article class="card revision-action-card" data-action-id="${esc(a.action_id)}">${header}${reasons}<label>人工选择修改操作</label><select id="operation-${esc(a.action_id)}">${Object.entries(
          op,
        )
          .filter(([key]) =>
            a.block_kind === 'table_cell'
              ? key === 'replace_table_cell'
              : key !== 'replace_table_cell',
          )
          .map(
            ([key, label]) =>
              `<option value="${key}" ${key === a.operation_suggestion ? 'selected' : ''}>${label}</option>`,
          )
          .join(
            '',
          )}</select><label>选择理由</label><input id="operation-reason-${esc(a.action_id)}"><button class="confirm-operation" data-action-id="${esc(a.action_id)}">确认操作类型</button><p class="muted">关键词推断只作为建议；没有这一步人工确认，系统不会接受 Hunk。</p></article>`;
      if (!a.hunk)
        return ui`<article class="card revision-action-card" data-action-id="${esc(a.action_id)}">${header}${reasons}<label>${a.operation === 'delete_block' ? tr('删除操作（文本留空）') : tr('本 Action 的具体文本')}</label><textarea id="revision-${esc(a.action_id)}"></textarea><label>Hunk 理由</label><input id="revision-reason-${esc(a.action_id)}"><label>文本来源</label><select id="revision-provenance-${esc(a.action_id)}"><option value="human-authored">人工撰写</option><option value="ai-assisted-manual-import">AI 辅助后人工提交</option></select><button class="propose-hunk" data-action-id="${esc(a.action_id)}">提交独立 Hunk</button></article>`;
      if (a.hunk_stale)
        return ui`<article class="card revision-action-card" data-action-id="${esc(a.action_id)}">${header}${reasons}<p class="error">操作类型决定已变化，旧 Hunk 失效。请按当前操作提交新的 Hunk。</p><label>新的具体文本</label><textarea id="revision-${esc(a.action_id)}"></textarea><label>新 Hunk 理由</label><input id="revision-reason-${esc(a.action_id)}"><button class="propose-hunk" data-action-id="${esc(a.action_id)}">提交新 Hunk</button></article>`;
      const decision = a.hunk_decision
        ? ui`<p class="${a.hunk_decision.decision === 'approve' ? 'ok' : 'error'}">人工决定：${esc(a.hunk_decision.decision)} · ${esc(a.hunk_decision.reason)}</p>${a.hunk_decision.decision === 'reject' ? ui`<label>提交新的修改文本</label><textarea id="revision-${esc(a.action_id)}"></textarea><label>新 Hunk 理由</label><input id="revision-reason-${esc(a.action_id)}"><button class="propose-hunk" data-action-id="${esc(a.action_id)}">重新提交</button>` : ''}`
        : ui`<label>Hunk 决定理由</label><input id="hunk-reason-${esc(a.hunk.hunk_id)}"><div class="row"><button class="decide-hunk" data-hunk-id="${esc(a.hunk.hunk_id)}" data-decision="approve">批准这一项</button><button class="decide-hunk danger" data-hunk-id="${esc(a.hunk.hunk_id)}" data-decision="reject">驳回并重写</button></div>`;
      return ui`<article class="card revision-action-card" data-action-id="${esc(a.action_id)}">${header}${reasons}<label>拟应用文本</label><div class="quote">${esc(a.hunk.after_text)}</div><p class="muted">操作：${esc(op[a.operation])} · 来源：${esc(a.hunk.provenance)} · 理由：${esc(a.hunk.rationale)}</p>${decision}</article>`;
    })
    .join('');
  return ui`<div class="card next"><h2>逐项修改与批准</h2><p>Action 绑定工作组和明确 Finding 集。操作类型必须由人显式选择，系统的自然语言推断只显示为建议。</p></div>${cards}<div class="card"><button id="finalize-revision" ${ws.ready_to_finalize ? '' : 'disabled'}>生成修改稿并复审</button><p class="muted">所有 Action 都必须确认操作类型，并完成最新 Hunk 的批准或驳回。</p><div id="err" class="error"></div></div>`;
}
function findingWorkspace(view) {
  const items = view.findings || [],
    summary = view.finding_summary || {},
    queue = view.attention_queue || { groups: [] };
  if (!items.length)
    return view.state.review_state !== 'not_started'
      ? tr(
          '<div class="card"><h2>本轮没有产生 Finding</h2><p>零 Finding 不代表自动确认合规或质量，审查范围仍记录在审计产物中。</p><button id="export-results">仅导出审查结果</button></div>',
        )
      : '';
  const blocks = view.extraction.blocks || [],
    limit = queue.default_limit || 30;
  const groups = queue.groups || [];
  return ui`<div class="card"><h2>人工裁决队列</h2><div class="summary"><span class="pill">${queue.total_groups || 0} 个工作组</span><span class="pill">${summary.total || 0} 条原子 Finding</span><span class="pill">待处理 ${summary.open || 0} ${tr('条计数单位')}</span><span class="pill">高/严重 ${(summary.by_severity?.high || 0) + (summary.by_severity?.critical || 0)} ${tr('条计数单位')}</span></div><p class="muted">默认只展示前 ${limit} 个承重工作组；归组仅管理注意力，不合并 critic 的理由、证据或决定。</p>${queue.hidden_groups ? ui`<button class="secondary" id="show-all-groups">展开其余 ${queue.hidden_groups} 个工作组</button>` : ''}<div class="toolbar"><label>维度<select id="filter-critic"><option value="">全部</option>${Object.entries(
    critics,
  )
    .map(([k, v]) => `<option value="${k}">${esc(tr(v))}</option>`)
    .join(
      '',
    )}</select></label><label>严重度<select id="filter-severity"><option value="">全部</option>${['critical', 'high', 'medium', 'low', 'info'].map((x) => `<option>${x}</option>`).join('')}</select></label><label>状态<select id="filter-status"><option value="">全部</option>${['open', 'accept', 'correct', 'reject', 'defer'].map((x) => `<option>${x}</option>`).join('')}</select></label></div></div><div class="workspace"><section class="card pane"><h3>原文与定位</h3><input id="workspace-search" type="text" placeholder="搜索原文">${blocks.map((b) => `<div class="source-block" id="source-${esc(b.location?.block_id || b.block_id)}"><small>${esc(b.location?.block_id || b.block_id)} · page ${esc(b.location?.page || '-')}</small><br>${esc(b.text)}</div>`).join('')}</section><section>${groups.map((g, index) => ui`<section class="finding-group ${index >= limit ? 'hidden extra-finding-group' : ''}"><div class="card"><div class="row"><b>工作组 ${esc(g.block_id)}</b><span class="pill">${g.finding_count} ${tr('条计数单位')}</span><span class="pill">${g.critic_count} 个 critic</span></div><p><b>排序依据：</b>${esc(g.priority_reasons.map(tr).join(uiLocale === 'en' ? '; ' : '；'))}</p><p><b>共同修改动作：</b>${esc(g.suggested_action)}</p></div>${g.findings.map(findingCard).join('')}</section>`).join('')}</section><aside class="card sticky"><h3>完成门</h3><p>所有原子 Finding 裁决完成后，才可生成 Action 和 Hunk。</p><p><b>待处理：</b>${summary.open || 0}</p><button id="prepare-bridge" ${summary.open ? 'disabled' : ''}>生成逐段修改计划</button><p><button id="export-results" ${summary.open ? 'disabled' : ''}>仅导出审查结果</button></p></aside></div><div id="err" class="error"></div>${revisionWorkspace(view)}`;
}
function exportCenter(view) {
  const rows = view.exports || [];
  if (!rows.length) return '';
  return ui`<div class="card"><h2>导出中心</h2><p class="ok">导出完成。文件可直接下载，也可打开所在文件夹。</p>${rows.map((row) => ui`<details open><summary>${row.kind === 'revision-bridge' ? tr('修改任务') : row.kind === 'ai-review' ? tr('AI 审查快照') : tr('正式审查导出')} · ${esc(row.export_id)}${row.finding_count ? ui` · ${row.finding_count} 条 Finding` : ''}</summary>${row.files.map((file) => ui`<div class="export-file"><span>${esc(tr(file.label))} <small>${esc(file.name)}</small></span><button class="secondary download" data-path="${esc(file.relative_path)}">下载</button></div>`).join('')}<p><button class="secondary open-folder" data-path="${esc(row.files[0]?.relative_path || '')}">打开所在文件夹</button></p></details>`).join('')}</div>`;
}
function project() {
  const v = state.selected,
    st = v.state;
  const readonly = st.read_only || st.integrity_errors?.length;
  root.innerHTML = ui`${serviceDetails(state.notice)}${readonly ? ui`<div class="card readonly"><h2>项目已切换为只读</h2><p>${esc((st.integrity_errors || []).join('；') || tr('完整性链异常'))}</p><p>请恢复可信项目副本后再继续，下载也会被阻止。</p></div>` : ''}<div class="card"><div class="row"><button class="secondary" id="back">← 返回项目列表</button><button class="danger" id="delete-selected" data-dir="${esc(v.directory)}">删除本地项目</button></div><h2>${esc(v.project.title)}</h2><p class="muted">原件：${esc(v.project.source.name)} · SHA-256 ${esc(v.project.source.sha256)}</p></div>${workflow(v)}${extractionCard(v)}${contextCard(v)}${reviewCards(v)}${findingWorkspace(v)}${adversarialHistory(v)}${exportCenter(v)}`;
}
