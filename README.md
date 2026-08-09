# Critic Divergence Tester

[![Tests](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml/badge.svg)](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml)

一个**模型无关、零第三方依赖**的论证审查框架。提示词只是入口，不是最终产品；项目正在从 Critic Divergence Tester 演进为更完整的 Argument Review Framework，divergence 只是其中一个评估子系统。

它最初以 Claude Code subagent 的形式出现，但核心从来不需要 Claude Code。现在仓库把“学术线”“具体审查协议”和“模型执行器”分开：学术线决定证据观，协议决定攻击入口，Claude Code、其他 CLI、本地模型、网页聊天都只是可替换的执行器。

这个项目也不是“让多个 agent 投票得出正确答案”。它做的是 philosophical pressure-test：让不同前提的敌对读者分别攻击同一份稿件，然后由作者本人判断哪些攻击真的改变论证。

```mermaid
flowchart LR
    A["原稿"] --> B["学科适配协议"]
    B --> C["自包含 prompt"]
    C --> D["任意 AI 的报告"]
    D --> E["结构校验与证据归档"]
    E --> F["逐条人工裁决"]
    F --> G["可执行修改计划"]
```

runner 会把每一步绑定到精确字节哈希：AI 负责提出批评，人负责接受、拒绝或暂缓，机器负责阻止漏项、伪完成、证据被改写和归档断链。

## 5 分钟上手（第一次用就看这里）

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

社科方法矩阵现在位于 [`ir/social-science-checks.json`](ir/social-science-checks.json)。每条规则都声明适用的主张类型、研究方法、检查问题、失败条件和所需上下文。因果机制与替代解释适用于所有因果 Claim；时间顺序、混杂、反向因果和选择偏差只适用于标明 `causal-observational` 或 `causal-experimental` 的经验识别 Claim，避免把观察窗口、样本选择等问题机械套到概念分析和形式模型。`core` 是较短的必要检查，`full` 会加入识别假设、溢出、稳健性等扩展检查。

check plan 使用规范化引用结构：完整 Argument IR 只保存一次，选中的检查定义也只保存一次，每个 task 只有 `id`、`claim_id`、`check_id`。模型结果不再复制一遍可能歧义的引文，而是用 `evidence_refs` 引用 IR 节点；程序再确定性解析原文与位置。`pass` / `fail` 必须给出与该 Claim 处于同一论证链的证据节点，分类有疑问时必须显式返回 `uncertain`，不存在可以让任务静默消失的 `not_applicable` 出口。

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

这个 fixture 包含“总会”式过强主张、漏 Claim、错 Evidence 绑定、显式 Assumption 和 Citation，可用于体验校正。Workbench 当前只支持单 Project / D1 / V1；跨版本 lineage、resolution、citation verification 和 GUI 尚未实现，真实文章 Gate A 也尚未执行。

完整 artifact lifecycle、parent hash 和 field provenance 约定见 [`docs/artifact-contracts.md`](docs/artifact-contracts.md)。

### Phase 2：从 Claim 进入 Review Findings

完成 IR 校正后，可以直接在同一个本地项目里运行现有 social-science Rule Lens。程序仍不调用或绑定任何模型 SDK：它只确定性选择适用于 Reviewed IR 的 checks，并生成一份 source/hash-bound prompt。

```powershell
# 1. 根据 Reviewed IR 和 bundled social-science rule library 创建 Rule Review
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

`claim-review.md` 按 Claim 显示 PASS / FAIL / UNCERTAIN，不生成论文总分。FAIL 与 UNCERTAIN 同时产生独立 `argument-finding` artifact，绑定 `V1:Cn`、Rule Lens、check、reason、evidence refs、原始模型结果和 target IR，初始状态只能是 `open`。模型 verdict/reason 始终标记为 `model-derived`。

### Phase 3：人工裁决与 Revision Plan

每条 open Finding 都由作者最终决定；模型不能自动 accept。Workbench 使用与旧 report workflow 相同的 decision 语义，但把每个决定和修改动作保存成独立、不可变、Claim-centered artifact：

```powershell
# 逐条查看 Finding，并选择 Accept / Reject / Defer；每次确认立即落盘
py -3 critic_runner.py ir adjudicate $project

# 只读查看当前状态，不进入交互
py -3 critic_runner.py ir adjudicate $project --view-only

# 大队列先看按 check 与 Claim 聚合的只读 open 摘要
py -3 critic_runner.py ir adjudicate $project --summary-only

# 大队列可先处理 FAIL，再按 Claim 或精确 check 缩小范围；仍然逐条人工确认
py -3 critic_runner.py ir adjudicate $project --verdict fail
py -3 critic_runner.py ir adjudicate $project --claim C4
py -3 critic_runner.py ir adjudicate $project --check causal.alternative-explanation

# 随时确定性重建 revision plan；--show 同时输出到终端
py -3 critic_runner.py ir revision-plan $project --show

# 验证 Finding → Adjudication → RevisionAction 和所有精确父哈希
py -3 critic_runner.py ir verify-project $project
```

`--summary-only` 不要求交互终端，只给出范围内的 FAIL/UNCERTAIN、人工决定计数，以及 open queue 的 check/Claim 聚合，不创建 adjudication 或 revision-plan。`--verdict fail|uncertain`、`--claim C4|V1:C4` 与 `--check CHECK_ID` 可以组合，只改变本次显示和交互队列，不改变 Finding、不批量写决定，也不把被过滤掉的条目视为 resolved。之后不带过滤器再次运行即可继续其余 open Findings。

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

### Product Gate A：先用真实文章验证，再进入 Phase 4

Phase 3 完成后不能直接增加 Perspective Lens。`ir gate-a` 把 3–5 篇真实稿件的 Phase 1–3 结果固定为一个私有、本地 evidence corpus。它只保存 workspace locator 与精确哈希，不复制稿件正文；建议输出目录使用 `*.product-gate-a/`，该模式默认不进 Git。

```powershell
# 三个变量分别指向已经完成 IR correction、Rule Review、人工裁决和 revision plan 的真实项目
$gate = "D:\private-evaluation\workbench.product-gate-a"
py -3 critic_runner.py ir gate-a init $gate $project1 $project2 $project3

# 每篇稿件追加一次人工 assessment；--anchor 可重复，用于保存已知重要 Claim、抽取陷阱、Finding 或框架反转
py -3 critic_runner.py ir gate-a assess $gate P1 `
  --comparison clearer --burden acceptable `
  --correction-minutes 18 --missed-claims 1 --wrong-claim-types 2 `
  --wrong-relations 1 --rhetoric-as-claims 0 --reversed-attributions 0 `
  --anchor "‘总会’的 denominator problem" `
  --anchor "吉拉尔 attribution/citation issue" `
  --actual-revision-notes "作者收窄了该 Claim，并补充比较范围" `
  --notes "校正成本仍可接受"

py -3 critic_runner.py ir gate-a report $gate --show
py -3 critic_runner.py ir gate-a verify $gate
```

对 P2/P3（以及可选的 P4/P5）完成 assessment 后，报告才会显示 `Ready for human gate decision: yes`。程序永远不会自动通过 Gate；只有人类 evaluator 可以追加决定：

```powershell
py -3 critic_runner.py ir gate-a decide $gate pass --reason "真实语料上的控制性与校正成本达到进入 Phase 4 的要求"
```

`pass` 前强制要求 3–5 个互不相同的 source、每个 workspace 的全部 hash 仍与捕获时一致、所有 Finding 已 adjudicate、每篇都有人工 assessment 和至少一个 regression anchor。报告只给出 Claims、corrections、accepted/rejected/deferred/open Findings、抽取错误和人工成本等计数，不生成质量分数。详细协议见 [`docs/product-gate-a.md`](docs/product-gate-a.md)。仓库中的 synthetic/现实结构 fixture 只测试工具机制，不能充当 Gate A 的真实文章，也不能证明 Gate 已通过。

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

## 你平时到底怎么用

不要跑 I₁/I₂/C₁/C₂。那是验证 critic 是否真的不同的测试，不是日常工作流。

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
