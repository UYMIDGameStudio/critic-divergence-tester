function bindWorkflowNavigation() {
  const targets = {
    extraction: '#source-preview', context: '#confirm-context', local: '#run-precheck',
    ai: '#ai-request', adjudication: '.finding-card',
    bridge: '.revision-action-card, #prepare-bridge',
    export: '.download, #export-final, #export-results',
  };
  root.querySelectorAll('[data-stage]').forEach(button => {
    const target = root.querySelector(targets[button.dataset.stage]);
    button.disabled = !target;
    button.onclick = () => {
      let parent = target;
      while (parent && parent !== root) {
        if (parent.tagName === 'DETAILS') parent.open = true;
        parent = parent.parentElement;
      }
      target.scrollIntoView({behavior: 'smooth', block: 'center'});
      target.focus({preventScroll: true});
    };
  });
  root.querySelectorAll('label').forEach(label => {
    const field = label.nextElementSibling;
    if (!label.htmlFor && field?.matches('input[id],select[id],textarea[id]')) label.htmlFor = field.id;
  });
}
