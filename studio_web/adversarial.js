// Each response belongs to one immutable request; normal human decisions remain
// in the finding card. This module never writes them from a model proposal.
function adversarialErrorMessage(message) {
  // Translate only known diagnostic grammar. JSON keys, model text and artifact
  // identifiers in the remaining detail must never pass through global replace.
  const archived = '；原响应和拒绝原因已保存，可修正后重试';
  const raw = String(message ?? '');
  const diagnostic = raw.endsWith(archived) ? raw.slice(0, -archived.length) : raw;
  let translated = UI_MESSAGES[diagnostic]?.[uiLocale];
  let match;
  if (!translated && diagnostic.startsWith('Adversarial response is not strict JSON: ')) {
    const detail = diagnostic.slice('Adversarial response is not strict JSON: '.length);
    const duplicate = 'Duplicate JSON field: ';
    translated = tr('深审响应不是有效的严格 JSON。') + '\n' + tr('技术详情：') +
      (detail.startsWith(duplicate) ? tr('JSON 字段重复：') + detail.slice(duplicate.length) : detail);
  } else if (!translated && (match = /^Adversarial response ([a-z_]+) does not match its request$/.exec(diagnostic))) {
    translated = tr('响应任务绑定不一致：') + match[1] + tr('。请使用这份任务的完整协议重新生成响应。');
  } else if (!translated && (match = /^([a-z_]+) must be bounded (optional|nonempty) text$/.exec(diagnostic))) {
    translated = (match[2] === 'optional' ? tr('响应字段必须是长度限制内的文本：') : tr('响应字段必须是长度限制内的非空文本：')) + match[1];
  } else if (!translated && (match = /^([a-z_]+) contains invalid (Unicode|control characters)$/.exec(diagnostic))) {
    translated = tr('响应字段包含无效字符：') + match[1];
  } else if (!translated && (match = /^(response|defense result|assessment result|context_evidence entry) must contain exactly: (.+)$/.exec(diagnostic))) {
    translated = tr('响应结构与协议不一致。') + '\n' + tr('技术详情：') + match[1] + ': ' + match[2];
  } else if (!translated && diagnostic.startsWith('对抗深审记录完整性校验失败：')) {
    translated = tr('对抗深审记录完整性校验失败：') + diagnostic.slice('对抗深审记录完整性校验失败：'.length);
  }
  return (translated || tr('深审操作失败，原始诊断如下：') + diagnostic) +
    (raw.endsWith(archived) ? '\n' + tr(archived.slice(1)) : '');
}

function adversarialEvidence(evidence, current) {
  const roles = {context: tr('上下文'), support: tr('支持材料'),
    counterevidence: tr('反证材料'), qualification: tr('限定条件')};
  return (evidence || []).map(anchor => {
    const label = `${anchor.evidence_id ? anchor.evidence_id + ' · ' : ''}${anchor.block_id} · ${roles[anchor.role] || anchor.role}`;
    return `<div class="quote">${current
      ? `<a href="#source-${esc(anchor.block_id)}">${esc(label)}</a>`
      : `<span>${esc(label)}</span>`}<p>${esc(anchor.quote)}</p></div>`;
  }).join('');
}

function adversarialModelFields(id) {
  return ui`<div class="grid"><div><label for="adv-provider-${esc(id)}">模型来源（仅作声明）</label><input id="adv-provider-${esc(id)}" autocomplete="off"></div><div><label for="adv-model-${esc(id)}">模型/版本（仅作声明）</label><input id="adv-model-${esc(id)}" autocomplete="off"></div></div>`;
}

function adversarialRequest(session, request, complete) {
  const current = session.current === true;
  const disabled = !current || state.selected.state.read_only;
  const stage = request.stage === 'defense' ? tr('独立辩护') : tr('证据复核');
  return ui`<section class="adversarial-stage" data-request-id="${esc(request.request_id)}">
    <h4>${esc(stage)} · ${complete ? tr('响应已导入') : tr('等待模型响应')}</h4>
    <p class="muted">声明的模型：${esc(request.provider)} / ${esc(request.model)}</p>
    <details><summary>查看这份深审协议</summary><pre class="block adversarial-prompt">${esc(request.prompt)}</pre></details>
    <div class="row"><button class="secondary adv-copy" data-request-id="${esc(request.request_id)}" ${disabled ? 'disabled' : ''}>复制这份协议</button><button class="secondary adv-download" data-request-id="${esc(request.request_id)}" ${disabled ? 'disabled' : ''}>下载协议</button></div>
    ${!complete && current ? ui`<label for="adv-response-${esc(request.request_id)}">模型原始 JSON 响应</label><textarea id="adv-response-${esc(request.request_id)}" spellcheck="false" placeholder="粘贴完整响应，保留协议中的任务绑定字段" ${disabled ? 'disabled' : ''}></textarea><button class="adv-import" data-session-id="${esc(session.session_id)}" data-request-id="${esc(request.request_id)}" ${disabled ? 'disabled' : ''}>导入这份深审响应</button>` : ''}
  </section>`;
}

function adversarialSession(session) {
  const current = session.current === true;
  const defense = session.defense;
  const assessment = session.assessment;
  const defenseRequest = (session.requests || []).find(request => request.stage === 'defense');
  const assessmentRequest = (session.requests || []).find(request => request.stage === 'assessment');
  const statuses = {awaiting_defense: tr('等待独立辩护'), defense_ready: tr('辩护已导入，待生成复核任务'),
    awaiting_assessment: tr('等待证据复核'), completed: tr('深审完成，供人工判断')};
  const dispositions = {retain: tr('建议保留批评'), narrow: tr('建议缩小批评'),
    withdraw: tr('建议撤回批评'), insufficient: tr('材料不足，暂不能判断')};
  return ui`<details class="adversarial-session" data-session-id="${esc(session.session_id)}" ${current ? 'open' : ''}>
    <summary><b>${current ? esc(statuses[session.status] || session.status) : tr('历史深审（已停止）')}</b> <small>${esc(session.session_id)}</small></summary>
    ${current ? '' : ui`<p class="warning">此记录对应较早的稿件或审查状态，仅供查阅；请针对当前问题重新深审。</p>`}
    ${!current && session.challenge ? ui`<details><summary>当时的批评与原文</summary><p>${esc(session.challenge.issue)}</p><div class="quote">${esc(session.challenge.evidence)}</div></details>` : ''}
    ${defenseRequest ? adversarialRequest(session, defenseRequest, Boolean(defense)) : ''}
    ${defense ? ui`<details class="adversarial-defense" open><summary>辩护结果（模型提议）</summary><p><b>作者的实际主张：</b>${esc(defense.author_position)}</p><p><b>最强辩护：</b>${esc(defense.strongest_defense)}</p><p><b>辩护的局限：</b>${esc(defense.limitations)}</p>${adversarialEvidence(defense.context_evidence, current)}</details>` : ''}
    ${current && session.status === 'defense_ready' ? ui`<div class="adversarial-next"><h4>下一步：独立复核双方证据</h4><p class="muted">在新的模型会话中执行复核协议。模型来源由你声明，工作台无法证明服务商或模型彼此独立。</p>${adversarialModelFields(session.session_id)}<button class="adv-assess" data-session-id="${esc(session.session_id)}" ${state.selected.state.read_only ? 'disabled' : ''}>生成证据复核任务</button></div>` : ''}
    ${assessmentRequest ? adversarialRequest(session, assessmentRequest, Boolean(assessment)) : ''}
    ${assessment ? ui`<details class="adversarial-assessment" open><summary>${esc(dispositions[assessment.disposition] || assessment.disposition)} · ${tr('模型提议')}</summary><p><b>复核理由：</b>${esc(assessment.reasons)}</p><p><b>仍然存在的问题：</b>${esc(assessment.remaining_issue)}</p><p><b>最小修改：</b>${esc(assessment.minimal_repair)}</p><p><b>修正验收方法：</b>${esc(assessment.repair_test)}</p><p><b>已回应的辩护证据：</b>${esc((assessment.defense_evidence_ids || []).join(', '))}</p>${adversarialEvidence(assessment.context_evidence, current)}<p class="muted">请使用下方人工决定按钮处理原问题；深审建议不会自动接受、撤回或解决问题。</p></details>` : ''}
    ${defense || assessment ? ui`<p class="muted">已核对引文与任务绑定；论证是否成立仍需人工判断。</p>` : ''}
  </details>`;
}

function adversarialFindingDetail(finding) {
  const sessions = (state.selected?.adversarial_reviews || [])
    .filter(session => session.finding_id === finding.finding_id);
  // Eligibility comes from the source audit, since older deterministic findings
  // can share the same origin label as imported model findings.
  const eligible = (state.selected?.adversarial_eligible_finding_ids || []).includes(finding.finding_id);
  if (!eligible && !sessions.length) return '';
  const restart = sessions.some(session => session.current);
  const start = ui`<div class="adversarial-start">${restart ? ui`<p class="warning">重新开始会保留旧记录并停止其响应导入。请重新执行辩护和复核；人工决定保持不变。</p>` : ''}${adversarialModelFields(finding.finding_id)}<button class="adv-start secondary" data-finding-id="${esc(finding.finding_id)}" data-restart="${restart}" ${state.selected.state.read_only ? 'disabled' : ''}>${restart ? tr('重新开始深审') : tr('生成独立辩护任务')}</button></div>`;
  return ui`<details class="adversarial-review" ${sessions.length ? 'open' : ''}><summary>对抗深审（可选）</summary>
    <p>针对这条批评，让独立辩护查找作者已有的回答，再由另一轮复核指出哪些问题仍成立。适用于学术论证和文书中的关键问题。</p>
    <p class="muted">每一步都由你导出协议并导入模型响应，不会自动上传稿件。请在新的模型会话中执行，保留完整响应绑定字段。</p>
    ${sessions.map(adversarialSession).join('')}
    ${!eligible ? '' : sessions.length ? ui`<details class="adversarial-restart"><summary>重新开始或更换模型</summary>${start}</details>` : start}
  </details>`;
}

function adversarialRequestById(id) {
  return (state.selected?.adversarial_reviews || [])
    .flatMap(session => session.requests || []).find(request => request.request_id === id);
}

function adversarialHistory(view) {
  const currentFindings = new Set((view.findings || []).map(finding => finding.finding_id));
  const previous = (view.adversarial_reviews || []).filter(session => !currentFindings.has(session.finding_id));
  if (!previous.length) return '';
  return ui`<details class="card adversarial-history"><summary>过去问题的深审记录 <span class="pill">${previous.length}</span></summary><p class="muted">这些问题已不在当前裁决队列。旧批评、辩护及复核仍可查阅，不能导入新响应或跳转到当前稿件。</p>${previous.map(session => ui`<p><b>${esc(tr(critics[session.critic] || session.critic))}</b> · ${esc(session.finding_id)}</p>${adversarialSession({...session, current: false})}`).join('')}</details>`;
}

function adversarialModel(id) {
  const provider = document.getElementById(`adv-provider-${id}`)?.value.trim();
  const model = document.getElementById(`adv-model-${id}`)?.value.trim();
  if (!provider || !model) {
    errorMessage(tr('请填写本轮模型来源和模型/版本，再生成任务。'));
    return null;
  }
  return {provider, model};
}

function bindAdversarial() {
  root.querySelectorAll('.adv-start').forEach(button => {
    button.onclick = () => {
      const model = adversarialModel(button.dataset.findingId);
      if (model) act('prepare_adversarial_review', {
        finding_id: button.dataset.findingId, restart: button.dataset.restart === 'true', ...model,
      });
    };
  });
  root.querySelectorAll('.adv-assess').forEach(button => {
    button.onclick = () => {
      const model = adversarialModel(button.dataset.sessionId);
      if (model) act('prepare_adversarial_assessment', {session_id: button.dataset.sessionId, ...model});
    };
  });
  root.querySelectorAll('.adv-import').forEach(button => {
    button.onclick = () => act('import_adversarial_response', {
      session_id: button.dataset.sessionId, request_id: button.dataset.requestId,
      response: document.getElementById(`adv-response-${button.dataset.requestId}`).value,
    });
  });
  root.querySelectorAll('.adv-copy').forEach(button => {
    button.onclick = () => copyPrompt(adversarialRequestById(button.dataset.requestId));
  });
  root.querySelectorAll('.adv-download').forEach(button => {
    button.onclick = () => {
      const request = adversarialRequestById(button.dataset.requestId);
      if (!request) return;
      const url = URL.createObjectURL(new Blob([request.prompt], {type: 'text/plain;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = `adversarial-${request.stage}-${request.request_id}.txt`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
  });
}
