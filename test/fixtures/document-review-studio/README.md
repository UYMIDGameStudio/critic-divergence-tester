# 合成验收样本

这些样本不含私人材料，用于 Document Review Studio 的协议和摄取回归：

- `activity-plan.md`：活动方案，故意缺少负责人、预算依据和验收指标，并包含收费、未成年人和个人信息触发词；
- `official-notice.md`：含标题、日期和附件指示的公文样本；
- `governance-policy.md`：含权力边界、回避、申诉和复议词项的治理样本；
- `test_document_review_studio.py` 在测试运行时构造合并单元格/未接受修订 DOCX 和最小文本 PDF；
- 扫描 PDF 和中英混排 OCR 通过可替换 OCR adapter 的依赖状态测试，缺少引擎时必须阻断而不是伪造文本。
