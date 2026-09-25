async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-Document-Review-Token': TOKEN,
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    const error = Error(
      tr(
        '无法连接本地服务。请确认启动 Studio 的终端仍在运行，并只使用本次启动自动打开的页面；旧标签页的端口可能已经失效。',
      ),
      { cause },
    );
    error.transportFailure = true;
    throw error;
  }
  let value;
  try {
    value = await response.json();
  } catch (cause) {
    const error = Error(
      ui`本地服务响应中断或格式无效（HTTP ${response.status}）。操作可能已经完成。`,
      { cause },
    );
    error.transportFailure = true;
    throw error;
  }
  if (!response.ok) {
    const rawError = value.error || tr('操作失败');
    const errorText = path === '/api/upload' && rawError === '文件名含路径、保留名称或不支持的字符，请重命名原文件后重新导入'
      ? tr(rawError) : rawError;
    const error = Error(['prepare_adversarial_review', 'prepare_adversarial_assessment',
      'import_adversarial_response'].includes(body?.action)
      ? adversarialErrorMessage(errorText)
      : ['create_backup', 'restore_backup', 'delete_project'].includes(body?.action)
        ? maintenanceErrorMessage(errorText, body.action) : errorText);
    error.uncertainMutation = Boolean(body) && response.status >= 500;
    throw error;
  }
  return value;
}
function errorMessage(message) {
  const box = document.getElementById('err');
  if (box) box.textContent = message;
  else alert(message);
}
function operationStatus(message, kind) {
  operationBox.textContent = message;
  operationBox.className = `operation-status ${kind}`;
}
async function download(url, name) {
  const response = await fetch(url, {
    headers: { 'X-Document-Review-Token': TOKEN },
  });
  if (!response.ok) {
    const value = await response.json();
    return errorMessage(value.error || tr('下载失败'));
  }
  const blob = await response.blob(),
    link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}
