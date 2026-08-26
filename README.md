# Critic Divergence Tester

[![Tests](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml/badge.svg)](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml)

一个 **local-first、模型无关、零第三方依赖的论证工作台**：普通 AI 给你一篇“改好了”的文章；Argument Workbench 让每一处修改都能追溯到审查发现、由你逐项批准、可以撤销，并能在不可变新版本上复查。

它不需要 Claude Code 或 API key。网页聊天、CLI、本地模型都只是可替换的提取／审查执行器；模型负责提出结构和 Finding，人负责校正、接受、拒绝或暂缓，确定性程序负责 provenance、hash binding 和可重建派生物。

这个项目也不是“让多个 agent 投票得出正确答案”。它做的是 philosophical pressure-test：让不同前提的敌对读者分别攻击同一份稿件，然后由作者本人判断哪些攻击真的改变论证。

默认入口只有一个：

```powershell
py -3 critic_runner.py app
```

也可以直接带入稿件；macOS/Linux 将 `py -3` 换成 `python3`：

```powershell
py -3 critic_runner.py app ".\结构的替身.md"
```

需要审查公文、通知、制度文件、活动策划案、项目方案或商业企划案时，
使用新的文档优先工作台：

```powershell
python critic_runner.py studio
```

Document Review Studio 当前标记为 **experimental preview**。本地确定性 Finding 已接通受约束修改与稳定 `check_id` 复审；外部模型复审请求绑定原 critic 的完整 prompt 快照、request、AuditRun、协议摘要和 provider/model。原 Finding 的 Resolution 由人确认；新 Finding 和仍未解决项会成为修订稿版本上的下一轮 open Finding，重新进入裁决和修改。它仍不是正式 V1：除 Gate C 外部用户验证外，原位 DOCX 修订、跨块结构修改和修改闭环的 CLI 对等入口等产品限制仍需解决。它支持 Markdown、TXT、DOCX、文本 PDF 和扫描 PDF；
上传后必须先确认识别质量和文档上下文，再明确选择“本地确定性预检”或导出/导入五个独立 AI critic。原始文件
字节与 SHA-256 永不覆盖，解析警告、页码/表格单元格定位、人工裁决和导出
审计包都保存在本地项目中。详细限制和环境要求见
[`docs/document-review-studio.md`](docs/document-review-studio.md)。
独立 AI 导入默认要求原始响应回显所选请求的 `request_id`、`prompt_sha256`、provider
和 model；如果模型做不到，可在界面或 CLI 明确选择“普通 JSON（人工关联）”，接受不带
回显字段的有效 JSON，但会记录较弱的 `response_binding`，不声称响应确由该 prompt 生成。
两种模式都只记录 `declared_model_metadata`，不冒充直接模型调用。
识别确认由受保护的 extraction decision 产物授权，`state.json` 只是可重建缓存；
本地索引可发现普通修改/删除，但强回滚仍需要项目外可信检查点或签名。

应用会自动打开本机页面。创建项目后，页面用七步状态条和一个“继续”主提示引导流程：确认识别 → 确认上下文 → 本地预检 → 导出/导入五个独立 AI critic → 人工裁决 → 独立 Action/Hunk 批准 → 修改稿复审与导出。首次裁决视图最多显示 30 个按“同一定位＋同一修改动作”形成的工作组，所有原子 Finding 和独立 critic 理由仍完整保留。修改层按工作组/明确 Finding 集生成 Action；系统可以建议 `replace_block`、前后插入、删除、表格单元格替换或追加章节，但用户必须显式选择并说明操作类型，关键词不会直接授权删除整块。每个 Revision 绑定全部当前 accept/correct/reject/defer 决定；任何决定变化都会使旧版本失效。外部新 Finding 不能直接标为 resolved，而会进入修订稿版本的下一轮。导出中心提供 `修改稿.docx`、`修改稿.md`、`修改说明.md`、`未解决风险.md`、复审结果和完整审计包。

独立 AI 响应导入后即可点击“导出当前 AI 审查”，无需先完成 Finding 裁决。系统会生成明确标注“未经人工裁决”的 Markdown 报告、结构化 JSON、模型原始响应和 ZIP 包，并在页面显示项目内保存位置与完整导出目录；它不会冒充正式审查结论或已批准修改。

已有 Reviewed IR 的项目会在同一项目主页显示“专业研究视图”，继续提供 Claim、Rule/Perspective Lens、Citation、版本 lineage 和 Finding Resolution；不同 Lens 的结论保持并列，不做投票合并。快速修订仍是默认入口。

```mermaid
flowchart LR
    A["V1 原稿"] --> B["审查报告"]
    B --> C["原子 Findings"]
    C --> D["人工选择"]
    D --> E["受约束修改提案"]
    E --> F["逐 hunk 审批"]
    F --> G["不可变 V2"]
    G --> H["原标准复查与导出"]
```

所有模型语义都明确标为 model-derived；Raw response 不覆盖，人工 correction 追加保存，Reviewed IR / map / plan 可确定性重建。产品不生成论文总分，也不以 Agent 投票替代作者判断。

快速修订中的人工决定、RevisionAction、application 和无修改 completion 会保存独立摘要凭据；默认入口会重新验证其字段、来源关系、派生结果和摘要，并在不一致时强制只读。这是一种**本地篡改可发现机制**，用于发现单文件误改、静默覆盖和普通篡改；它不是带密钥的密码学防篡改方案。能够同时改写原文件、摘要凭据和策略文件的完整磁盘写入者仍可协同重算这些内容。

## 第一次使用：完整修稿闭环

应用内不绑定模型或 API。需要 AI 时会显示可一键复制的提示词和大文本框；把提示词交给任意 AI，再粘贴返回即可。无效返回仍会保存，并提供可复制的修复提示词。

本地项目库默认位于：

- Windows：`%LOCALAPPDATA%\DocumentReviewStudio\projects`
- macOS/Linux：`$XDG_DATA_HOME/document-review-studio/projects`，未设置时为 `~/.local/share/document-review-studio/projects`

每个项目都可单独复制备份或从项目库页面删除；删除前会二次确认且不可撤销。正式导出位于项目内的 `exports/<application-id>/`，包含可编辑规范化副本、审查报告、结构化结果和完整审计包。环境自检可在首页直接一键修复缺失的 Python PDF 适配器，并在当前项目尚未确认识别时自动重试；CLI 也可运行 `py -3 critic_runner.py doctor --repair`。Tesseract 和语言包仍需按系统提示安装，应用不会静默安装系统软件。

## 专业研究 / 高级 CLI

下面的 IR、Perspective Lens、Citation、lineage 和 Resolution 命令继续保留给研究用户和自动化回归。把 `draft.md` 换成你的 UTF-8 稿件路径：

```powershell
# 1. 创建本地工作区和绑定原稿的 IR extraction prompt
py -3 critic_runner.py ir init .\draft.md

# 2. 将工作区里的 extraction-prompt.md 交给任意模型，保存其纯 JSON 回答
py -3 critic_runner.py ir collect .\draft.argument-workbench --file .\argument-ir-returned.json

# 3. 在行式 Inspector 中浏览并校正 Claims / Evidence / Assumptions / relations
py -3 critic_runner.py ir inspect .\draft.argument-workbench

# 3a. Reviewed IR 生成后，可随时打开本机三栏工作台；Ctrl+C 停止
py -3 critic_runner.py ir ui .\draft.argument-workbench

# 4. 默认只审核 conclusion/intermediate 及其上游承重链；--scope all 才是全面 audit
py -3 critic_runner.py ir review prepare .\draft.argument-workbench --depth core

# 5. 将 review-prompt.md 交给模型，再收集严格结果
py -3 critic_runner.py ir review collect .\draft.argument-workbench --file .\review-results.json

# 6. 先处理模型提出的 routing/applicability 状态，再裁决文章 Findings
py -3 critic_runner.py ir review show .\draft.argument-workbench
py -3 critic_runner.py ir review triage .\draft.argument-workbench
py -3 critic_runner.py ir adjudicate .\draft.argument-workbench
py -3 critic_runner.py ir revision-plan .\draft.argument-workbench --show

# 7. Gate A 通过后可分别运行完整方法论 Perspective Lens；结果不投票或合并
py -3 critic_runner.py ir review prepare-perspective .\draft.argument-workbench `
  --lens methodological-individualism
py -3 critic_runner.py ir review collect-perspective .\draft.argument-workbench `
  --file .\individualist-results.json --producer-label "模型标签"
py -3 critic_runner.py ir review show-perspective .\draft.argument-workbench

# 随时验证完整 hash/provenance 链
py -3 critic_runner.py ir verify-project .\draft.argument-workbench
```

`review prepare` 的 `--scope thesis-chain` 是默认日常范围；可用 `--scope claim --claim C4`、`--scope claims --claim C4 --claim C7` 精确选择，或显式 `--scope all` 做全面审计。`--depth core|full` 只决定每个入选 Claim 使用 core checks 还是加上 extended checks，与 review scope 正交。

新结果 contract 把检查执行状态与实质 verdict 分开：`routing_mismatch`、`not_applicable`、`blocked_missing_context` 不会伪装成文章缺陷，但必须经过独立的人工 triage。PASS 同时记录判断依据 `basis_refs`、真正支持通过的 `support_refs`，以及从该节点到目标 Claim 的 `support_paths`；路径只能使用 `supports`、合规的 `qualifies` 和 `cites`，不能让目标 Claim 自证，也不能让 `contradicts` 或 `assumes` 替它作证。

更完整的文件布局、Inspector 操作和 Phase 1–3 示例见下方 Argument IR / Workbench 章节。

## Legacy critic report workflow（兼容 / Advanced）

最简单的用法**不需要 API key，不需要安装 Claude Code，也不需要配置模型命令**。工具会生成 prompt、回收 AI 报告、提取结构化发现、引导你逐条裁决，并生成修改计划。

### 第 1 步：准备 Python

需要 Python 3.10 或更高版本。在终端里检查：

```powershell
# Windows PowerShell
py -3 --version
```

```bash
# macOS / Linux
python3 --version
```

只要显示 `Python 3.10`、`3.11`、`3.12`、`3.13`、`3.14` 或更高版本即可。Windows 如果提示找不到 `py`，安装 [Python](https://www.python.org/downloads/) 时勾选 **Add Python to PATH**；macOS/Linux 如果提示找不到 `python3`，先用系统的软件管理器安装 Python 3。

### 第 2 步：下载项目

会用 Git：

```bash
git clone https://github.com/UYMIDGameStudio/critic-divergence-tester.git
cd critic-divergence-tester
```

不会用 Git：打开 [项目主页](https://github.com/UYMIDGameStudio/critic-divergence-tester)，点击 **Code → Download ZIP**，解压后在该文件夹里打开终端。

本项目没有第三方依赖，因此不需要运行 `pip install`。

先运行一次自检，确认 Python、协议文件、学术线配置和当前目录都能正常使用：

```powershell
# Windows
py -3 critic_runner.py doctor
```

```bash
# macOS / Linux
python3 critic_runner.py doctor
```

最后显示 `ready` 就可以继续。若出现 `[error]`，按那一行提示处理；这一步不会上传或修改你的文章。

### 第 3 步：运行中文引导

把文章保存为 UTF-8 文本，例如 `draft.md`。普通 `.txt` 文件也可以。然后只运行下面这一条命令：

```powershell
# Windows
py -3 critic_runner.py quickstart
```

```bash
# macOS / Linux
python3 critic_runner.py quickstart
```

程序会用中文询问文章路径和学术线；输入 `1` 选文科·社会科学，`2` 选理科·自然科学，`3` 选工科·工程学。文章路径可以直接复制粘贴，Windows 拖入终端后带双引号也能识别。

如果你不想回答问题，也可以直接写完整命令。文科、历史、哲学、法学、政治学、社会学、经济学等使用：

```powershell
# Windows
py -3 critic_runner.py prepare-track humanities-social-science draft.md
```

```bash
# macOS / Linux
python3 critic_runner.py prepare-track humanities-social-science draft.md
```

自然科学把 `humanities-social-science` 换成 `natural-science`；工程、软件、系统设计把它换成 `engineering`。这是给脚本和熟悉命令行的用户准备的非交互方式。

成功后终端会打印一个类似下面的路径：

```text
.critic-runs/20260807T120000.000000Z--critic-social-science/prompt.md
```

### 第 4 步：交给 AI，再安全回收报告

打开刚生成的 `prompt.md`，复制全部内容，粘贴到你常用的 AI 对话里。AI 返回的六节报告就是审查结果。

不用先创建报告文件。AI 回答完成后，运行“继续并粘贴”命令：

```powershell
# Windows
py -3 critic_runner.py resume --paste
```

```bash
# macOS / Linux
python3 critic_runner.py resume --paste
```

程序会自动找到最新的待办审查并显示文章名和学术协议。把 AI 的完整回答粘贴进去；粘贴完成后另起一行，只输入 `::END::` 并回车。程序只有看到这条结束标记后才开始处理，因此回答里的普通空行不会提前结束输入。

如果同时有多次待办，它会明确告诉你本次选择了最新一次；要继续指定运行，可以写 `resume ".critic-runs/<run-directory>" --paste`。

喜欢先保存文件也可以：把回答保存为 UTF-8 的 `report-returned.md`，然后运行 `resume --report report-returned.md`。直接运行不带选项的 `resume` 时，程序也会询问报告文件路径。文件模式保留文件中的精确原始字节；粘贴模式保存终端实际收到并统一为 UTF-8、换行结尾的内容，manifest 会明确记录 `terminal-paste`，不会伪装成文件导入。

`resume` 会根据真实归档状态自动完成下一步：等待报告时回收报告，裁决未完成时接着裁决，裁决已完成但计划缺失时生成计划。可以随时退出，下次仍运行同一条命令。回收报告时会先验证；无效报告不会写入归档。验证通过后，它会：

- 原样保存 `report.md` 并记录 SHA-256；
- 把运行状态从 `prepared` 更新为 `collected`；
- 将每条批评及其后果检验提取到 `adjudication.json`；
- 把 `STATUS` 与 `UNVERIFIED` 一并带入裁决和修改计划；
- 把报告、manifest 和裁决文件互相绑定，防止张冠李戴。

随后程序用中文逐条显示批评，让你选择“接受、拒绝、暂缓”。接受必须写具体修改动作，拒绝或暂缓必须留下理由；每裁决一条就立即保存。全部完成后自动生成带裁决文件哈希的修改计划；工具会重新推导并逐字核对已有计划，发现裁决变化或计划被手改时拒绝把旧文件当成当前结果：

```text
.critic-runs/<run-directory>/revision-plan.md
```

这份修改计划才是日常工作流的最终产物，而不是 prompt 或 AI 原始回答。
如果要在计划上继续自由增删，请先复制并另存为其他文件；原始 `revision-plan.md` 保留为可重复生成、可核对的机器产物。

以后运行记录多了，不需要自己翻时间戳目录。直接查看所有审查现在走到哪一步：

```powershell
# Windows
py -3 critic_runner.py status
```

```bash
# macOS / Linux
python3 critic_runner.py status
```

它会把最新记录放在最前，显示文章名、协议、当前进度和一条可以直接复制的“下一步”命令。只看某一次运行时，在后面加运行目录即可，例如 `py -3 critic_runner.py status ".critic-runs/<run-directory>"`。熟悉命令行的用户仍可直接使用底层的 `import-report`、`adjudicate` 和 `revision-plan`；日常使用不需要记住它们。

如果计划生成后改变了判断，不要手改 `adjudication.json`。使用显式复议模式：

```powershell
# Windows
py -3 critic_runner.py adjudicate ".critic-runs/<run-directory>" --review-all
```

```bash
# macOS / Linux
python3 critic_runner.py adjudicate ".critic-runs/<run-directory>" --review-all
```

程序会重新展示全部发现；直接回车保留原裁决，输入 `1`、`2` 或 `3` 才会重做当前裁决。只要有一条发生变化，原 `revision-plan.md` 就会先改名为 `revision-plan.previous-<hash>.md` 留档，再根据新裁决生成当前计划。没有任何变化时不会制造重复备份。

runner 本身不会上传文章；当你把 `prompt.md` 粘贴到某个 AI 平台时，文章才会发送给该平台。处理未发表、保密或含个人信息的稿件前，请先确认所用平台的数据政策。`.critic-runs` 中也保存了完整文章，不要把这个目录公开上传。

重点看报告末尾：

- `STATUS: complete`：报告结构完整，且没有声明未核实项目。
- `STATUS: partial`：可以使用，但先查看 `UNVERIFIED` 里有哪些内容没有确认。
- `STATUS: blocked`：缺少关键材料，本次报告不能当作完整审查。

不要看到批评就全部接受。这个工具提供的是敌对压力测试，最后仍由作者判断哪条批评真的改变论证。

### 可选：只检查报告格式

`import-report` 已经自动执行格式校验。如果只想检查一个外部报告、不打算归档，可以运行：

```powershell
# Windows；社会科学示例
py -3 critic_runner.py validate critic-social-science report.md
```

```bash
# macOS / Linux；社会科学示例
python3 critic_runner.py validate critic-social-science report.md
```

显示 `valid` 就表示标题、编号和必填字段合格。它只检查结构，不代表报告中的批评一定正确。

### 常见问题

| 问题 | 怎么处理 |
| --- | --- |
| 提示找不到 `python` / `py` | 安装 Python 3.10+；Windows 优先尝试 `py -3`，macOS/Linux 使用 `python3` |
| 提示找不到 `critic_runner.py` | 先用 `cd` 进入解压后的项目文件夹 |
| 提示找不到 `draft.md` | 确认文章就在当前文件夹，或传入它的完整路径 |
| 输入 `path/to/draft.md` 后提示找不到 | 这是文档占位符，不是真实文件；换成你自己的文章完整路径 |
| 提示“稿件文件是空的” | 用记事本打开该文件，粘贴正文并以 UTF-8 保存；文件不能只有空格 |
| 不知道命令里的学术线英文怎么写 | 运行 `quickstart`，直接输入数字 1、2 或 3 |
| 文件路径里有空格 | 用英文双引号包住路径，例如 `"C:\My Papers\draft.md"` |
| 中文乱码 | 将文章和保存的报告改为 UTF-8 编码 |
| `doctor` 显示 `[error]` | 按错误行修复；最常见原因是 Python 版本太旧、下载不完整或当前文件夹不可写 |
| `import-report` / `validate` 返回很多错误 | 把错误信息交给 AI，让它严格按原提示中的六节格式重新输出；无效报告不会污染归档 |
| `adjudicate` 中途退出 | 已完成的条目已经保存；再次运行同一命令会跳过它们并继续 |
| 忘了下一步该运行什么 | 始终运行 `resume`；它会根据归档状态自动判断 |
| 不会把 AI 回答另存为 UTF-8 文件 | 使用 `resume --paste`，粘贴后单独输入一行 `::END::` |
| 运行太多，不知道该继续哪一个 | 运行 `python3 critic_runner.py status`；Windows 使用 `py -3` |
| 已完成后想改变某条裁决 | 运行 `adjudicate <运行目录> --review-all`；旧修改计划会自动留档 |
| 不知道选哪条线 | 文科与社会研究选 `humanities-social-science`；自然规律与实验选 `natural-science`；产品、系统与实现选 `engineering` |

如果是用 Git 下载的，更新到最新版：

```bash
git pull
```

如果使用 Download ZIP，请重新下载并解压最新版。

## Argument IR v1：把方法论从提示词变成可执行对象

`critic-social-science.md` 仍是稳定工作流使用的兼容基线。新的实验性纵向切片不再要求模型“理解一大篇审查提示词”，而是把任务拆成五种可验证产物：

```mermaid
flowchart LR
    A["原稿"] --> B["Argument IR"]
    B --> C["机器可读规则库"]
    C --> D["确定性 Check Plan"]
    D --> E["模型逐项回答"]
    E --> F["结构与来源校验"]
    F --> G["Findings"]
    G -. "下一阶段接入" .-> H["人工裁决与 benchmark"]
```

IR 明确分开 `Claim`、`Evidence`、`Assumption`、`Citation` 及其关系。每个节点都保留原稿逐字引文；引文必须在整篇原稿中唯一可定位，生成 plan 时程序会把模型写的位置提示改成确定性的 `L行:C列-L行:C列` 区间。程序还会核对原稿文件名、精确字节 SHA-256、ID 是否连续、关系端点是否合法，以及支持／限定关系是否形成循环。它不使用看似精确但无法校准的数字 `confidence`；隐含主张和假设必须写明 `uncertainty`。

社科方法矩阵现在位于 [`ir/social-science-checks.json`](ir/social-science-checks.json)。每条规则都声明适用的主张类型、研究方法、检查问题、失败条件、所需上下文和 PASS `evidence_policy`。因果机制与替代解释适用于所有因果 Claim；时间顺序、混杂、反向因果和选择偏差只适用于标明 `causal-observational` 或 `causal-experimental` 的经验识别 Claim。`core/full` 是 check depth；`thesis-chain/claim/claims/all` 是独立的 Claim scope。

check plan 使用规范化引用结构：完整 Argument IR 只保存一次，选中的检查定义也只保存一次。v3 结果以 `basis_refs` 保存模型判断依据，以 `support_refs` 保存真正支持 PASS 的独立上游依据，并用 `support_paths[].relation_ids` 保存可验证的有向支持路径；`upstream-required` 不接受目标 Claim 自证，`citation-required` 必须从 Citation 经 `cites` 开始。`execution_status` 先区分 `evaluated`、缺上下文、routing mismatch 和实质不适用；后三者必须解释、留痕并由人确认或驳回，但不生成文章缺陷 Finding。旧 v1/v2 plan/results 继续按原 contract 验证，不会被静默升级语义。

### Argument Workbench：不用打开 JSON 的正式流程

Phase 1 把 Argument IR 从一份临时 JSON 变成稿件旁的本地项目。以下命令仍然只需要 Python 标准库，不安装模型 SDK，也不会自动上传文章。

Windows PowerShell：

```powershell
$article = Read-Host "请输入文章的真实完整路径"
$project = Join-Path (Split-Path $article) (([IO.Path]::GetFileNameWithoutExtension($article)) + ".argument-workbench")

# 1. 精确归档 V1 source，并生成绑定 source hash 的抽取提示词
py -3 critic_runner.py ir init $article --project-dir $project

# 2. 把项目中打印出的 extraction-prompt.md 交给任意模型；直接粘贴返回的纯 JSON
py -3 critic_runner.py ir collect $project --paste --producer-label "你使用的模型标签"

# 3. 浏览并校正 Claim / Evidence / Assumption / Citation / relation
py -3 critic_runner.py ir inspect $project

# V2 Reviewed IR 完成后生成精确 source/IR structural diff
py -3 critic_runner.py ir diff-versions $project

# 4. 随时复核全部父哈希和确定性派生产物
py -3 critic_runner.py ir verify-project $project
```

macOS / Linux 把 `py -3` 换成 `python3`。如果模型回答已经保存为文件，第二步改用 `--file path/to/returned.json`。文件模式保留精确字节；终端粘贴模式记录为 `terminal-paste`，并保存终端实际收到的 UTF-8 文本。

新建工作区使用 `argument-ir-extraction-v2`：每条 Claim 的 `types` 与 `methods` 默认各选一个主要值，method 只描述实际支撑该 Claim 的方法，而不是罗列整篇文章出现过的所有方法；结论和中间 Claim 还会被要求连接可追踪的支持关系。这样可避免一次过度多重分类在下游确定性膨胀成大量无关 checks。旧工作区的 v1 prompt 仍按原始字节重建验证，Raw attempt 保存的 `prompt_sha256` 不会因为工具升级而失效或被静默换成 v2。

每次模型返回都写入新的 `raw-ir/attempt-nnnn/`，包括无效返回；旧 attempt 永远不会被覆盖。在尚未产生人工 correction 时，最近一次 `valid` 或 `correctable` attempt 会成为当前 Raw IR；第一条 correction 写入后就把 V1 固定到该 attempt，后续返回只能归档，不会偷换已经人工审查的基础。可定位但存在类型、引文或 relation 问题的结果标记为 `correctable`，可以直接进入 Inspector。无法解析、source hash 不符或没有可用节点身份的返回标记为 `unusable`，仍会归档，但需要重新收集一次。

Inspector 使用普通行式菜单，Windows 和 Linux 行为一致。`[C]lassify` 会按 Claim 依次显示原文、role、types 和 methods；直接回车保留模型值且不冒充人工确认，只有再次确认的修改才会立即写成独立 `ICnnnn.json` 并重建 Reviewed IR。其他编辑同样逐次落盘；Undo 追加一条 revert event，不删除历史。程序随后从 Raw IR 和完整 correction 序列确定性生成：

```text
<project>/documents/D1/versions/V1/reviewed-ir/argument-ir.json
<project>/documents/D1/versions/V1/reviewed-ir/record.json
<project>/documents/D1/versions/V1/reviewed-ir/argument-map.md
```

`argument-map.md` 是日常阅读入口：按 Claim 展示原文位置、上下游支持、Evidence、Assumption、Citation、完整 relation list，以及 deterministic / model-derived / human-confirmed 来源。`argument-ir.json` 保持 v1 兼容，可继续交给已有 `ir validate` 和 `ir plan`。正式 provenance 位于绑定该 payload 的 `record.json`。

仓库内置一个现实结构 demo，不声称它是《结构的替身》原文：

```powershell
py -3 critic_runner.py ir init .\test\fixtures\workbench-demo\manuscript.md --project-dir .\demo.argument-workbench
py -3 critic_runner.py ir collect .\demo.argument-workbench --file .\test\fixtures\workbench-demo\raw-ir.json --producer-label fixture-model
py -3 critic_runner.py ir inspect .\demo.argument-workbench
```

这个 fixture 包含“总会”式过强主张、漏 Claim、错 Evidence 绑定、显式 Assumption 和 Citation，可用于体验校正。Workbench 当前支持单 Project / D1 下的线性多版本历史、many-to-many Claim Lineage、原 Lens Finding Resolution、Citation→Evidence→Claim provenance，以及标准库实现的本地 document-first UI。Product Gate A 与 Gate B 均已由作者作出 human-confirmed `pass`，但早期队列负担和批量裁决等限制仍保留在 Gate 证据中，不能被“通过”抹掉。

完整 artifact lifecycle、parent hash 和 field provenance 约定见 [`docs/artifact-contracts.md`](docs/artifact-contracts.md)。

### Phase 2：从 Claim 进入 Review Findings

完成 IR 校正后，可以直接在同一个本地项目里运行现有 social-science Rule Lens。程序仍不调用或绑定任何模型 SDK：它只确定性选择适用于 Reviewed IR 的 checks，并生成一份 source/hash-bound prompt。

```powershell
# 1. 默认只选择 conclusion/intermediate 及其上游承重 Claim；--scope all 才全面审计
py -3 critic_runner.py ir review prepare $project --depth core

# 2. 把打印出的 review-prompt.md 交给任意模型，并收集纯 JSON 结果
py -3 critic_runner.py ir review collect $project --file .\argument-check-results.json --producer-label "模型标签"

# 也可以像收集 Raw IR 一样直接粘贴，使用 ::END:: 结束
py -3 critic_runner.py ir review collect $project --paste --producer-label "模型标签"

# 3. 从某条 Claim 查看全部适用检查和 open Findings；也可省略 --claim 查看全文
py -3 critic_runner.py ir review show $project --claim C1

# 4. 验证 review snapshot、模型原始返回、Finding envelopes 和 Markdown view
py -3 critic_runner.py ir verify-project $project
```

若要离线演示，可以在未修改 bundled demo IR 的新项目上直接收集 `test/fixtures/workbench-demo/review-results.json`。它包含 C1 的 denominator FAIL 和 C3 的 rival-reading UNCERTAIN，并由测试固定到确定生成的 plan hash；一旦先修改 IR，必须为新 plan 取得新的模型结果，旧 fixture 会被正确拒绝。

每个 `RVn` 会精确保存当时的 Reviewed IR record/payload 和 check library snapshot，因此之后继续校正 IR 不会让旧 review provenance 断链。每次模型结果都进入新的 `results/attempt-nnnn/`；无效结果同样保留，但不会生成 Finding。有效结果确定性生成：

```text
reviews/RV1/
├── review-run.json
├── reviewed-ir-record.json
├── target-argument-ir.json
├── check-library.json
├── check-plan.json
├── review-prompt.md
├── results/attempt-0001/{response.json,record.json}
└── derived/attempt-0001/
    ├── claim-review-index.json
    ├── claim-review.md
    └── findings/F0001.json ...
```

`claim-review.md` 按 Claim 显示 PASS / FAIL / UNCERTAIN，以及 BLOCKED_MISSING_CONTEXT / ROUTING_MISMATCH / NOT_APPLICABLE，不生成论文总分。只有 evaluated 的 FAIL 与实质 UNCERTAIN 产生独立 `argument-finding`；执行或路由状态进入另一条人工队列：

```powershell
# 只读查看当前队列
py -3 critic_runner.py ir review triage $project

# 追加一条 human-confirmed 决定，不覆盖模型结果或旧决定
py -3 critic_runner.py ir review triage $project `
  --task T4 --decision acknowledge --action correct_ir `
  --note "修正该 Claim 的 method 分类后重新运行审查。"
```

`routing_mismatch` 可进入 `correct_ir`，缺上下文可进入 `add_context` / `add_evidence`，`not_applicable` 必须由人明确 acknowledge 或 reject。未完成 triage 会阻止 Gate A corpus capture；决定及复议均为 append-only，并由可重建的 triage index 钉住精确字节。

### Phase 3：人工裁决与 Revision Plan

每条 open Finding 都由作者最终决定；模型不能自动 accept。Workbench 使用与旧 report workflow 相同的 decision 语义，但把每个决定和修改动作保存成独立、不可变、Claim-centered artifact：

```powershell
# 逐条查看 Finding，并选择 Accept / Reject / Defer；每次确认立即落盘
py -3 critic_runner.py ir adjudicate $project

# 只读查看当前状态，不进入交互
py -3 critic_runner.py ir adjudicate $project --view-only

# 大队列先看按 check 与 Claim 聚合的只读 open 摘要
py -3 critic_runner.py ir adjudicate $project --summary-only

# 展开为 Claim-level bundles；不写决定
py -3 critic_runner.py ir adjudicate $project --group-by-claim
py -3 critic_runner.py ir adjudicate $project --group-by-claim --claim C4

# 大队列可先处理 FAIL，再按 Claim 或精确 check 缩小范围；仍然逐条人工确认
py -3 critic_runner.py ir adjudicate $project --verdict fail
py -3 critic_runner.py ir adjudicate $project --claim C4
py -3 critic_runner.py ir adjudicate $project --check causal.alternative-explanation

# 若同一 Claim 下的 3 条 open Findings 确实应作相同决定，可一次明确确认整组
py -3 critic_runner.py ir adjudicate $project --claim C4 `
  --batch-decision reject `
  --reason "这些检查依赖本文没有作出的总体性主张。" `
  --confirm-count 3

# 批量 Accept 仍要求 RevisionAction；每条 Finding 各生成一份独立 action artifact
py -3 critic_runner.py ir adjudicate $project --claim C7 `
  --batch-decision accept `
  --reason "这些问题共同暴露了论断范围过宽。" `
  --confirm-count 2 `
  --action "narrow_claim:把结论限制到本文观察的案例。"

# 随时确定性重建 revision plan；--show 同时输出到终端
py -3 critic_runner.py ir revision-plan $project --show

# 验证 Finding → Adjudication → RevisionAction 和所有精确父哈希
py -3 critic_runner.py ir verify-project $project
```

`--summary-only` 不要求交互终端，只给出范围内的 FAIL/UNCERTAIN、人工决定计数，以及 open queue 的 check/Claim 聚合，不创建 adjudication 或 revision-plan。`--group-by-claim` 展开每个 Claim 的原文、Finding、check、verdict 和 reason，也保持只读。`--verdict fail|uncertain`、`--claim C4|V1:C4` 与 `--check CHECK_ID` 可以组合，只改变本次显示和交互队列，不把被过滤掉的条目视为 resolved。之后不带过滤器再次运行即可继续其余 open Findings。

`--batch-decision` 只是减少重复确认的 application-layer 操作，不产生“综合裁决”：它必须指定唯一 Claim、人工理由和刚刚看到的精确 open 数量。`--confirm-count` 是乐观锁；队列变化时整组拒绝写入。确认成功后，每条 Finding 仍分别生成不可变的 `finding-adjudication`，并保留模型 verdict 的 `model-derived` 来源。Accept 的每个 Finding 还分别生成指定的 RevisionAction；Reject / Defer 禁止带 action。若一组内判断不同，应继续使用逐条模式，或用 `--verdict` / `--check` 缩小到真正同质的子集。

Accept 必须至少指定一个结构化 action type（`narrow_claim`、`add_evidence`、`add_qualification`、`remove_claim`、`restructure_argument`、`clarify_concept`、`verify_citation` 或 `other`）和具体行动文本。Reject / Defer 必须记录理由。改变决定不会覆盖历史：新 `ADnnnn` 通过 `supersedes` 指向旧决定；旧 RevisionAction 也保留。

当前计划写入：

```text
documents/D1/versions/V1/
├── adjudications/AD0001.json ...
├── revision-actions/RA0001.json ...
└── revision-plan/
    ├── record.json
    └── revision-plan.md
```

`revision-plan.md` 分列 accepted / deferred / rejected / open Finding，并保留模型 reason、人工 reason 和行动来源；它只显示状态计数，不产生论文总分。`record.json` 是 `derived-replaceable` cache，逐字绑定 Markdown，并可由 immutable Finding、Adjudication 和 RevisionAction 完整重建。已有顶层 `adjudicate` / `revision-plan` 命令继续服务 legacy report workflow，不被这套 `ir` 子命令替换。

为避免同一 Claim 的同一修改动作因逐 Finding provenance 而重复几十次，Markdown 会确定性生成 `Consolidated Revision Actions`：按 Claim、action type 和完整文本聚合展示，并列出覆盖的 Finding IDs 与全部底层 RevisionAction IDs。聚合只改变可读 cache；每个 adjudication/action artifact、父哈希和 `record.json` item 都保持独立，不能借展示合并抹掉方法论分歧或人工历史。

### Product Gate A：已由作者通过，限制继续保留

Phase 3 完成后不能仅凭工程测试增加 Perspective Lens。`ir gate-a` 把 3–5 篇真实稿件的 Phase 1–3 结果固定为一个私有、本地 evidence corpus。当前作者已作出 human-confirmed `pass`，从而解除 Phase 4 产品门；详细 hash 和未消除的可用性限制记录在 [`docs/product-gate-a.md`](docs/product-gate-a.md)。Gate 工具只保存 workspace locator 与精确哈希，不复制稿件正文；建议输出目录使用 `*.product-gate-a/`，该模式默认不进 Git。

```powershell
# 用同一协议生成全文内嵌 prompt；已有文件不会被覆盖
py -3 critic_runner.py ir gate-a prepare-baseline $project1 .\P1-direct-prompt.md

# 在使用 Workbench Findings 前保存一次完整稿件 direct-chat 对照；时间由两个时间戳确定性计算
py -3 critic_runner.py ir gate-a baseline $project1 `
  --prompt-file .\P1-direct-prompt.md --response-file .\P1-direct-response.md `
  --model-label "模型与版本标签" `
  --model-provider "模型提供方" --model-id "提供方模型 ID" `
  --interaction-mode fresh-session --prior-context none `
  --manuscript-delivery inline --full-manuscript-confirmed `
  --started-at "2026-08-10T10:00:00+08:00" `
  --completed-at "2026-08-10T10:05:00+08:00"

# 实际检查 IR 前后分别调用；时间来自系统时钟，不再靠事后回忆
py -3 critic_runner.py ir gate-a session start $project1 `
  --activity ir-inspection --note "逐条对照原文检查 IR"
py -3 critic_runner.py ir inspect $project1
py -3 critic_runner.py ir gate-a session finish $project1 GS1

# If no valid inspection occurred, close the interval without counting it:
py -3 critic_runner.py ir gate-a session abandon $project1 GS1 `
  --reason "The author left before making an IR judgment."
py -3 critic_runner.py ir gate-a session list $project1

# 在捕获 Gate corpus 前只读汇总 3–5 个项目；未完成时逐篇给出下一条命令
py -3 critic_runner.py ir gate-a readiness $project1 $project2 $project3

# 三个变量分别指向已经完成 IR correction、Rule Review、人工裁决和 revision plan 的真实项目
$gate = "D:\private-evaluation\workbench.product-gate-a"
py -3 critic_runner.py ir gate-a init $gate $project1 $project2 $project3

# 每篇稿件追加一次人工 assessment；--anchor 可重复，用于保存已知重要 Claim、抽取陷阱、Finding 或框架反转
py -3 critic_runner.py ir gate-a assess $gate P1 `
  --comparison clearer --burden acceptable `
  --missed-claims 1 --wrong-claim-types 2 `
  --wrong-relations 1 --rhetoric-as-claims 0 --reversed-attributions 0 `
  --anchor "‘总会’的 denominator problem" `
  --anchor "吉拉尔 attribution/citation issue" `
  --actual-revision-notes "作者收窄了该 Claim，并补充比较范围" `
  --notes "校正成本仍可接受"

py -3 critic_runner.py ir gate-a report $gate --show
py -3 critic_runner.py ir gate-a verify $gate
```

`ir gate-a prepare-baseline` 使用版本化的 `direct-full-manuscript-review-v1` 协议，并把 DocumentVersion 的 source bytes 原样嵌入 prompt。`ir gate-a baseline` 原样保存 direct-chat prompt/response、provider/model ID、开始/完成时间、稿件交付方式和会话条件，并绑定稿件精确字节；inline 模式会再次验证 prompt 确实包含完整原稿。它不填写比较结论。新 Gate corpus 只接受 controlled v2 baseline：fresh session、没有既有对话上下文，而且模型确实收到完整稿件。旧 v1 baseline 继续可验证，但不能进入新的 Gate。`ir gate-a readiness` 可以在人工裁决尚未完成时运行，只读汇总 Claim、correction、模型 Findings、Finding 决定、status triage、revision plan、baseline 和 IR inspection timing 状态。只有全部项目没有 open Finding 或 open triage、revision plan 与 controlled baseline 已生成、至少一段 IR inspection 在首个 Rule Review 结果前完成、没有仍开放的 work session，且 source bytes 互不重复时，才允许捕获不可变 corpus。

`ir gate-a session start/finish/abandon/list` 记录实际人工作业时间。start、完成 record 和 abandonment record 都不可变，结束时间与 elapsed milliseconds 由系统时钟确定；同一 workspace 同时只允许一个 open session。若命名活动未实际发生或区间被中断，必须使用 `abandon` 并记录理由；该区间仍可审计，但不会计入 Gate 时间，也不能满足 IR inspection 要求。活动明确区分 IR inspection、Finding adjudication、status triage、revision planning、manuscript revision 和 other。新建的 v5 Gate corpus 和 assessment 仅绑定首个 Rule Review 结果之前真正完成的 IR inspection record，并确定性汇总精确毫秒；`--correction-minutes` 仅为读取/追加旧 v1–v4 Gate 留作兼容参数，v5 会拒绝自报时间。旧 Gate 工件保持按原 schema 验证，不会被迁移或改写。

对 P2/P3（以及可选的 P4/P5）完成 assessment 后，报告才会显示 `Ready for human gate decision: yes`。程序永远不会自动通过 Gate；只有人类 evaluator 可以追加决定：

```powershell
py -3 critic_runner.py ir gate-a decide $gate pass --reason "真实语料上的控制性与校正成本达到进入 Phase 4 的要求"
```

`pass` 前强制要求 3–5 个互不相同的 source、每个 workspace 的全部 hash 仍与捕获时一致、所有 Finding 已 adjudicate、所有非实质执行状态已完成人工 triage、每篇都有人工 assessment 和至少一个 regression anchor。报告只给出 Claims、corrections、accepted/rejected/deferred/open Findings、抽取错误和人工成本等计数，不生成质量分数。仓库中的 synthetic/现实结构 fixture 只测试工具机制；当前 Gate 通过来自仓库外的私有真实稿件 corpus，而不是 fixture。

### Phase 4：Perspective Lenses

Phase 4 首先接入两种保持完整框架承诺的 Review Lens：`methodological-individualism`（兼容旧名 `critic-individualist`）与 `contrastive-explanation`（兼容旧名 `critic-contrastivist`）。它们不是 Rule Lens，不会被拆成 check × Claim 的任务矩阵。每个 Lens 对每条入选 Claim 最多形成一个整体判断，不产生分数、投票或自动综合。

```powershell
# 方法论个人主义：默认 thesis-chain，也可用 claim/claims/all
py -3 critic_runner.py ir review prepare-perspective $project `
  --lens methodological-individualism

# 把生成的 review-prompt.md 交给任意模型，原样回收纯 JSON
py -3 critic_runner.py ir review collect-perspective $project `
  --file .\individualist-results.json --producer-label "模型标签"

# 查看该 Lens；可加 --claim C4
py -3 critic_runner.py ir review show-perspective $project

# 另一 Perspective Lens 单独准备、单独归档、单独显示
py -3 critic_runner.py ir review prepare-perspective $project `
  --lens contrastive-explanation

# 从同一 Claim 并列查看所有当前 Rule/Perspective Lens，不做综合
py -3 critic_runner.py ir review show-claim-lenses $project --claim C7
```

每个 `PVn` 冻结完整 critic Markdown、Reviewed IR、非循环的 `perspective-review-plan.json`、prompt 和每次模型原始返回。`complete` 结果必须按 scope 对每条 Claim 恰好判断一次；FAIL / 实质 UNCERTAIN 确定性转成统一 `argument-finding`，PASS 留在可读 index 但不产生待办。Perspective Finding 直接进入现有 `ir adjudicate` 和 RevisionAction 流程，人工可以 accept / reject / defer；模型不能自动接受自己的批评。

```text
perspective-reviews/PV1/
├── perspective-lens-protocol.json
├── perspective-lens.md
├── perspective-review-plan.json
├── review-run.json
├── reviewed-ir-record.json
├── target-argument-ir.json
├── review-prompt.md
├── results/attempt-0001/{response.json,record.json}
└── derived/attempt-0001/
    ├── perspective-review-index.json
    ├── perspective-review.md
    └── findings/F0001.json ...
```

同一 Claim 可以同时显示 Social Science FAIL、Individualism FAIL 和 Contrastivism PASS；系统不会把它们压成 `66% confidence`。《结构的替身》C7 已完成一次真实 vertical-slice 运行，artifact hashes、框架分歧和暴露出的 cache lifecycle 修复记录在 [`docs/phase4-perspective-demo.md`](docs/phase4-perspective-demo.md)。Perspective Lens 自身仍只负责产生独立 Finding；跨版本 lineage、Finding resolution、Citation verification 和 UI 分别由后续 application layer 接入，不改变 Lens contract。

### Phase 5：导入新的 DocumentVersion

修改稿不覆盖 V1。把新稿导入同一个项目后，程序创建连续的 `V2`、`V3`……，每个版本保存自己的 source、extraction prompt、Raw IR、corrections、Reviewed IR、reviews 和人工历史：

```powershell
py -3 critic_runner.py ir import-version $project .\draft-v2.md

# 普通 IR 命令默认作用于最新版本，此时是 V2
py -3 critic_runner.py ir collect $project --file .\argument-ir-v2.json `
  --producer-label "模型标签"
py -3 critic_runner.py ir inspect $project

# verify-project 会验证 V1..当前版本，而不只检查最新目录
py -3 critic_runner.py ir verify-project $project
```

新版本必须与当前 parent 的原稿精确字节不同，并通过 `parent-version` SHA-256 绑定前一份 `document-version.json`。当前产品只允许线性 `V1 → V2 → V3`，尚不开放分支版本；这避免在 lineage 与 Finding resolution contract 稳定前暗中引入未定义的合并语义。导入只创建版本和 source-bound extraction prompt，不会复制 V1 的 Claim ID，也不会声称新旧 Claim 相同。

`diff-versions` 逐行比较 source bytes，并以排除版本局部 ID/位置的精确内容 fingerprint 比较 Claims、Evidence、Assumptions、Citations 和 relations。它只报告 exact unchanged、相同文字锚点下的字段变化、removed 和 added；文本改变的 `V1:C4` 与 `V2:C7` 在这里仍显示为 removed + added。这个结果明确标为 deterministic structural comparison，不会越权宣布 semantic identity。可读结果位于 `documents/D1/version-diffs/V1--V2/structural-diff.md`。

结构 diff 完成后，可以让任意模型提出 semantic Claim lineage。模型只负责 proposal，不能确认跨版本身份：

```powershell
# 冻结两版 Reviewed IR、structural diff 与 lineage prompt；相同输入不会重复建 run
py -3 critic_runner.py ir lineage prepare $project

# 保存每一次模型原始返回；无效结果同样保留且不产生派生 lineage
py -3 critic_runner.py ir lineage collect $project --file .\lineage-proposals.json `
  --producer-label "模型标签"

# 不打开 JSON，查看 unchanged / modified / split / merged / removed / new / uncertain
py -3 critic_runner.py ir lineage show $project

# 逐条确认或拒绝；每次判断都是新的 human-confirmed artifact
py -3 critic_runner.py ir lineage adjudicate $project --proposal LP1 `
  --decision confirm --reason "两版确实是同一主张的收窄"

# 确实逐条看完后也可批量确认；expected-count 防止误操作到数量已变化的队列
py -3 critic_runner.py ir lineage adjudicate $project --all --expected-count 3 `
  --decision confirm --reason "已逐条核对全部三项"

# 同屏查看模型原提议和当前人工判断
py -3 critic_runner.py ir lineage history $project
```

每次 analysis 都保存两版 Reviewed IR、确定性 structural diff、完整 prompt 和 exact response。派生的 `claim-lineage` 使用 `V1:C4 → V2:C7` 这类版本限定引用，原生支持一对多 split 与多对一 merged；semantic changes、reason 和 uncertainty 均明确标记为 `model-derived`。`complete` proposal 必须覆盖两侧全部 Claims，但允许复杂关系重叠。

人工 `confirm`、`reject`、`correct` 都追加新的 schema-v3 `claim-lineage`，不会修改模型 proposal。`correct` 可用 `--from-claim`、`--to-claim`、`--relation`、`--semantic-change`、`--lineage-reason` 和 `--basis-ref` 从命令行正式改正关系，无需打开 JSON。再次判断同一 proposal 时，新事件通过 `supersedes` 绑定上一判断，完整历史仍然保留。

### Phase 6：重新运行原始 Lens 并确认 Finding Resolution

Finding 被接受、生成 RevisionAction、导入 V2 并确认 Claim Lineage 后，使用原始 Finding ID 准备重测：

```powershell
# 冻结旧 Finding、accept、RevisionAction、人工确认 lineage、V2 IR 和原 Lens
py -3 critic_runner.py ir resolve prepare $project V1-RV1-attempt-0001-F0001 `
  --from-version V1 --to-version V2

# 把 resolution-retest-prompt.md 交给任意模型；模型只重跑原 Lens
py -3 critic_runner.py ir resolve collect $project --file .\retest-results.json `
  --producer-label "模型标签"

# 查看程序根据各后代 PASS/FAIL/UNCERTAIN 得出的确定性 proposal
py -3 critic_runner.py ir resolve show $project

# 最终状态仍由人确认；也可 reject 或用 --decision correct --final-status ... 改正
py -3 critic_runner.py ir resolve decide $project --decision confirm `
  --reason "原 denominator 检查现在确实通过"
```

系统不会调用 generic model 问“解决了吗”。它要求原 Rule Lens 的同一 `check_id` 或原 Perspective Lens 的完整 protocol 对所有人工确认的后代 Claim 重测。Rule Lens 的 PASS 继续受原 `evidence_policy` 和 relation-aware `support_paths` 约束。全部 PASS → `resolved`，全部 FAIL → `unresolved`，split 后混合结果 → `partially_resolved`，任一实质不确定 → `uncertain`；人工确认删除且没有后代时，不伪造模型运行，确定性提出 `obsolete`。这些都只是 proposal，只有 `ir resolve decide` 会产生 human-confirmed 最终状态。

### Product Gate B：真实多版本写作验证

Phase 7 前必须用 2–3 个作者真实继续修改的多版本项目建立 Gate B。工具只保存本地 locator 与 exact hashes，不复制稿件，也不会根据计数自动宣布通过：

真实语料的纳入标准、已审计的作者仓库历史和当前排除理由见
[`docs/product-gate-b.md`](docs/product-gate-b.md)。元数据、SEO 或纯格式变化不能冒充论证版本；接受 RevisionAction 也不等于授权系统代写 V2。

```powershell
py -3 critic_runner.py ir gate-b init .\private-gate-b $project1 $project2

py -3 critic_runner.py ir gate-b assess .\private-gate-b P1 `
  --lineage-correction-minutes 12 `
  --lineage-reasonable yes --split-merge-worked yes `
  --finding-inheritance-correct yes `
  --resolved-stopped-reappearing yes `
  --unresolved-persisted not_observed `
  --revision-rationale-clarity clear

py -3 critic_runner.py ir gate-b report .\private-gate-b --show
py -3 critic_runner.py ir gate-b decide .\private-gate-b pass `
  --reason "作者完成真实 V1→V2 工作流并确认 lineage/resolution 可理解"
py -3 critic_runner.py ir gate-b verify .\private-gate-b
```

Gate B 要求每个项目至少两版、每个相邻版本都有人工 lineage 决定、语料整体实际出现过 human-confirmed split/merge，并至少保存一条人工 Finding Resolution。人工 assessment 还必须分别说明 lineage 是否合理、Finding 是否正确继承、resolved 是否停止骚扰、unresolved 是否持续追踪、修改理由是否仍可理解。若证据不齐，`pass` 会被拒绝；`fail` 或 `defer` 始终可以诚实记录。

### Phase 7：Evidence 与 Citation provenance

Gate B 通过后，可以对当前版本的全部 Citation 或指定 Citation 做实质核验。程序只生成 provider-neutral prompt、保存原始结果、验证证据联锁并传播依赖状态；它不会联网替你偷偷补结果，也不会把模型记忆当来源：

```powershell
# 1. 默认审核当前版本全部 Citation；也可重复 --citation Z1 --citation Z3
py -3 critic_runner.py ir citations prepare $project

# 2. 将 citation-audit-prompt.md 交给能够访问可核查来源的模型
py -3 critic_runner.py ir citations collect $project `
  --file .\citation-audit-results.json --producer-label "模型标签"

# 3. 模型全绿也仍是 unverified；人逐项 confirm / reject / correct
py -3 critic_runner.py ir citations show $project
py -3 critic_runner.py ir citations decide $project --citation Z1 `
  --decision confirm --reason "已核对书目、原文、措辞支持与上下文"

# 人工纠正必须明确给出全部四个最终维度
py -3 critic_runner.py ir citations decide $project --citation Z1 `
  --decision correct --reason "原文存在，但不支持稿件当前措辞" `
  --bibliographic-existence verified --exact-source-located verified `
  --content-support does_not_support --context-preserved yes `
  --uncertainty ""

py -3 critic_runner.py ir citations rebuild $project
py -3 critic_runner.py ir verify-project $project
```

四个维度分别是 `bibliographic_existence`、`exact_source_located`、`content_support` 和 `context_preserved`。确定性判断必须引用可检查的来源；exact source 与 content support 必须有 primary/repository source。没有找到原文时，后两项只能是 `uncertain`。

派生的 `evidence-provenance.md` 按 Citation 展示四维结果、来源、人工决定，并沿 Reviewed IR 的 `cites`、`supports`、`qualifies` 路径列出下游 Evidence 和 Claims。只要 Citation 尚未得到四维人工确认，下游显示 `depends_on_unverified_evidence`；这不等于 `claim_false`，也不会产生论文总分。无效模型返回仍保存在新的 attempt 中，不会生成派生事实或覆盖旧结果。

一条作者 V2 Citation 已完成从外部原文、模型四维提案、人工确认到下游依赖更新的真实 vertical slice；公开哈希、来源、结果和限制见 [`docs/phase7-citation-demo.md`](docs/phase7-citation-demo.md)。该演示证明工作流可运行，不代表两篇稿件的全部 Citation 已核完。

### Phase 8：本地三栏 Workbench

Reviewed IR 建立后，可打开 document-first 的本机工作台：

```powershell
py -3 critic_runner.py ir ui $project
```

默认页面把原文、当前 Claim 的上下游论证和 Review 放在三个联动栏中。版本选择器可查看历史稿；Review 栏按 Lens 分别保留 PASS/FAIL/UNCERTAIN，并显示 Finding 的人工决定、RevisionAction、Citation 状态与 Claim Lineage。当前版本可以直接 `Accept / Reject / Defer`；Accept 必须填写正式 revision action，保存后写入与 CLI 完全相同的 append-only artifact。

服务只监听 `127.0.0.1`，每次启动使用新的本地 token，Ctrl+C 即停止。它没有账号、同步、遥测或云端数据库，也不允许改成公网监听。界面不显示总分、不投票、不自动综合方法论冲突。架构、安全边界、真实浏览器演示和仍保留为行式交互的功能见 [`docs/phase8-local-ui.md`](docs/phase8-local-ui.md)。

从导入、人工校正到 Argument History 的最终逐项验收矩阵见 [`docs/product-definition-of-done.md`](docs/product-definition-of-done.md)。它同时记录仍保留的 UX 限制，不把工程闭环夸大为模型正确性。

### 兼容的低层 Argument IR 流程

先让工具为原稿生成一份**抽取提示词**：

以下命令只需要 Python 3.10 或更高版本。**不要原样输入 `path/to/draft.md`**：它只是文档里的占位写法，仓库中没有这个文件。Windows PowerShell 最不容易输错的方法是先让终端询问真实路径：

```powershell
$article = Read-Host "请输入文章的真实完整路径（也可以把文件拖进窗口）"
py -3 critic_runner.py ir prepare $article
```

把生成的 `draft.argument-ir-prompt.md` 全部交给任意 AI。把 AI 返回的**纯 JSON**保存为 `argument-ir.json`，然后逐步运行：

```powershell
# 1. 确认 IR 没有伪造引文、错绑原稿或破坏图结构
py -3 critic_runner.py ir validate $article .\argument-ir.json

# 2. 由程序选择适用检查，并生成给 AI 的短执行提示词
py -3 critic_runner.py ir plan $article .\argument-ir.json --depth core

# 3. 把 argument-check-prompt.md 交给 AI；将纯 JSON 回答保存为 argument-check-results.json
py -3 critic_runner.py ir validate-results .\argument-check-plan.json .\argument-check-results.json

# 4. 只把 fail / uncertain 确定性转换为发现
py -3 critic_runner.py ir findings .\argument-check-plan.json .\argument-check-results.json
```

macOS / Linux 可把 `py -3` 换成 `python3`，并直接把 `$article` 换成带引号的真实文件路径。

最后得到 `argument-findings.json`。check plan 和结果互相用精确文件哈希绑定；模型不能悄悄增删、合并或重排任务，也不能引用当前 Claim 论证链之外的节点充当证据。命令重复运行时，只会复用完全相同的输出；若目标文件内容不同，工具会拒绝覆盖。

这条低层 IR 流程继续作为兼容接口；Argument Workbench 已把相同的 IR-native checks 接成 Claim-centered Findings，并接入人类最终决定的 adjudication / RevisionAction。它仍不能证明某条规则本身正确。必须先用 3–5 篇真实文章执行 Product Gate A，测量问题召回、误报、人工接受、实际修改、重跑稳定性和校正成本；Gate A 通过前不继续横向增加学科 critic。

## 三条学术线

| 学术线 | 主协议 | 它优先审查什么 |
| --- | --- | --- |
| 文科·社会科学 `humanities-social-science` | `critic-social-science` | 主张类型、研究设计、测量、识别、案例与材料、解释边界、规范前提 |
| 理科·自然科学 `natural-science` | `critic-natural-science` | 假说、实验／观察设计、测量不确定性、统计推断、复现与边界条件 |
| 工科·工程学 `engineering` | `critic-engineering` | 需求、约束、权衡、接口、失效模式、安全、验证与确认 |

文科·社会科学是当前重点线。它先把承担论证功能的主张分成规范、概念、诠释、描述、因果、预测和评价，再选择相称的检查。经验和因果研究会被追问构念、操作化、样本形成、测量效度、混杂、选择、反向因果、机制与外部效度；定性研究会被追问案例选择、三角互证、过程追踪、负例和反身性；历史、诠释和规范研究则使用来源语境、时代错置、竞争读法、价值前提与分配后果等标准。它明确禁止拿 p 值、随机对照实验或代表性抽样机械审判所有文科研究。

`critic-individualist` 和 `critic-contrastivist` 没有消失；它们现在是文科·社会科学线下的专门敌对视角。`citation-auditor` 是三条线共享的跨学科协议。

先查看分轨和完整协议表：

```bash
python critic_runner.py tracks
python critic_runner.py list
```

## Legacy critic report 什么时候用

正常写作优先使用页首的 Argument Workbench。下面这条整篇报告流程为旧用户和高级实验保持兼容；不要跑 I₁/I₂/C₁/C₂，那是验证 critic 是否真的不同的测试，不是日常工作流。

写稿时只按需要叫一个：

| 什么时候用 | 协议 | 它只追问什么 |
| --- | --- | --- |
| 文科或社会科学稿件需要整体方法审查 | `critic-social-science` | 这类主张应接受哪种证据标准，现有推断跨了多远？ |
| 自然科学论文、实验或模拟 | `critic-natural-science` | 设计、测量、不确定性和可复现性是否支持结论？ |
| 工程方案、原型或系统报告 | `critic-engineering` | 它能否在声明的需求、约束和失效场景下工作？ |
| 文章大量使用“结构 / 系统 / 话语 / 资本”等解释 | `critic-individualist` | 能不能还原到具体个体的信念、激励和行动？ |
| 文章提出自己的解释、概念区分或文本读法 | `critic-contrastivist` | 你解释的是 X 而不是哪个 Y，凭什么排除 Y？ |
| 投稿 / 发布前核引证 | `citation-auditor` | 每一个“通过”有没有真正的书目和内容证据链？ |

报告互相冲突时不要投票，也不要再加一个 lead agent 把冲突抹平。先看每份报告末尾的 `STATUS` 和 `UNVERIFIED`，再由人决定哪些发现成立。

一个很实用的最小流程是：

```text
写完一稿
  ↓
按文章风险选 1 个 critic
  ↓
回收并验证 AI 报告
  ↓
人工逐条接受 / 拒绝 / 暂缓
  ↓
按照 revision-plan.md 修改
  ↓
准备发布时跑 citation-auditor
```

多个 critic 都值得审同一篇文章时可以都跑，但必须独立；后一个不能看到前一个的报告。

## 进阶使用：自动运行与精细协议

下面内容适合已经完成上面“5 分钟上手”，并希望自动调用模型 CLI、选择更窄审查视角或做正式校准的人。

下文统一写 `python`；Windows 如果该命令不可用，直接替换成 `py -3`，macOS/Linux 可替换成 `python3`。

不想记协议名时，直接按学术线准备提示词或运行：

```bash
python critic_runner.py prepare-track humanities-social-science path/to/draft.md
python critic_runner.py run-track natural-science path/to/draft.md -- your-model-command
```

### 方法 A：生成提示词包

这是最通用、也最不容易出问题的方式：

```bash
python critic_runner.py prepare critic-individualist path/to/draft.md
```

命令会打印一个 `prompt.md` 路径。把这个文件交给任意你愿意使用的模型即可。协议和稿件已经装在同一个自包含提示词里，不依赖 Claude Code 的 `~/.claude/agents`。

### 方法 B：交给任意 CLI 执行器

如果某个模型 CLI 遵守“UTF-8 stdin 读提示词、UTF-8 stdout 写回答”的约定：

```bash
python critic_runner.py run critic-contrastivist path/to/draft.md --timeout 900 --max-output-bytes 16777216 -- your-model-command arg1 arg2
```

runner 不认识也不保存任何 API key，不用 shell 拼接命令，也不绑定某家模型。执行器参数可能包含密钥，因此归档只记录可执行文件名和参数数量，不保存参数值。

为了让实验说明“到底是哪一个模型／配置”，可添加不含密钥的公开标签：

```bash
python critic_runner.py run-track humanities-social-science draft.md \
  --executor-label "model-x; temperature=0.2; prompt-profile=v3" \
  -- your-model-command
```

标签会进入可验证 manifest，并在 campaign 的所有子运行之间强制一致。它是公开元数据，禁止放 API key、访问令牌或本机敏感路径。

`run` 一次只启动**一个**执行器进程。项目故意没有并发 fan-out；要跑第二个 critic，就在第一个结束后再运行一条命令。

runner 默认给每次执行 900 秒和 16 MiB 的 stdout/stderr 合计额度；可用 `--timeout` 与 `--max-output-bytes` 调整。超时返回 124，超过输出额度返回 125。输出先流入私有临时文件，不会无界堆在内存里；最终归档只保留额度内的原始字节。POSIX 使用独立进程组，Windows 使用启动前绑定的 kill-on-close Job Object，超时、超量或退出后会清理执行器后代。非法 UTF-8 会原样留档并判为无效报告，不会被静默替换。

### 一键校准：四次隔离运行 + 可复算计分

需要正式验证两个 critic 是否真的不同，不必再手工管理 I₁/I₂/C₁/C₂：

```bash
python critic_runner.py campaign path/to/old-draft.md --repeat 2 -- your-model-command arg1 arg2
```

`campaign` 仍然严格串行运行，各次执行看不到其他报告。只要至少有两种非引证 critic、每种至少重复两次、全部报告成功且结构有效，它就会生成独立运行归档、`campaign.json`、可点击的 `SUMMARY.md` 和待填写的动态 schema v3 `scorecard.json`。scorecard 已从每份报告第一节提取全部 A 指控及其位置、指控和理由。

campaign schema v3 使用带种子的反向轮次平衡：第一轮按种子确定协议顺序，第二轮反向，第三轮再恢复，避免固定的 `I1, I2, C1, C2` 把时间漂移、缓存或执行器热身误当成协议差异。随机种子、策略和实际执行次序全部写入 `campaign.json`；用 `--order-seed published-seed` 可以精确复现，验证器会独立重算顺序并拒绝被调换的记录。

三条学术线也能直接组成方法对照 campaign：

```bash
python critic_runner.py campaign draft.md \
  --track humanities-social-science \
  --track natural-science \
  --track engineering \
  --repeat 2 --order-seed published-seed -- your-model-command
```

这种跨线 campaign 会生成真正可计分的 N 协议 × R 重复矩阵。组内 W 自动包含同一协议各重复之间的全部组合，组间 B 自动包含不同协议之间的全部组合；每种协议必须使用相同重复次数，避免某一条线因样本更多而获得额外权重。三条线各重复两次会得到 3 组组内比较和 12 组组间比较。比较数随总运行数平方增长，因此日常校准建议先使用两次重复。

#### 真正的盲分，而不是手动遮名字

原始 `scorecard.json` 必须保存协议身份和证据出处，不能直接交给配对评审者。先生成两个分离的 artifact：

```bash
python critic_runner.py blind-scorecard .critic-campaigns/<campaign>/scorecard.json
```

命令会在 `scorecard.json` 旁边生成 `blind-review.json` 和 `blind-key.json`；这些文件都留在默认不进 Git 的 `.critic-campaigns` 目录中。只把 `blind-review.json` 交给评审者。它以随机化的 `R01`、`R02`……替代运行身份，随机排列比较，不含协议名、重复编号、归档路径或报告 hash。`blind-key.json` 保存身份映射，不得在分类结束前交给评审者，也不要提交到 GitHub。

评审者只填写每组 `pairs`，分类使用 `overlap`、`different_reason` 或 `ambiguous`，完成后把 `complete` 改为 `true`。回收时不要覆盖原件：

```bash
python critic_runner.py apply-blind-scorecard .critic-campaigns/<campaign>/scorecard.json
```

runner 默认读取同目录的 `blind-review.json` 和 `blind-key.json`，校验后生成 `completed-scorecard.json`。它会确认盲评文件与 key 属于同一个原始 scorecard，claims 没被编辑，别名映射、比较集合、A 编号与一对一约束都成立，再恢复真实比较。已有输出不会被覆盖；如需保留多个版本，可显式传入 `--output`、`--key-output` 或 `--key`。`--seed` 可让别名分配可复现；默认使用随机种子。盲法消除了工具主动泄露的身份元数据，但不能保证稿件引文或 critic 特有措辞本身完全不可识别。

```json
"I1:I2": {
  "complete": true,
  "pairs": [
    {"left": "A1", "right": "A3", "classification": "different_reason"}
  ]
}
```

```bash
python critic_runner.py score .critic-campaigns/<campaign>/completed-scorecard.json --format markdown --output divergence-score.md
```

记分器会重新读取全部归档报告，核对原始字节 hash，并再次提取 claims；它还把 scorecard 的运行顺序、协议归属、重复编号和归档路径反向绑定到 campaign 记录。证据清单被修改、报告被替换、运行身份被重写、路径逃出 campaign、同一条 claim 被重复配对或比较尚未明确完成时都会拒绝计分。随后它自动计算每次 d 的上下界、W/B 区间及 `reject` / `advance` / `inconclusive` 判决。它只接管可确定的算术，不替人判断两条指控是否语义重合。

`python critic_runner.py init-scorecard scorecard.json` 仍可创建兼容的 schema v1 汇总计数表，用于没有 campaign 归档的旧实验；固定 I₁/I₂/C₁/C₂ 的 schema v2 仍可读取，新 campaign 默认使用可追溯且动态分组的逐条配对 schema v3。整个 campaign 可单独复核：

```bash
python critic_runner.py verify-campaign .critic-campaigns/<campaign> --source path/to/old-draft.md
```

### 报告结构校验

所有非引证 critic 使用同一套六节输出骨架。`run` 会自动检查标题顺序、A 编号连续性、第一／二节的一一对应、必填字段、唯一最弱／最强项标记，以及末尾唯一的 `STATUS` / `UNVERIFIED`。`complete` 必须配 `UNVERIFIED: none`；`partial` / `blocked` 必须给出具体未核实原因。

`citation-auditor` 使用专用校验器：检查 C 编号、全部审计字段、证据分布计数，以及“内容证据 B/C/D 不得判明确支持或通过语境”“书目证据 D 不得判存在性通过”等硬联锁。执行器即使返回 0，只要结构或联锁不合格，runner 仍返回 3，并把具体错误写到 stderr 和 manifest。

已有报告可以单独检查：

```bash
python critic_runner.py validate critic-individualist path/to/report.md
```

这是结构校验，不判断指控是否正确、两条指控是否语义重合，也不代替 W/B 的人工盲分。

归档完成后可以重新计算文件哈希、复核生命周期与退出码不变量，并再次执行报告结构校验：

```bash
python critic_runner.py verify-run .critic-runs/<run-directory> --source path/to/draft.md
```

省略 `--source` 时仍会检查归档内部文件，但会明确警告原稿字节没有重新核对。对于 `collected` critic 运行，验证器还要求 `adjudication.json` 与报告逐条一致；如果存在 `revision-plan.md`，会从当前裁决重新生成并逐字比较；复议留存的 `revision-plan.previous-<hash>.md` 也会核对文件名中的内容哈希前缀。这个机制用于发现意外损坏和不一致，不是带密钥的防篡改签名；能同时修改文件与 manifest 的攻击者仍可重算哈希。

## 运行材料不会再丢

`prepare` 和 `run` 都会自动创建 `.critic-runs/<timestamp>--<protocol>/`：

```text
prompt.md       本次真正送给模型的完整提示词
report.md       模型输出（run、文件导入或终端粘贴）
manifest.json   精确字节 SHA-256、生命周期、校验结果与执行信息
adjudication.json  从报告提取的发现、来源绑定和人工裁决（critic 手动流程）
revision-plan.md   只根据完成的人类裁决生成的修改计划
revision-plan.previous-<hash>.md  每次复议前自动留存的旧修改计划
stderr.log      执行器错误输出（仅出现错误时）
```

`.critic-runs/` 和 `.critic-campaigns/` 默认不进 Git。runner 会在启动执行器前先原子写入 prompt 和 manifest，执行完成后再原子补写 report、stderr、退出码和结构校验结果。run schema v3 增加 `collected` 手动回收状态，以及区分 `manual-import` / `terminal-paste`、不含本机路径的 collection 元数据，并继续验证旧版 schema v1/v2；campaign schema v3 记录计划协议、重复次数、统一资源限制、完整运行矩阵、顺序策略、种子和实际执行次序。SHA-256 针对磁盘中的原始字节计算，不受 Windows 换行转换影响；UTF-8 BOM 可以读取但不会混进提示词。JSON 验证会拒绝重复键，避免同一字段出现两种解释。manifest 只保存稿件和返回报告的文件名或粘贴来源标签，不保存本机绝对路径，也不保存执行器参数值。

归档包含完整稿件、模型报告和可能回显敏感信息的 stderr。POSIX 上 runner 把运行目录设为 `0700`、文件设为 `0600`；Windows 上保密性取决于父目录的 ACL。验证器拒绝 manifest、产物和嵌套运行路径中的符号链接，避免归档通过链接逃出预期目录。不要把归档放在共享目录，密钥应通过环境变量传给执行器，并在分享归档前检查 `prompt.md`、`report.md` 与 `stderr.log`。

这样以后真要做 I₁/I₂/C₁/C₂，不会再发生“跑完了但原报告没保存，无法复算 W/B”的情况，也能确认四次到底用了哪一版协议。执行器启动失败、中断或超时时，manifest 会分别记录 `start_failed`、`interrupted` 或 `timed_out`，而不会把未完成运行伪装成成功。

常用退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 执行器成功且报告结构有效 |
| `2` | 输入、协议或执行器启动错误 |
| `3` | 执行器成功，但报告结构无效 |
| `4` | 归档文件或 manifest 自相矛盾 |
| `6` | scorecard 缺项、未填写或格式无效 |
| `7` | campaign 中至少一次运行失败 |
| `8` | 报告回收、人工裁决或修改计划工作流无效 |
| `124` | 执行超时 |
| `125` | 执行器输出超过额度 |
| 其他非零值 | 执行器自身的失败码 |

## Claude Code 仍然可以用，但只是适配器

仓库根目录的全部协议文件仍保留 Claude Code 能识别的 YAML frontmatter，所以原来的安装方式仍可选：

```text
~/.claude/agents/critic-individualist.md
~/.claude/agents/critic-contrastivist.md
~/.claude/agents/citation-auditor.md
~/.claude/agents/critic-social-science.md
~/.claude/agents/critic-natural-science.md
~/.claude/agents/critic-engineering.md
```

独立 runner 会自动剥掉这段 provider-specific frontmatter，只读取真正的审查协议。因此以后即使完全不用 Claude Code，也不需要维护第二套 prompt。

## 两个辅助审查器

`diff-reviewer.md` 用于代码修改后的验收审查。它要求调用方**原样引述用户请求**作为验收标准，并显式给出 worktree / commit-range / branch 范围；没有标准就应 blocked，不能由上游模型自己编一份。

`citation-auditor.md` 的关键制度是证据对称：不仅“不通过”要来源，“通过”也必须有证据链。书目证据和内容证据分轨评级；内容证据不够时不得靠模型记忆判“明确支持”。独立 runner 可以负责打包它的 prompt，但真正执行它的模型仍必须具有可核查外部来源的能力，否则应返回 `partial` / `blocked`，而不是伪造确认。

`build-verifier.md` 保留为旧版适配器，但不属于 critic divergence 的核心。构建、类型检查、lint、test 本质上应由确定性脚本 / CI 执行，而不是让语言模型决定跑什么。

## 共同失败出口

审查报告末尾统一使用：

```text
STATUS: complete | partial | blocked
UNVERIFIED: <逐条列出没有确认的内容；没有则写 none>
```

这不是装饰。没有合法的失败出口，模型最容易用一份语气完整的答案填补自己其实没检查到的东西。

## Divergence test 是校准工具，不是日常流程

`divergence-test.md` 回答一个很窄的问题：`critic-individualist` 与 `critic-contrastivist` 的差异，是否明显大于同一 critic 重跑产生的采样噪声。

在新的项目结构里，它概念上属于 `evaluation/divergence`：W/B 只能说明 critic 是否产生不同输出，不能说明输出是否正确或有用。质量判断必须由真实问题标签、人工裁决和实际修订结果组成的 benchmark 提供。

它是 **否决-only** 的：高分歧不能证明意见正确，更不能证明值得每篇都花 token。第二级控制件在 `test/critic-generic.md`，故意不参与普通审稿；runner 要求显式传 `--allow-test-artifact` 才允许执行它。所有非引证 critic 的六节骨架、原子化要求、逐条跟进量和强制判断项相同，generic 只缺少专用框架承诺。

正式测试时仍然跑 I₁、I₂、C₁、C₂，并人工按“同处同因 / 同处异因 / 独有”拆原子指控。不要让模型自己给自己的分歧打分；`campaign` 和 `score` 只负责隔离运行、留档和复算。完整公式和判据见 `divergence-test.md`。

## 开发检查

```bash
python -m unittest discover -s test -p 'test_*.py'
```

CI 在 Ubuntu 与 Windows 上分别覆盖 Python 3.10 和 3.14；外部 action 固定到已核对的发布提交 SHA，工作流权限只读。

项目目前刻意只用 Python 标准库。工具边界仍然明确：**组装协议、构建 Argument IR、选择机器可读检查、受限串行执行、完整留档、确定性结构校验、可复算计分**。联网检索和语义配对属于人的判断层，不偷偷塞进 runner。
