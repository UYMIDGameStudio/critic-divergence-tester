function bind() {
  document.querySelectorAll('.open-research').forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          const opened = await fetch('/research/api/open', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Argument-Workbench-Token': TOKEN,
            },
            body: JSON.stringify({ directory: b.dataset.dir }),
          });
          const value = await opened.json();
          if (!opened.ok) throw Error(value.error);
          location.href = '/research/';
        } catch (error) {
          errorMessage(error.message);
        }
      }),
  );
  document.getElementById('upload')?.addEventListener('click', uploadFile);
  document
    .querySelectorAll('.open-project')
    .forEach((b) => (b.onclick = () => openProject(b.dataset.dir)));
  document
    .querySelectorAll('.delete-project')
    .forEach((b) => (b.onclick = () => removeProject(b.dataset.dir)));
  document
    .getElementById('delete-selected')
    ?.addEventListener('click', (e) =>
      removeProject(e.currentTarget.dataset.dir),
    );
  document
    .getElementById('back')
    ?.addEventListener('click', () => act('close_project'));
  document
    .getElementById('repair-all')
    ?.addEventListener('click', () => repair());
  document
    .querySelectorAll('.repair-one')
    .forEach((b) => (b.onclick = () => repair([b.dataset.name])));
  document
    .querySelector('[data-action="confirm-extraction"]')
    ?.addEventListener('click', () =>
      act('confirm_extraction', { choice: 'confirm' }),
    );
  document
    .querySelector('[data-action="continue-extraction"]')
    ?.addEventListener('click', () =>
      act('confirm_extraction', { choice: 'continue_with_warning' }),
    );
  document
    .querySelector('[data-action="replace-file"]')
    ?.addEventListener('click', () => act('close_project'));
  document
    .getElementById('retry-extraction')
    ?.addEventListener('click', () => act('retry_extraction'));
  document
    .getElementById('show-correction')
    ?.addEventListener('click', (event) => {
      const area = document.getElementById('correction');
      if (area.classList.contains('hidden')) {
        area.classList.remove('hidden');
        event.currentTarget.textContent = tr('提交修正文本');
      } else {
        act('confirm_extraction', {
          choice: 'correct',
          corrected_text: area.value,
        });
      }
    });
  document
    .getElementById('confirm-context')
    ?.addEventListener('click', confirmContext);
  document.getElementById('run-precheck')?.addEventListener('click', () =>
    act('run_local_prechecks', {
      critics: [...document.querySelectorAll('.critic:checked')].map(
        (x) => x.value,
      ),
      provider: document.getElementById('ai-provider')?.value || tr('手动导入'),
      model: document.getElementById('ai-model')?.value || tr('未声明模型'),
    }),
  );
  document.getElementById('prepare-ai')?.addEventListener('click', () =>
    act('prepare_ai_audits', {
      critics: Object.keys(critics),
      provider: document.getElementById('ai-provider').value,
      model: document.getElementById('ai-model').value,
    }),
  );
  document.getElementById('import-ai')?.addEventListener('click', () => {
    const r = selectedRequest();
    if (!r) return errorMessage(tr('请先导出协议'));
    act('import_ai_audit', {
      request_id: r.request_id,
      critic: r.critic,
      provider: r.provider,
      model: r.model,
      binding_mode: document.getElementById('binding-mode').value,
      response: document.getElementById('ai-response').value,
    });
  });
  document
    .getElementById('copy-protocol')
    ?.addEventListener('click', () => copyPrompt(selectedRequest()));
  document
    .querySelectorAll('.copy-request')
    .forEach(
      (b) =>
        (b.onclick = () =>
          copyPrompt(
            state.selected.ai_requests.find(
              (x) => x.request_id === b.dataset.id,
            ),
          )),
    );
  document
    .getElementById('download-protocols')
    ?.addEventListener('click', () =>
      download('/api/protocols.zip', 'ai-review-protocols.zip'),
    );
  document
    .getElementById('previous-request')
    ?.addEventListener('click', () => moveRequest(-1));
  document
    .getElementById('next-request')
    ?.addEventListener('click', () => moveRequest(1));
  document.querySelectorAll('.decision').forEach(
    (b) =>
      (b.onclick = () =>
        act('decide_finding', {
          finding_id: b.dataset.id,
          decision: b.dataset.value,
          reason: document.getElementById(`reason-${b.dataset.id}`).value,
          corrected_action:
            document.getElementById(`action-${b.dataset.id}`).value || null,
        })),
  );
  document
    .getElementById('prepare-bridge')
    ?.addEventListener('click', () => act('prepare_bridge'));
  document
    .getElementById('export-results')
    ?.addEventListener('click', () => act('export'));
  document
    .querySelectorAll('.download')
    .forEach(
      (b) =>
        (b.onclick = () =>
          download(
            `/api/download?path=${encodeURIComponent(b.dataset.path)}`,
            b.dataset.path.split('/').pop(),
          )),
    );
  document
    .querySelectorAll('.open-folder')
    .forEach(
      (b) =>
        (b.onclick = () =>
          act('open_export_folder', { relative_path: b.dataset.path })),
    );
  ['filter-critic', 'filter-severity', 'filter-status'].forEach((id) =>
    document.getElementById(id)?.addEventListener('change', filterFindings),
  );
  document
    .getElementById('source-search')
    ?.addEventListener('input', filterSourcePreview);
  document
    .getElementById('workspace-search')
    ?.addEventListener('input', filterWorkspace);
}
function bindRevision() {
  document
    .getElementById('show-all-groups')
    ?.addEventListener('click', (event) => {
      document
        .querySelectorAll('.extra-finding-group')
        .forEach((group) => group.classList.remove('hidden'));
      event.currentTarget.remove();
    });
  document.querySelectorAll('.confirm-operation').forEach(
    (button) =>
      (button.onclick = () => {
        const id = button.dataset.actionId;
        act('set_revision_action_operation', {
          action_id: id,
          operation: document.getElementById(`operation-${id}`).value,
          reason: document.getElementById(`operation-reason-${id}`).value,
        });
      }),
  );
  document.querySelectorAll('.propose-hunk').forEach(
    (button) =>
      (button.onclick = () => {
        const id = button.dataset.actionId;
        act('propose_revision_hunk', {
          action_id: id,
          revised_text: document.getElementById(`revision-${id}`).value,
          rationale: document.getElementById(`revision-reason-${id}`).value,
          provenance:
            document.getElementById(`revision-provenance-${id}`)?.value ||
            'human-authored',
        });
      }),
  );
  document.querySelectorAll('.decide-hunk').forEach(
    (button) =>
      (button.onclick = () =>
        act('decide_revision_hunk', {
          hunk_id: button.dataset.hunkId,
          decision: button.dataset.decision,
          reason: document.getElementById(
            `hunk-reason-${button.dataset.hunkId}`,
          ).value,
        })),
  );
  document
    .getElementById('finalize-revision')
    ?.addEventListener('click', () => act('finalize_revision'));
  document
    .getElementById('export-final')
    ?.addEventListener('click', () => act('export'));
  document.querySelectorAll('.copy-external-recheck').forEach(
    (button) =>
      (button.onclick = async () => {
        const request =
          state.selected?.revision_workspace?.external_recheck?.requests?.find(
            (item) => item.critic === button.dataset.critic,
          );
        try {
          await navigator.clipboard.writeText(request?.prompt || '');
          alert(tr('复审协议已复制'));
        } catch (error) {
          errorMessage(tr('浏览器未允许复制，请展开协议后手动复制'));
        }
      }),
  );
  document.querySelectorAll('.import-external-recheck').forEach(
    (button) =>
      (button.onclick = () =>
        act('import_external_recheck', {
          revision_id: button.dataset.revisionId,
          critic: button.dataset.critic,
          provider: document.getElementById(
            `external-provider-${button.dataset.critic}`,
          ).value,
          model: document.getElementById(
            `external-model-${button.dataset.critic}`,
          ).value,
          binding_mode: document.getElementById(
            `external-binding-${button.dataset.critic}`,
          ).value,
          response: document.getElementById(
            `external-response-${button.dataset.critic}`,
          ).value,
        })),
  );
  document.querySelectorAll('.external-resolution').forEach(
    (button) =>
      (button.onclick = () => {
        const critic = button
          .closest('details')
          .querySelector('.copy-external-recheck').dataset.critic;
        act('decide_external_resolution', {
          revision_id: button.dataset.revisionId,
          result_id: button.dataset.resultId,
          finding_id: button.dataset.findingId,
          state: button.dataset.state,
          reason: document.getElementById(
            `external-reason-${critic}-${button.dataset.findingId}`,
          ).value,
        });
      }),
  );
  document
    .getElementById('start-followup-round')
    ?.addEventListener('click', (button) =>
      act('start_followup_round', {
        revision_id: button.currentTarget.dataset.revisionId,
      }),
    );
}
async function repair(names) {
  const p = document.getElementById('repair-progress');
  if (p) p.textContent = tr('正在安装并重新自检…');
  await act('repair_environment', names ? { names } : {});
}
function confirmContext() {
  const value = (id) => document.getElementById(id).value;
  act('confirm_context', {
    review_profile: value('review_profile'),
    discipline:
      value('review_profile') === 'document' ? 'general' : value('discipline'),
    research_type:
      value('review_profile') === 'document'
        ? 'unspecified'
        : value('research_type'),
    user_provided_materials: value('user_provided_materials')
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean),
    document_type: value('document_type'),
    jurisdiction: value('jurisdiction'),
    effective_date: value('effective_date'),
    publisher_type: value('publisher_type'),
    audience: value('audience'),
    publication_status: value('publication_status'),
    ...Object.fromEntries(
      [
        'minors',
        'fees',
        'sponsorship',
        'contract',
        'personal_information',
        'intellectual_property',
      ].map((k) => [
        `involves_${k}`,
        document.getElementById(`involves_${k}`).checked,
      ]),
    ),
  });
}
async function copyPrompt(request) {
  if (!request) return errorMessage(tr('请先导出协议'));
  try {
    await navigator.clipboard.writeText(request.prompt);
    alert(tr('协议已复制'));
  } catch (error) {
    errorMessage(tr('浏览器未允许复制，请展开协议后手动复制'));
  }
}
function moveRequest(delta) {
  const select = document.getElementById('ai-request');
  if (select) {
    select.selectedIndex = Math.max(
      0,
      Math.min(select.options.length - 1, select.selectedIndex + delta),
    );
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
}
function filterFindings() {
  const c = document.getElementById('filter-critic')?.value || '',
    s = document.getElementById('filter-severity')?.value || '',
    t = document.getElementById('filter-status')?.value || '';
  document
    .querySelectorAll('.finding-card')
    .forEach(
      (card) =>
        (card.hidden = Boolean(
          (c && card.dataset.critic !== c) ||
            (s && card.dataset.severity !== s) ||
            (t && card.dataset.status !== t),
        )),
    );
}
function filterSourcePreview(event) {
  const q = event.target.value.toLowerCase(),
    blocks = state.selected.extraction.blocks || [];
  document.getElementById('source-preview').textContent = blocks
    .filter(
      (b) =>
        !q ||
        sourceBlockText(b).toLowerCase().includes(q) ||
        String(b.location?.block_id || b.block_id)
          .toLowerCase()
          .includes(q),
    )
    .map(
      (b) =>
        `[${b.location?.block_id || b.block_id} · page ${b.location?.page || '-'}] ${sourceBlockText(b)}`,
    )
    .join('\n\n');
}
function filterWorkspace(event) {
  const q = event.target.value.toLowerCase();
  document
    .querySelectorAll('.source-block')
    .forEach(
      (block) =>
        (block.hidden = Boolean(
          q && !block.textContent.toLowerCase().includes(q),
        )),
    );
}
function selectedRequest() {
  const id = document.getElementById('ai-request')?.value;
  return state.selected?.ai_requests?.find((x) => x.request_id === id);
}
