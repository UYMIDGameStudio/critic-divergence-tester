function bindDelivery() {
  const quit = document.createElement('button');
  quit.className = 'secondary';
  quit.textContent = tr('退出工作台');
  quit.onclick = quitStudio;
  root.prepend(quit);
  if (!state.selected) {
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = tr(
      '<h2>恢复项目备份</h2><p>验证完整备份并恢复为独立项目。已有项目不会被覆盖。较大备份可使用维护工具。</p><input id="backup-file" type="file" accept=".zip"><button id="restore-backup">校验并恢复</button>',
    );
    root.appendChild(card);
    document.getElementById('restore-backup').onclick = async () => {
      try {
        const file = document.getElementById('backup-file').files[0];
        if (!file) throw Error(tr('请选择备份文件'));
        if (file.size > 30 * 1024 * 1024)
          throw Error(
            tr('浏览器恢复支持 30 MiB 以内备份；请使用维护工具恢复较大备份'),
          );
        let binary = '';
        for (const byte of new Uint8Array(await file.arrayBuffer()))
          binary += String.fromCharCode(byte);
        await act('restore_backup', { content_base64: btoa(binary) });
      } catch (error) {
        errorMessage(error.message);
      }
    };
    return;
  }
  const toolbar = document.createElement('div');
  toolbar.className = 'card row';
  toolbar.innerHTML = ui`<button class="secondary" id="create-backup">备份当前项目</button><button class="secondary" id="download-draft">下载当前草稿</button><button class="secondary" id="retry-draft">保存草稿</button>${draftContext?.legacy ? tr('<button class="secondary" id="legacy-draft">找回旧版草稿</button>') : ''}<span id="draft-status" role="status">表单草稿保存在本机</span><small>版本 ${esc(state.app_version)}</small>`;
  root.prepend(toolbar);
  document.getElementById('create-backup').onclick = () => act('create_backup');
  document.getElementById('download-draft').onclick = () => downloadDraft();
  document.getElementById('retry-draft').onclick = () =>
    saveDraft().catch((error) => errorMessage(error.message));
  document
    .getElementById('legacy-draft')
    ?.addEventListener('click', () => downloadDraft(true));
  const blocks = state.selected.extraction?.blocks || [];
  if (state.selected.findings?.length) {
    const batch = document.createElement('details');
    batch.className = 'card';
    batch.innerHTML = tr(
      '<summary>批量处理明确选中的问题</summary><p>先逐项勾选问题；批量处理仍为每条问题保存独立决定。</p><input id="batch-reason" placeholder="对这些问题适用的共同裁决理由"><button class="batch-decide" data-value="accept">接受选中项</button> <button class="batch-decide secondary" data-value="reject">拒绝选中项</button> <button class="batch-decide secondary" data-value="defer">暂缓选中项</button>',
    );
    toolbar.after(batch);
    batch.querySelectorAll('button').forEach(
      (button) =>
        (button.onclick = () =>
          act('decide_finding_batch', {
            finding_ids: [
              ...document.querySelectorAll('.select-finding:checked'),
            ].map((el) => el.dataset.id),
            decision: button.dataset.value,
            reason: document.getElementById('batch-reason').value,
          })),
    );
  }
  document.querySelectorAll('.finding-card').forEach((card) => {
    const id = card.querySelector('.decision')?.dataset.id;
    if (!id) return;
    const selection = document.createElement('label');
    selection.innerHTML = ui`<input type="checkbox" class="select-finding" data-id="${esc(id)}"> 选中此问题用于批量裁决`;
    card.prepend(selection);
    const details = document.createElement('details');
    details.innerHTML = ui`<summary>校正原文定位</summary><p>新定位需要重新裁决，旧批准会失效。</p><select id="location-${esc(id)}">${blocks
      .filter(
        (b) =>
          b.text &&
          !['table', 'image_placeholder', 'page_break'].includes(b.kind),
      )
      .map(
        (b) =>
          `<option value="${esc(b.block_id)}">${esc(sourceBlockText(b).slice(0, 70))} · ${esc(b.block_id)}</option>`,
      )
      .join(
        '',
      )}</select><input id="location-reason-${esc(id)}" placeholder="说明为什么应定位到此处"><button class="secondary">保存定位校正</button>`;
    details.querySelector('button').onclick = () =>
      act('correct_finding_location', {
        finding_id: id,
        block_id: document.getElementById('location-' + id).value,
        reason: document.getElementById('location-reason-' + id).value,
      });
    card.appendChild(details);
  });
  (state.selected.revision_workspace?.actions || []).forEach((action) => {
    const button = document.querySelector(
      `.confirm-operation[data-action-id="${CSS.escape(action.action_id)}"]`,
    );
    if (button && action.block_kind !== 'table_cell') {
      const select = document.getElementById('operation-' + action.action_id),
        option = document.createElement('option');
      option.value = 'replace_range';
      option.textContent = tr('替换连续段落');
      if (![...select.options].some((o) => o.value === 'replace_range'))
        select.appendChild(option);
      const from = blocks.findIndex((b) => b.block_id === action.block_id),
        range = document.createElement('select');
      range.id = 'range-end-' + action.action_id;
      let options = '';
      for (let i = from + 1; i < blocks.length; i++) {
        const b = blocks[i];
        if (!['paragraph', 'heading', 'list_item'].includes(b.kind)) break;
        options += `<option value="${i}">${esc(sourceBlockText(b).slice(0, 70))}</option>`;
      }
      range.innerHTML =
        tr('<option value="">范围结束段落（范围替换时必选）</option>') +
        options;
      button.before(range);
      button.onclick = () => {
        const operation = select.value,
          end = Number(range.value);
        if (operation === 'replace_range' && !range.value)
          return errorMessage(tr('请选择范围结束段落'));
        act('set_revision_action_operation', {
          action_id: action.action_id,
          operation,
          reason: document.getElementById(
            'operation-reason-' + action.action_id,
          ).value,
          ...(operation === 'replace_range'
            ? { block_ids: blocks.slice(from, end + 1).map((b) => b.block_id) }
            : {}),
        });
      };
    }
    const editor = document.getElementById('revision-' + action.action_id);
    if (editor && action.operation) {
      const panel = document.createElement('details');
      panel.innerHTML = ui`<summary>由 AI 起草此项修改</summary><button class="secondary copy-draft">获取起草提示词</button><textarea class="draft-prompt" readonly placeholder="提示词将在此处显示"></textarea><textarea id="draft-response-${esc(action.action_id)}" placeholder="粘贴 AI 返回的完整 JSON"></textarea><button class="import-draft">导入为待批准修改</button>`;
      panel.querySelector('.copy-draft').onclick = async () => {
        try {
          const request = await api(
            '/api/drafting?project=' +
              encodeURIComponent(state.selected.directory) +
              '&action_id=' +
              encodeURIComponent(action.action_id),
          );
          panel.querySelector('.draft-prompt').value = request.prompt;
          try {
            await navigator.clipboard.writeText(request.prompt);
          } catch (ignored) {}
        } catch (error) {
          errorMessage(error.message);
        }
      };
      panel.querySelector('.import-draft').onclick = () =>
        act('import_revision_draft', {
          action_id: action.action_id,
          response: document.getElementById(
            'draft-response-' + action.action_id,
          ).value,
        });
      editor.after(panel);
    }
    if (action.operation && !state.selected.revision_workspace.revision) {
      const card = document.querySelector(
        `.revision-action-card[data-action-id="${CSS.escape(action.action_id)}"]`,
      );
      if (card) {
        const change = document.createElement('details');
        change.innerHTML = ui`<summary>更改操作或重新起草</summary><p>更改操作会使旧修改批准失效。</p><select class="change-operation" id="change-operation-${esc(action.action_id)}">${(action.block_kind === 'table_cell' ? ['replace_table_cell'] : ['replace_block', 'insert_before', 'insert_after', 'delete_block', 'append_section']).map((value) => `<option value="${value}">${esc({ replace_block: tr('替换段落'), insert_before: tr('段前插入'), insert_after: tr('段后插入'), delete_block: tr('删除段落'), append_section: tr('追加章节'), replace_table_cell: tr('替换单元格') }[value])}</option>`).join('')}</select><input id="change-operation-reason-${esc(action.action_id)}" class="change-operation-reason" placeholder="说明更改理由"><button class="secondary">保存操作并重新起草</button>`;
        change.querySelector('button').onclick = () =>
          act('set_revision_action_operation', {
            action_id: action.action_id,
            operation: change.querySelector('select').value,
            reason: change.querySelector('input').value,
          });
        card.appendChild(change);
      }
    }
  });
}
