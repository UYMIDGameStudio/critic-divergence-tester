# 0.2.2 交付工程记录

状态：本机源码与候选发行程序验收通过，尚未满足完整商业交付定义。不得把本机自动化通过
替代 [原产品完成定义](product-definition-of-done.md) 所要求的真实作者验收。
上一版 0.2.1 的 ZIP 和校验文件保持原样，本轮使用新版本号。

## 本轮实现

- 首页明确区分新稿审查与已有报告直接修稿；繁體中文和 English 均有对应入口。
- 复查确认不再预选“已解决”。没有既有人类决定的条目必须显式选择最终状态；
  既有决定和理由回显，模型未解决／部分解决结果不会因默认选项而被批准为解决。
- 研究流程 13 个写服务统一使用可恢复事务，V2 与应用记录共同提交；专业裁决
  的决定、修改动作和计划共同提交。批次保留已完成单条，回滚当前未完成单条。
- 论证 IR 与报告修稿管线的错误模型字段按验证失败完整归档；归档写失败则回滚。显式运行编号不能用绝对路径
  选择另一项目，防止跨项目写入。
- 旧二进制 Office 转换后以实际转换产物的摘要和大小校验现代格式解析；原件绑定
  保持原始字节，并保存转换输入／输出摘要、大小及实际观察到的转换器版本。
- 便携包增加 PDFium 页面渲染。扫描件的临时灰度图无需 Pillow／NumPy；原件不变。
  修复混合 PDF 页序、文字行边界、资源关闭，渲染前限制页面尺寸和像素。
- Windows OCR 探测常见 Tesseract 安装目录，后台调用不弹控制台；完整 OCR 能力
  检查同时要求引擎、语言包和页面渲染组件。
- 同一原始 DOC/XLS/PPT 重复转换时，ZIP 时间戳等元数据差异不再改变段落／表格
  编号；现代解析器仍校验实际转换字节，原件身份不能绕过该检查。
- 修复协议验证器错误类型导致的归档前崩溃；严格拒绝布尔／浮点版本、非有限数字、
  非法 Unicode、伪人工来源与跨版本引用。独立 bundle 校验绑定对象与实际原始字节。
- 修复本机 HTTP 的外来 Host、歧义鉴权头、非法正文边界及慢请求阻塞；研究写入
  绑定页面所见的项目／任务上下文，旧标签页和重复提交不能写入另一项目。
- 加入 [文章定位与细读依据](review-quality-0.2.2.md)，关联原文、最强辩护、剩余
  缺陷和修正验收方法。复审使用原任务保存的标准，不随当前模板升级漂移。

## 真实 Office 验收

2026-09-13，在本机 Microsoft Word 16.0（build 16.0.20326）、Excel 16.0、
PowerPoint 16.0 创建专用测试文件，不使用用户稿件。LibreOffice 26.8.0.3
实际转换 DOC、XLS、PPT；三者均通过生产项目导入和原件完整性检查，八语文本均在。
Excel 缓存公式 `12 + 30 = 42` 结果也被提取。

DOCX 测试包含两页、两个分节（纵向／横向）、页眉页脚、编号、粗体／斜体、
多段落表格和八语文本。通过生产保格式导出生成净稿、原生修订稿和分段稿；
Word 以 `OpenAndRepair=false` 打开并输出 PDF。实际接受全部修订与净稿正文完全
相同，拒绝全部修订与源稿正文完全相同。各稿均保持两页、两分节及一个表格。
对源稿、净稿、接受修订稿和分段稿的全部八张页面图逐页查看，未见缺字、遮挡、
表格截断或页眉页脚丢失。该样本不代表所有复杂域、浮动图形、脚注或 WPS 变体。

另外通过完整项目的提取确认、Finding 决定、修改操作确认、Hunk 批准、生成新版
和导出流程，检查实际修改稿及项目完整性。

可复查证据（本机 `dist/native-office-0.2.2/`）：

- `fixtures-native.json`：Office 实际版本和原稿结构。
- `production-checks.json`：三种二进制文件、转换回执、保格式报告和项目导出文件集。
- `verify-native.json`：Word 打开的页数、分节、表格、修订数与接受／拒绝正文。
- `*-word.pdf`、`*-word-1.png`、`*-word-2.png`：原稿和结果的真实 Word 渲染。
- 重现脚本：`scripts/verify_native_office.ps1`、`scripts/verify_office_roundtrip.py`。

LibreOffice 下载自 [Document Foundation 官方镜像清单](https://download.documentfoundation.org/libreoffice/stable/26.8.0/win/x86_64/LibreOffice_26.8.0_Win_x86-64.msi.mirrorlist)，
SHA-256 为 `4aa6c6e1895f4055104effcb556bd3362d20c6ad707c149543304f395ef9db95`，
Authenticode 签名验证通过，签名者 The Document Foundation。
仅在独立工具目录进行行政解包，未改变文件关联；转换使用各自临时 profile。

## PDF 组件与许可证

默认 PDF 依赖为 pypdf 与 pypdfium2；本轮固定 pypdfium2 5.13.0。
Python 包和便携包都做真实 PDF→PNG 自检，便携包缺少渲染器时拒绝发布。
发行目录收录 pypdfium2、PDFium 及其随附第三方许可证。
PDFium 原生调用依 [官方 API 的线程要求](https://pypdfium2.readthedocs.io/en/stable/python_api.html)
串行保护，显式关闭文档、页面和位图。PyMuPDF 仅保留手动兼容后端，不随默认安装引入。
Tesseract 引擎和所选语言包仍是外部组件。

真实八语扫描样本对比后，OCR 改为 300 DPI，并将实际分辨率记入提取记录。
同一日文样本的字符错误率从 144 DPI 的 10.91% 降至 300 DPI 的 0%；
这仅是该样本结果，不是日文普遍准确率承诺。依据和限制符合
[Tesseract 官方图像质量说明](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)。
300 DPI 仍受原像素与页面尺寸上限约束。

## 最终检查与交付状态

2026-09-13 本机结果：

| 验收 | 结果 | 本机证据 |
| --- | --- | --- |
| 完整 Python 回归 | 650 项，647 通过、3 环境性跳过；415.740 秒 | `dist/regression-0.2.2-frozen.log` |
| 浏览器 | 六组全部通过；主流程 13 步、状态 5、主界面 16、研究界面 12、细读 4、HTTP 7 项 | `dist/browser-final-0.2.2.log` |
| 新细读显示复验 | 繁中／英文、上下文跳转、模型 HTML 转义、动态计数与排序说明通过 | `dist/browser-close-reading-final-0.2.2.log` |
| 历史兼容 | 5 项目、2 corpus、71 阶段；1582 文件三份摘要一致、原件不变 | `dist/legacy-replay-0.2.2/frozen-source/report.json` |
| wheel | 全新环境、隔离导入；核心无需 PDF/OCR 可选依赖 | `dist/wheel-release-verified-0.2.2.log` |
| 候选便携程序 | 18 自检通过，包含真实 PDF 页面渲染 | `dist/portable-candidate-0.2.2.log` |
| 实际原生导入 | 3 Office + 9 OCR 样本通过，OCR 文字逐项匹配已核对基线 | `dist/frozen-native-complete-0.2.2/report.json` |
| 实际安装升级 | 实际候选程序安装、重装、备份恢复、卸载保留项目通过 | `dist/built-install-0.2.2.log` |

3 项跳过分别为两项 POSIX 专属检查及需要显式提供发行包的安装检查；后一项已在
实际包验收中单独通过。最终 ZIP、wheel 与真实可执行文件的摘要及最终包装验证
以发行目录外层 `FINAL-VERIFICATION.json` / `.md` 为准；ZIP 自身另附 SHA-256。
原 0.2.1 文件与校验记录没有覆盖。

八语扫描识别和字符错误率见 [真实 OCR 验收](real-ocr-acceptance-0.2.2.md)。
细读基准有 16 个合成案例，其评分器 13 项测试通过，目前仍是 awaiting_responses，
不构成模型准确率结果。
真实非开发者作者对照试用、其他设备、远端 CI 与发布者签名仍没有本轮完成证据。
详见 [商业要求逐项审计](commercial-requirements-audit-0.2.2.md)。
