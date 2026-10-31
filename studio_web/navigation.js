function bindWorkflowNavigation() {
  const targets = {
    extraction: ['#correction-editor:not(.hidden) #correction', '#extraction-recovery', '#source-preview', '#workspace-search'], context: '#confirm-context', local: '#run-precheck',
    ai: '#ai-request', adjudication: ['.finding-card', '.argument-assessment', '.review-empty'],
    bridge: '.revision-action-card, #prepare-bridge',
    export: '.download, #export-final, #export-results',
  };
  root.querySelectorAll('[data-stage]').forEach(button => {
    const selectors = targets[button.dataset.stage];
    const resolveTarget = () => (Array.isArray(selectors) ? selectors : [selectors])
      .flatMap(selector => [...root.querySelectorAll(selector)])
      .find(target => target && !target.matches(':disabled') && !target.closest('.hidden, [hidden]'));
    button.disabled = !resolveTarget();
    button.onclick = () => {
      // Correction can open or close without rerendering the workflow.
      const target = resolveTarget();
      if (!target) return;
      let parent = target;
      while (parent && parent !== root) {
        if (parent.tagName === 'DETAILS') parent.open = true;
        parent = parent.parentElement;
      }
      target.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center'});
      if (target.tabIndex < 0) target.tabIndex = -1;
      target.focus({preventScroll: true});
    };
  });
  root.querySelectorAll('label').forEach(label => {
    const field = label.nextElementSibling;
    if (!label.htmlFor && field?.matches('input[id],select[id],textarea[id]')) label.htmlFor = field.id;
  });
}
