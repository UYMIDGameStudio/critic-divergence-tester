# 0.2 交付工程记录

范围：现有本地单人文书与论证审查产品。云账号、支付、多人协作和直接模型调用
没有纳入本次工程范围；模型仍由使用者选择，所有实质批准仍由人提交。

## 实现与验收对应

| 原问题 | 实现 | 验证 |
| --- | --- | --- |
| 检测损坏后缺少恢复途径 | 项目外完整 ZIP 备份；逐文件大小/摘要及恢复后完整性验证；只恢复到新项目；删除前先备份 | `test_delivery` 的往返、篡改、越界路径和不覆盖测试 |
| 多文件操作中断 | 外层写操作保留恢复清单；覆盖前保存旧字节；提交标记区分完成和中断；下次打开自动恢复 | 子进程写入后强制退出、模拟磁盘错误测试 |
| 升级和旧项目兼容 | 0.2 延用 schema 1；拒绝未知版本；升级检查及默认库升级前备份；保留旧程序版本 | 未来版本拒绝且不重写原件；便携安装后自检 |
| 操作次数和输入丢失 | 显式勾选后的批量裁决；项目内表单草稿；恢复输入与位置；重复请求回执；跨项目写入拒绝 | 批量选择校验、重复请求测试；浏览器刷新恢复 |
| 定位和结构修改不足 | 人工定位校正使旧决定失效；连续普通段落范围替换；按空行分段；范围重叠拒绝 | 定位失效及跨段落起草—批准—复查测试 |
| 修改文本需自行拼接 | 每个已确认操作可获取绑定范围与决定摘要的 AI 起草提示；保存原始返回并校验后生成待批准 Hunk | 请求绑定、结构修改和来源记录测试 |
| Word 输出缺少保真 | 保留未修改的 OOXML 包内部分；普通段落/简单单元格原位净稿；段内替换额外提供原生修订副本 | 原件部件字节对比、重新解析及修订元素检查；复杂元素明确拒绝 |
| 无修改结束状态不清楚 | 全部驳回导出 `本轮未修改稿` 和 no-change 状态，保留风险说明 | 无修改完成包测试 |
| 部分流程无 CLI 对等入口 | `studio-manage action` 使用同一服务执行完整 Action/Hunk/外部复查流程 | 维护命令与 wheel 安装验证 |
| 安装发行依赖开发环境 | 版本化便携包、安装/升级/卸载脚本、包摘要、许可文件、独立自检 | Windows 本机构建，项目目录外运行便携自检 |
| 浏览器、安全和容量验证不足 | 真实 Chromium 操作链；Host/Origin 校验；连接数/读取超时限制；中文下载头；日志限制与去除异常参数 | 浏览器回归、HTTP 隔离测试、1000 段容量脚本 |

## 使用维护命令

```powershell
python critic_runner.py studio-manage backup PROJECT backup.zip
python critic_runner.py studio-manage restore backup.zip LIBRARY
python critic_runner.py studio-manage verify PROJECT
python critic_runner.py studio-manage upgrade-check PROJECT
python critic_runner.py studio-manage action PROJECT request.json
```

`request.json` 使用与本机界面相同的 `action` 和 `data`。例如：

```json
{
  "request_id": "manual-operation-000001",
  "action": "set_revision_action_operation",
  "data": {
    "action_id": "从修改计划读取实际 ID",
    "operation": "replace_block",
    "reason": "作者确认修改范围"
  }
}
```

请求编号可用于重复提交的幂等保护；同一编号不能换成不同内容。起草、接受或
拒绝修改、生成版本、导出和外部复查均调用现有项目服务，不走第二套写入逻辑。

## 验证命令与证据

2026-09-11 本机验证（Windows 11、Python 3.14.7）：完整回归 344 项，342 项通过，
2 项因环境跳过；真实 Chrome 操作链通过，无页面脚本异常。0.2 wheel 已构建、
安装，并在隔离导入模式下验证模块、协议资源、文书/学术流程及统一服务器。
便携程序另行通过项目目录外的内置自检。远端 CI 和其他系统尚未实际执行。

```powershell
python -m unittest discover -s test -p "test_*.py" -q
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
python scripts/benchmark_delivery.py
python -m pip install -r scripts/release-requirements.txt
python scripts/build_portable.py
```

浏览器脚本在临时独立项目库运行，覆盖上传、识别确认、表单保存/刷新恢复、
上下文、本地预检、裁决、操作选择、修改提案、批准、版本复查、Word 下载、备份
及恢复；不会接触现有作者项目，也不会调用外部模型。页面截图和机器结果写入
`dist/browser-verification/`。容量脚本写入 `dist/capacity-report.json`。

本机第一次容量运行：1000 段、60,908 字节；导入 0.190 秒、预检 0.225 秒、
读取工作台 0.140 秒、备份 0.059 秒、恢复 0.123 秒。这是固定文本样本的一次
测量，不是扫描 PDF、复杂 Word、长期大量历史或所有机器上的性能保证。

## 支持边界和公开发行剩余验收

- 恢复机制针对进程中断、普通写入失败和本地文件不一致；硬件故障时仍需独立
  介质备份。它不提供发布者签名或对完整磁盘写入者的认证。
- 0.2 不迁移旧内容，因为项目 schema 未变；未来新增 schema 必须新增明确迁移
  和回退实现。回退旧程序应恢复升级前备份到新项目，不能假定旧版理解新版记录。
- 原位 Word 路径不编辑复杂表格结构、图片或嵌入对象。涉及不支持的修改位置时
  导出规范化副本并附原因。原生修订副本仅覆盖段内文本替换；复杂版式仍需真实
  Word 渲染验收，不能以 XML 检查代替视觉验收。
- 本次 Windows 便携包包含 pypdf 文本解析，不内置扫描 OCR；冻结程序不会尝试
  把自身当作 Python 执行 pip。其他适配器继续由源码版的可选环境支持。
- 本地构建未签名；签名凭据、其他目标系统干净环境验收、远端 CI 实际运行和
  外部真人验收尚不能由本次本机验证代替，因此保留 experimental preview 标识。
- 构建使用 [PyInstaller 的官方打包接口](https://pyinstaller.org/en/stable/usage.html)。
  程序安装不要求管理员权限；卸载保留项目和备份，旧程序版本供回退使用。
