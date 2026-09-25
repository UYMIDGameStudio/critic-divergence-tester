const TOKEN = __TOKEN__,
  root = document.getElementById('app'),
  operationBox = document.getElementById('operation-status');
let state = null;
const reviewConfig = __REVIEW_CONFIG__;
let critics = reviewConfig.critics;
const actionLabels = {
  run_local_prechecks: '本地确定性预检',
  prepare_ai_audits: 'AI 审查协议生成',
  import_ai_audit: 'AI 审查导入',
  prepare_adversarial_review: '独立辩护任务生成',
  prepare_adversarial_assessment: '证据复核任务生成',
  import_adversarial_response: '深审响应导入',
  confirm_extraction: '识别确认',
  confirm_context: '上下文确认',
  prepare_bridge: '修改计划生成',
  finalize_revision: '修改稿生成与复审',
  export: '导出',
};
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[
        c
      ],
  );
