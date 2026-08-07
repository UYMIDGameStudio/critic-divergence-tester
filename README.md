# Critic Divergence Tester

[![Tests](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml/badge.svg)](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml)

一组**模型无关**的敌对审查协议，以及一个零依赖的独立 runner。

它最初以 Claude Code subagent 的形式出现，但核心从来不需要 Claude Code。现在仓库把“学术线”“具体审查协议”和“模型执行器”分开：学术线决定证据观，协议决定攻击入口，Claude Code、其他 CLI、本地模型、网页聊天都只是可替换的执行器。

这个项目也不是“让多个 agent 投票得出正确答案”。它做的是 philosophical pressure-test：让不同前提的敌对读者分别攻击同一份稿件，然后由作者本人判断哪些攻击真的改变论证。

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
作者自己处理真正成立的发现
  ↓
准备发布时跑 citation-auditor
```

多个 critic 都值得审同一篇文章时可以都跑，但必须独立；后一个不能看到前一个的报告。

## 独立运行：不安装任何 agent

需要 Python 3.10+，没有第三方依赖。

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

`campaign` 仍然严格串行运行，各次执行看不到其他报告。只有四次报告全部成功且结构有效时，它才会在 `.critic-campaigns/<timestamp>--campaign/` 中生成四个独立运行归档、`campaign.json`、可点击的 `SUMMARY.md` 和待填写的 schema v2 `scorecard.json`。scorecard 已从报告第一节提取每条 A 指控及其位置、指控和理由。先遮掉 critic 名称，再在六组比较的 `pairs` 中填写左右 A 编号及 `overlap`、`different_reason` 或 `ambiguous`，完成一组后把 `complete` 改为 `true`；未配对条目会自动计为左右独有。

campaign schema v3 使用带种子的反向轮次平衡：第一轮按种子确定协议顺序，第二轮反向，第三轮再恢复，避免固定的 `I1, I2, C1, C2` 把时间漂移、缓存或执行器热身误当成协议差异。随机种子、策略和实际执行次序全部写入 `campaign.json`；用 `--order-seed published-seed` 可以精确复现，验证器会独立重算顺序并拒绝被调换的记录。

三条学术线也能直接组成方法对照 campaign：

```bash
python critic_runner.py campaign draft.md \
  --track humanities-social-science \
  --track natural-science \
  --track engineering \
  --repeat 2 --order-seed published-seed -- your-model-command
```

这种跨线 campaign 用于比较方法视角，不会生成专属于 I₁/I₂/C₁/C₂ 的 W/B scorecard；后者只在默认两协议、各重复两次且全部成功时生成。

```json
"I1:I2": {
  "complete": true,
  "pairs": [
    {"left": "A1", "right": "A3", "classification": "different_reason"}
  ]
}
```

```bash
python critic_runner.py score .critic-campaigns/<campaign>/scorecard.json --format markdown --output divergence-score.md
```

记分器会重新读取四份归档报告，核对原始字节 hash，并再次提取 claims；scorecard 中的证据清单被修改、报告被替换、路径逃出 campaign、同一条 claim 被重复配对或比较尚未明确完成时都会拒绝计分。随后它自动计算每次 d 的上下界、W/B 区间及 `reject` / `advance` / `inconclusive` 判决。它只接管可确定的算术，不替人判断两条指控是否语义重合。

`python critic_runner.py init-scorecard scorecard.json` 仍可创建兼容的 schema v1 汇总计数表，用于没有 campaign 归档的旧实验；新 campaign 默认使用可追溯的逐条配对 schema v2。整个 campaign 可单独复核：

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

省略 `--source` 时仍会检查归档内部文件，但会明确警告原稿字节没有重新核对。这个机制用于发现意外损坏和不一致，不是带密钥的防篡改签名；能同时修改文件与 manifest 的攻击者仍可重算哈希。

## 运行材料不会再丢

`prepare` 和 `run` 都会自动创建 `.critic-runs/<timestamp>--<protocol>/`：

```text
prompt.md       本次真正送给模型的完整提示词
report.md       模型输出（run 模式）
manifest.json   精确字节 SHA-256、生命周期、校验结果与执行信息
stderr.log      执行器错误输出（仅出现错误时）
```

`.critic-runs/` 和 `.critic-campaigns/` 默认不进 Git。runner 会在启动执行器前先原子写入 prompt 和 manifest，执行完成后再原子补写 report、stderr、退出码和结构校验结果。run schema v2 记录输出额度与截断状态；campaign schema v3 还记录计划协议、重复次数、统一资源限制、完整运行矩阵、顺序策略、种子和实际执行次序，并继续验证旧版 schema v1/v2 归档。SHA-256 针对磁盘中的原始字节计算，不受 Windows 换行转换影响；UTF-8 BOM 可以读取但不会混进提示词。JSON 验证会拒绝重复键，避免同一字段出现两种解释。manifest 只保存稿件文件名，不保存本机绝对路径，也不保存执行器参数值。

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

它是 **否决-only** 的：高分歧不能证明意见正确，更不能证明值得每篇都花 token。第二级控制件在 `test/critic-generic.md`，故意不参与普通审稿；runner 要求显式传 `--allow-test-artifact` 才允许执行它。所有非引证 critic 的六节骨架、原子化要求、逐条跟进量和强制判断项相同，generic 只缺少专用框架承诺。

正式测试时仍然跑 I₁、I₂、C₁、C₂，并人工按“同处同因 / 同处异因 / 独有”拆原子指控。不要让模型自己给自己的分歧打分；`campaign` 和 `score` 只负责隔离运行、留档和复算。完整公式和判据见 `divergence-test.md`。

## 开发检查

```bash
python -m unittest discover -s test -p 'test_*.py'
```

CI 在 Ubuntu 与 Windows 上分别覆盖 Python 3.10 和 3.14；外部 action 固定到已核对的发布提交 SHA，工作流权限只读。

项目目前刻意只用 Python 标准库。工具边界仍然明确：**组装协议、受限串行执行、完整留档、确定性结构校验、可复算计分**。联网检索和语义配对属于人的判断层，不偷偷塞进 runner。
