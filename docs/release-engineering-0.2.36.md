# 0.2.36：Word 容器提取与审查范围

## 问题与修复

对照文件显示，原解析器只处理正文的直接段落和表格。整段文章放在 Word 内容控件
`sdt/sdtContent` 中时会被跳过；前后普通段落仍可正常显示，且没有遗漏警告。
尚未展开的 `altChunk` 嵌入正文同样没有提示。存在普通文字时，旧的 `text_coverage`
值为1；这个值仅说明有文本，不能证明完整提取了原文。

本版按原顺序遍历内容控件、自定义 XML 容器以及已有修订容器，提取其中的段落与表格。
表格行、单元格中的同类包装也进入逻辑网格；不会将控件属性当成文章。
标题、空段落计数、行列和原文块定位保留相应关系。
有未接受修订时，既有未定稿警告仍生效，不把提取结果当作最终版本。

内容控件是正文容器，其内容还可能绑定自定义数据，依据见 Microsoft 的
[SdtContentBlock](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.sdtcontentblock?view=openxml-3.0.1)
与 [DataBinding](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.databinding?view=openxml-3.0.1) 说明。

## 原位 Word 修改

新 IR 用 `docx_content_layout: wrapped-blocks-v1` 明确标记正文遍历方式。
原位导出按同一顺序绑定实际 XML 节点；普通、未绑定数据且内容可编辑的控件支持段内
文字替换，净稿和原生修订副本保留容器与未修改部件。

数据绑定、内容锁定、日期或选项控件、正在显示的占位文本及暂不支持的扩展属性会阻止
该控件的原位修改，防止只改缓存文字、被 Word 数据覆盖或绕过内容锁。
锁定控件本身不阻止修改别处的普通段落。
仅禁止删除控件的 `sdtLocked` 仍允许文字替换，具体语义见 Microsoft 的
[Lock](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.lock?view=openxml-3.0.1) 说明。
容器内或紧邻容器的段落增删、拆分目前需要规范化副本与人工版式核对，不会悄悄把新
段落移到容器之外。现有保格式失败提示、规范化文件命名和能力报告继续适用。

历史 IR 不自动重写。带包装但没有新版容器定位标记的旧识别记录不能冒充已完成原位
绑定；需要补齐提取时使用「重新解析為新專案」，保留旧项目和审查记录。

## 未完整提取的内容

`altChunk`、兼容分支、Office 数学公式、子文档、嵌入对象、文本框，以及不能映射的
正文或表格结构会触发 `docx-special-content` 高风险警告。它表示正文或结构尚未完整
识别，不等于这些内容都完全没有文字。确认页提供繁中、英文说明，普通确认不能越过
这项提示；作者可以核对、修正，或明确选择带警告继续。

兼容内容的 Choice 与 Fallback 不再一起拼成重复文字；本版不决定实际应显示哪个分支，
也不执行嵌入 HTML、读取外部链接或渲染公式。Word 的
[AltChunk](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.altchunk?view=openxml-2.8.1)
说明将它定义为导入其他内容的位置；兼容分支选择属于独立的
[标记兼容处理](https://learn.microsoft.com/en-us/office/open-xml/general/introduction-to-markup-compatibility) 过程。
原位导出重新检查原始 XML 中的特殊内容，旧元数据不能绕过这项检查。

新的审查协议携带 `docx_content_visibility`：记录涉及部件、类型、数量，并明确
覆盖率未测量、内容完整性尚未建立。提取警告保留在绑定的协议里，要求模型对依赖这些
内容的批评作条件判断；不能把未提取的公式、嵌入内容或兼容分支当作原文缺失。
这是输入范围约束，不是对模型实际准确率的证明；历史请求与响应继续使用原有协议。

## 验证

合成夹具覆盖八种语言、嵌套控件、自定义 XML、包装后的表格行和单元格、重复文字的
准确定位、段内净稿与修订稿、数据绑定和各类锁、扩展属性、结构修改边界、未展开内容、
旧版识别、资源限额、人工确认、学术协议以及备份恢复。
批准后的真实项目导出另用标准 XML 读取器核对控件数量和目标文字，检查其他 ZIP 部件
不变，避免只用同一解析器往返作证。

浏览器检查控件文字实际出现在页面数据、繁中桌面与英文320宽度的警告、普通确认拦截、
带警告继续，以及保存的学术协议包含可见范围且不冒充读过未展开的嵌入正文。
发行自检增加 `docx-containers`。既有 XML、正文块及展开表格限额在包装内继续执行，
完整上传字节保持不变。本版不宣称全面支持 Word 的所有绘图、公式和控件类型。
