const TOKEN = __WORKBENCH_TOKEN__;
let state = null,
  selectedClaim = null,
  selectedLens = 'all',
  pendingFinding = null;
const $ = id => document.getElementById(id);
const esc = s => {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
};
async function requestJson(path, options, mutation = false) {
  if (mutation) options = {...options, headers: {...options?.headers,
    'X-Argument-Project-Context': state?.request_context || ''}};
  markResearchMutation(1);
  document.body.inert = true;
  // Modal dialogs escape an inert ancestor; mark the dialog itself as well.
  document.querySelectorAll('dialog').forEach(dialog => {dialog.inert = true;});
  try {
    let r;
    try {
      r = await fetch(path, options);
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
      e.uncertainMutation = mutation && r.status >= 500;
      throw e;
    }
    return j;
  } finally {
    markResearchMutation(-1);
    document.body.inert = researchMutationPending > 0;
    document.querySelectorAll('dialog').forEach(dialog => {dialog.inert = researchMutationPending > 0;});
  }
}
async function load(version) {
  const q = version ? '?version=' + encodeURIComponent(version) : '';
  state = await requestJson('/api/view' + q, {
    headers: {
      'X-Argument-Workbench-Token': TOKEN
    }
  });
  if (!selectedClaim || !state.claims.some(c => c.id === selectedClaim)) selectedClaim = state.claims[0]?.id || null;
  render();
}
function render() {
  document.title = state.project.title + ' · ' + tr('Argument Workbench');
  $('projectTitle').textContent = state.project.title + ' · ' + state.project.source_name;
  $('version').innerHTML = state.project.versions.map(v => ui`<option ${v === state.project.version_id ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const d = state.dashboard;
  const ms = [['claims', 'Claims'], ['open_findings', tr('未裁决')], ['deferred', tr('推迟')], ['resolved', tr('已解决')], ['accepted', tr('已接受')], ['unverified_citations', tr('未核验引文')]];
  $('metrics').innerHTML = ms.map(([k, l]) => ui`<div class="metric"><b>${d[k]}</b><span>${tr(l)}</span></div>`).join('');
  renderManuscript();
  renderClaims();
  renderReview();
}
function renderManuscript() {
  $('manuscript').innerHTML = state.manuscript.map(l => ui`<div class="line ${l.claim_ids.includes(selectedClaim) ? 'active' : ''}" data-claims="${l.claim_ids.join(',')}"><span class="ln">${l.number}</span><span>${esc(l.text)}${l.claim_ids.map(id => ui`<button class="claim-chip" data-claim="${id}">${id}</button>`).join('')}</span></div>`).join('');
  document.querySelectorAll('[data-claim]').forEach(b => b.onclick = () => selectClaim(b.dataset.claim));
}
function nodeLink(id) {
  const n = state.nodes[id];
  return n ? ui`<div class="card"><span class="claim-id">${esc(id)}</span> ${esc(n.text)}</div>` : ui`<div class="card">${esc(id)}</div>`;
}
function renderClaims() {
  $('claims').innerHTML = state.claims.map(c => ui`<button class="${c.id === selectedClaim ? 'active' : ''}" data-select="${c.id}"><span class="claim-id">${c.id}</span> <span class="badge">${esc(systemLabel(c.role))}</span><div>${esc(c.text)}</div></button>`).join('');
  document.querySelectorAll('[data-select]').forEach(b => b.onclick = () => selectClaim(b.dataset.select));
  const c = state.claims.find(x => x.id === selectedClaim);
  if (!c) {
    $('claimDetail').innerHTML = tr('<div class="empty">尚无 Claim</div>');
    return;
  }
  const incoming = c.incoming.map(r => nodeLink(r.from) + ui`<div class="relation">${esc(r.id)} · ${esc(systemLabel(r.type))} → ${esc(r.to)}</div>`).join('');
  const outgoing = c.outgoing.map(r => nodeLink(r.to) + ui`<div class="relation">${esc(r.id)} · ${esc(r.from)} → ${esc(systemLabel(r.type))}</div>`).join('');
  $('claimDetail').innerHTML = ui`<div class="section"><h3>当前主张</h3><div class="card"><b>${esc(c.source_quote)}</b><p>${esc(c.text)}</p><span class="badge">${esc(c.types.map(systemLabel).join(' / '))}</span><span class="badge">${esc(c.methods.map(systemLabel).join(' / '))}</span><p class="muted">${esc(c.position)} · 位置为 deterministic；语义为 model-derived / human-corrected</p></div></div><div class="section"><h3>上游 · Supported by / Assumptions / Citations</h3>${incoming || tr('<div class="empty">没有上游关系</div>')}</div><div class="section"><h3>下游 · Supports / Qualifies / Contradicts</h3>${outgoing || tr('<div class="empty">没有下游关系</div>')}</div>`;
}
function provenanceTrace(f) {
  const p = f.provenance_trace;
  const row = (label, value) => value ? ui`<div class="relation">${tr(label)} · ${esc(value)}</div>` : '';
  return ui`<details><summary>完整 provenance</summary>${row('Source', p.source_sha256)}${row('Reviewed IR', p.reviewed_ir_sha256)}${row('Review run', p.review_run_sha256)}${row('Lens protocol', p.lens_protocol_sha256)}${row('Model result', p.model_result_sha256)}${row('Finding', p.finding_sha256)}${row('Human decision', p.adjudication_sha256)}${(p.action_sha256s || []).map((x, i) => row('RevisionAction ' + (i + 1), x)).join('')}</details>`;
}
function lensBasis(o) {
  const b = o.lens_basis || {},
    lens = state.lenses.find(l => l.review_id === o.review_id);
  const rule = ui`<p><b>${esc(b.label)}</b></p>${b.question ? ui`<p>检查问题：${esc(b.question)}</p>` : ''}${b.failure_condition ? ui`<p>失败条件：${esc(b.failure_condition)}</p>` : ''}${b.evidence_policy ? ui`<p class="muted">Evidence policy：${esc(systemLabel(b.evidence_policy))}</p>` : ''}`;
  const protocol = lens?.protocol_text ? ui`<pre class="protocol">${esc(lens.protocol_text)}</pre>` : '';
  return ui`<details><summary>Lens 的规则／方法论依据</summary>${rule}${protocol}</details>`;
}
function renderHistory() {
  const versions = state.version_history.map(v => ui`<div class="card"><b>${esc(v.version_id)} · ${esc(v.source_name)}</b><p>${v.claims} Claims · ${v.corrections} 人工 correction · ${v.findings.open} 未裁决 · ${v.findings.accept} 接受 · ${v.findings.defer} 推迟 · ${v.unverified_citations} 未核验 Citation</p><div class="relation">Source · ${esc(v.source_sha256)}</div></div>`).join('');
  const transitions = state.lineage.map(h => ui`<div class="card"><b>${esc(h.pair)} · Claim Lineage</b><p>${h.proposals.length} correspondences · ${Object.entries(h.summary || {}).map(([k, v]) => esc(systemLabel(k)) + ': ' + v).join(' · ')}</p><div class="human">${h.proposals.filter(p => p.human_decision).length}/${h.proposals.length} human-confirmed</div></div>`).join('');
  const resolutions = state.resolutions.map(r => ui`<div class="card"><b>${esc(r.resolution_id)} · ${esc(r.original_finding_id)}</b><p>${esc(r.original_finding.reason)}</p><div>${esc((r.descendant_claims || []).join(', ') || systemLabel('removed'))} · ${esc(systemLabel(r.human_decision?.final_status || r.proposed_status || 'pending'))}</div></div>`).join('');
  $('historyTimeline').innerHTML = versions + transitions + resolutions;
  $('historyDialog').showModal();
}
function renderReview() {
  const lenses = [{
    id: 'all',
    label: tr('全部 Lenses')
  }, ...state.lenses.map(l => ({
    id: l.review_id,
    label: l.id
  }))];
  $('lensTabs').innerHTML = lenses.map(l => ui`<button data-lens="${esc(l.id)}" class="${l.id === selectedLens ? 'active' : ''}">${esc(systemLabel(l.label))}</button>`).join('');
  document.querySelectorAll('[data-lens]').forEach(b => b.onclick = () => {
    selectedLens = b.dataset.lens;
    renderReview();
  });
  const target = state.project.version_id + ':' + selectedClaim;
  const outcomes = state.outcomes.filter(o => o.target_claim === target && (selectedLens === 'all' || o.review_id === selectedLens));
  const findings = new Map(state.findings.map(f => [f.finding_id, f]));
  const html = outcomes.map(o => {
    const f = o.finding_id ? findings.get(o.finding_id) : null,
      decision = f?.decision || null;
    const buttons = f && state.permissions.can_adjudicate ? ui`<div class="decision"><button data-decide="${esc(f.finding_id)}">${decision ? tr('复议') : tr('人工裁决')}</button></div>` : '';
    const actions = f?.actions?.map(a => ui`<li>${esc(systemLabel(a.action_type))} · ${esc(a.text)}</li>`).join('') || '';
    return ui`<div class="card verdict-${esc(o.verdict)}"><div><span class="status">${esc(systemLabel(o.verdict))}</span> · <b>${esc(systemLabel(o.lens.id))}</b> ${o.check_id ? '· ' + esc(o.check_id) : ''}</div><p>${esc(o.reason)}</p>${o.basis_refs ? ui`<p class="muted">依据：${esc(o.basis_refs.join(', '))}</p>` : ''}${o.consequence ? ui`<p class="muted">影响：${esc(o.consequence)}</p>` : ''}${lensBasis(o)}${f ? ui`<div class="human">人工决定：${decision ? esc(systemLabel(decision)) + ' · ' + esc(f.human_reason) : tr('尚未裁决')}</div>${actions ? '<ul>' + actions + '</ul>' : ''}${provenanceTrace(f)}` : ''}${buttons}</div>`;
  }).join('');
  const cite = state.citations.filter(c => (c.dependent_claims || []).includes(selectedClaim) || state.relations.some(r => r.from === c.id && r.to === selectedClaim)).map(c => ui`<div class="card"><b>${esc(c.id)} · ${esc(c.text)}</b><div class="${c.verification_state === 'verified' ? 'deterministic' : 'model'}">${esc(systemLabel(c.verification_state))}</div></div>`).join('');
  const history = state.lineage.filter(x => x.pair.includes(state.project.version_id)).flatMap(x => x.proposals.filter(p => (p.from_claims || []).includes(target) || (p.to_claims || []).includes(target))).map(p => ui`<div class="card"><b>${esc(systemLabel(p.relation))}</b> · ${esc((p.from_claims || []).join(', ') || systemLabel('new'))} → ${esc((p.to_claims || []).join(', ') || systemLabel('removed'))}<div class="human">${p.human_decision ? tr('人工：') + esc(systemLabel(p.human_decision.decision)) + ' · ' + esc(p.human_decision.human_note) : tr('等待人工确认')}</div></div>`).join('');
  const resolutions = state.resolutions.filter(r => (r.descendant_claims || []).includes(target) || r.original_finding.target_claim === target).map(r => {
    const final = r.human_decision?.final_status || tr('等待人工确认');
    const actions = r.revision_actions.map(a => ui`<li>${esc(systemLabel(a.action_type))} · ${esc(a.text)}</li>`).join('');
    return ui`<div class="card"><b>${esc(r.resolution_id)} · ${esc(systemLabel(final))}</b><p>原问题：${esc(r.original_finding.reason)}</p><p class="muted">原 Lens：${esc(systemLabel(r.lens.id))}${r.lens.check_id ? ' · ' + esc(r.lens.check_id) : ''}</p>${actions ? '<ul>' + actions + '</ul>' : ''}<p>${esc((r.descendant_claims || []).join(', ') || systemLabel('removed'))} · 重测提案 ${esc(systemLabel(r.proposed_status || 'pending'))}</p>${r.human_decision ? ui`<div class="human">人工确认：${esc(r.human_decision.reason)}</div>` : ''}</div>`;
  }).join('');
  $('review').innerHTML = (html || tr('<div class="empty">这个 Claim 在所选 Lens 下没有当前结果</div>')) + ui`<div class="section"><h3>Citation provenance</h3>${cite || tr('<div class="empty">没有绑定的 Citation provenance</div>')}</div><div class="section"><h3>Claim Lineage</h3>${history || tr('<div class="empty">尚无跨版本 Lineage</div>')}</div><div class="section"><h3>Finding Resolution</h3>${resolutions || tr('<div class="empty">没有继承的旧 Finding</div>')}</div>`;
  document.querySelectorAll('[data-decide]').forEach(b => b.onclick = () => openDecision(b.dataset.decide));
}
function selectClaim(id) {
  selectedClaim = id;
  renderManuscript();
  renderClaims();
  renderReview();
  document.querySelector(ui`.line[data-claims*="${CSS.escape(id)}"]`)?.scrollIntoView({
    behavior: 'smooth',
    block: 'center'
  });
}
function openDecision(id) {
  pendingFinding = id;
  const f = state.findings.find(x => x.finding_id === id);
  $('decisionTitle').textContent = (f?.decision ? tr('复议 ') : tr('裁决 ')) + id;
  $('decisionValue').value = f?.decision || 'accept';
  $('decisionReason').value = f?.human_reason || '';
  $('actionText').value = '';
  $('decisionError').innerHTML = '';
  toggleAction();
  $('decisionDialog').showModal();
}
function toggleAction() {
  $('actionFields').style.display = $('decisionValue').value === 'accept' ? 'block' : 'none';
}
$('decisionValue').onchange = toggleAction;
$('decisionDialog').addEventListener('cancel', event => {
  if (researchMutationPending) event.preventDefault();
});
$('version').onchange = () => {
  selectedClaim = null;
  load($('version').value).catch(showFatal);
};
$('historyButton').onclick = renderHistory;
$('decisionForm').onsubmit = async e => {
  if (e.submitter?.value === 'cancel') return;
  e.preventDefault();
  const decision = $('decisionValue').value;
  const actions = decision === 'accept' ? [{
    action_type: $('actionType').value,
    text: $('actionText').value.trim()
  }] : [];
  const payload = {
    finding_id: pendingFinding,
    decision,
    reason: $('decisionReason').value.trim(),
    actions
  };
  try {
    state = await requestJson('/api/adjudications', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Argument-Workbench-Token': TOKEN
      },
      body: JSON.stringify(payload)
    }, true);
    $('decisionDialog').close();
    render();
  } catch (err) {
    const uncertain = err.transportFailure || err.uncertainMutation;
    if (uncertain) {
      try {
        await load($('version').value);
      } catch (ignore) {}
    }
    const message = uncertain ? tr('本地服务连接中断或发生内部错误；状态已尝试刷新。操作可能已经完成，请先检查当前裁决，不要直接重复提交。') : err.message;
    $('decisionError').innerHTML = ui`<div class="error">${esc(message)}</div>`;
  }
};
function showFatal(err) {
  document.body.innerHTML = ui`<div class="error" style="margin:30px">${esc(err.message)}</div>`;
}
function renderResearchView() {
  if (state) {
    render();
    if ($('historyDialog').open) renderHistory();
    if (pendingFinding) {
      const f = state.findings.find(x => x.finding_id === pendingFinding);
      $('decisionTitle').textContent = tr(f?.decision ? '复议 ' : '裁决 ') + pendingFinding;
    }
  }
}
applyResearchLanguage();
load().catch(showFatal);
