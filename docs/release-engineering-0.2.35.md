# 0.2.35：表格定位、修订与交付一致性

本版修复三条实际路径：Word 行首省略列未计入位置，合并表头在规范化副本中与数据行
错列，以及 HTML 合并单元格修订时使用物理顺序覆盖逻辑列，导致相邻占位列被改、原文
仍留在目标列。检查覆盖导入、人工批准、修改稿生成与正式导出。

## Word 表格的逻辑列

新解析结果使用 `grid_layout: logical-columns` 标记。行首省略列、横向跨列和行尾空位
进入矩形网格；实际单元格保留独立块与来源坐标，占位字符串不冒充原文块。
例如行首空一列、第一格跨两列、第二格位于第四列时，规范化行是：

`| 空位 | 第一格 | 空位 | 第二格 | 空位 |`

表格范围取声明网格与实际跨列占用的最大值。`gridSpan` 可以扩展较小的声明网格；
`gridAfter` 不用于制造新列，冲突的行尾省略值按 Word 的规则忽略。依据为 Microsoft
对 [gridBefore](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.gridbefore?view=openxml-3.0.1)、
[gridSpan](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.gridspan?view=openxml-3.0.1)
和 [gridAfter](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.gridafter?view=openxml-3.0.1) 的说明。

当前不支持超出声明网格的行首偏移，以及没有声明网格却带非零省略偏移的情况。
这是解析范围限制，不表示所有此类文件都违反 Word 格式规范；此时明确停止识别并保留
原件，请在 Word 中重新保存、拆分或简化后重新导入。

合并或省略单元格会给出繁中、英文警告：规范化 Markdown/Word 副本不保留合并外观，
需对照原件核对表头关系。原位 Word 导出在来源、结构与文本绑定通过时保留原表格结构；
无法安全保格式的情况仍走明确标识的规范化副本路径。本版不宣称完整复现 Word 排版。

## 修改和历史记录

表格修改统一核对容器、子块成员、行列及原单元格正文。HTML 的横向、纵向合并使用
逻辑列；历史 Word 紧凑行继续按物理单元格顺序读取，仅该历史表示使用兼容映射。
共同检查器也在正式导出前核对已批准修改稿，旧版本生成、收据完整但内容错列的稿件
不能作为可信修改稿再次导出。检查失败前不创建导出文件、事件或收据。

历史识别与已生成文件不会自动重写，也不会撤回已经下载的文件。需要恢复时使用
「重新解析為新專案」重新确认和审查；原项目、审查历史与旧产物保留。
本次更新没有改变模型响应协议或替作者批准修改。

## 资源与修订标记

Word 表格展开前执行既有 Office 上限：每表最多10,000行、1,000列，所有表格累计
最多50,000个矩形网格位置。行首空位、合并占位和补齐空位都计数。跨度须为正整数，
省略值须为非负整数；非法值和超限均停止识别，避免负列号或超量分配。
XML 结构预算和正文块上限仍分别执行。

额外识别 `tblGridChange`、`tblPrExChange`、`cellIns`、`cellDel`、`cellMerge`：
表格网格、属性及插入、删除、合并修订会触发现有未定稿警告，原位导出也会重新检查
原始 XML，防止旧元数据漏报。标记定义见 Microsoft 的
[表格单元格属性](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.tablecellproperties?view=openxml-3.0.1)
与 [Wordprocessing 命名空间](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing?view=openxml-3.0.1)。

## 验证范围

自动回归使用合成文件，覆盖跨列、行首/行尾空位、声明网格扩展、HTML 跨行、多个表格
累计容量、非法跨度、未接受的表格修订、旧版紧凑 Word、历史错列稿拦截及重新导入恢复。
原位 Word 的目标物理单元格另用标准 XML 读取器核对，避免只靠同一解析器往返证明。
失败测试核对全部项目文件字节不变。

浏览器执行 Finding 裁决、操作选择、修改提案、人工批准、修改稿生成、正式导出和下载，
核对 Word 原位结构与 Markdown 逻辑列，并检查繁中桌面和英文320宽度警告。
发行自检增加 `table-grid`，源码、独立安装包与便携包执行同一路径。

这批测试验证位置和交付一致性，不代表模型审查准确率或商业试用已经验收。
