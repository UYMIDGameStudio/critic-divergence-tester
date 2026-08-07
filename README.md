# Critic Divergence Tester

[![Tests](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml/badge.svg)](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/workflows/tests.yml)

一组**模型无关**的敌对审查协议，以及一个零依赖的独立 runner。

它最初以 Claude Code subagent 的形式出现，但核心从来不需要 Claude Code：两个 critic 本质上是两套不可互相让步的审查前提。现在仓库把“审查协议”和“模型执行器”分开。Claude Code、其他 CLI、本地模型、网页聊天都只是可替换的执行器。

这个项目也不是“让多个 agent 投票得出正确答案”。它做的是 philosophical pressure-test：让不同前提的敌对读者分别攻击同一份稿件，然后由作者本人判断哪些攻击真的改变论证。

## 你平时到底怎么用

不要跑 I₁/I₂/C₁/C₂。那是验证 critic 是否真的不同的测试，不是日常工作流。

写稿时只按需要叫一个：

| 什么时候用 | 协议 | 它只追问什么 |
| --- | --- | --- |
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

两个 critic 都值得咬同一篇文章时可以都跑，但必须独立；第二个不能看到第一个的报告。

## 独立运行：不安装任何 agent

需要 Python 3.10+，没有第三方依赖。

先看有哪些协议：

```bash
python critic_runner.py list
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

`run` 一次只启动**一个**执行器进程。项目故意没有并发 fan-out；要跑第二个 critic，就在第一个结束后再运行一条命令。

runner 默认给每次执行 900 秒和 16 MiB 的 stdout/stderr 合计额度；可用 `--timeout` 与 `--max-output-bytes` 调整。超时返回 124，超过输出额度返回 125。输出先流入私有临时文件，不会无界堆在内存里；最终归档只保留额度内的原始字节。非法 UTF-8 会原样留档并判为无效报告，不会被静默替换。

### 一键校准：四次隔离运行 + 可复算计分

需要正式验证两个 critic 是否真的不同，不必再手工管理 I₁/I₂/C₁/C₂：

```bash
python critic_runner.py campaign path/to/old-draft.md --repeat 2 -- your-model-command arg1 arg2
```

`campaign` 仍然严格串行运行，各次执行看不到其他报告。它会在 `.critic-campaigns/<timestamp>--campaign/` 中生成四个独立运行归档、`campaign.json`、可点击的 `SUMMARY.md` 和待填写的 `scorecard.json`。先遮掉 critic 名称，人工完成一对一语义配对，再填写每组的重合、同处异因、左右独有和模糊配对数：

```bash
python critic_runner.py score .critic-campaigns/<campaign>/scorecard.json --format markdown --output divergence-score.md
```

记分器自动计算每次 d 的上下界、W/B 区间及 `reject` / `advance` / `inconclusive` 判决，还会拒绝“同一报告在不同两两比较中原子指控总数不同”的自相矛盾表格。它只接管可确定的算术，不替人判断两条指控是否语义重合。单独建空表可用 `python critic_runner.py init-scorecard scorecard.json`。

### 报告结构校验

三个 critic 使用同一套六节输出骨架。`run` 会自动检查标题顺序、A 编号连续性、第一／二节的一一对应、必填字段、唯一最弱／最强项标记，以及末尾唯一的 `STATUS` / `UNVERIFIED`。`complete` 必须配 `UNVERIFIED: none`；`partial` / `blocked` 必须给出具体未核实原因。

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

`.critic-runs/` 和 `.critic-campaigns/` 默认不进 Git。runner 会在启动执行器前先原子写入 prompt 和 manifest，执行完成后再原子补写 report、stderr、退出码和结构校验结果。schema v2 记录输出额度与截断状态，同时仍可验证旧版 schema v1 归档。SHA-256 针对磁盘中的原始字节计算，不受 Windows 换行转换影响；UTF-8 BOM 可以读取但不会混进提示词。manifest 只保存稿件文件名，不保存本机绝对路径，也不保存执行器参数值。

归档包含完整稿件、模型报告和可能回显敏感信息的 stderr。POSIX 上 runner 把运行目录设为 `0700`、文件设为 `0600`；Windows 上保密性取决于父目录的 ACL。不要把归档放在共享目录，密钥应通过环境变量传给执行器，并在分享归档前检查 `prompt.md`、`report.md` 与 `stderr.log`。

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

仓库根目录的 `critic-individualist.md`、`critic-contrastivist.md`、`citation-auditor.md` 仍保留 Claude Code 能识别的 YAML frontmatter，所以原来的安装方式仍可选：

```text
~/.claude/agents/critic-individualist.md
~/.claude/agents/critic-contrastivist.md
~/.claude/agents/citation-auditor.md
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

它是 **否决-only** 的：高分歧不能证明意见正确，更不能证明值得每篇都花 token。第二级控制件在 `test/critic-generic.md`，故意不参与普通审稿；runner 要求显式传 `--allow-test-artifact` 才允许执行它。三个 critic 的六节骨架、原子化要求、逐条跟进量和强制判断项现在相同，generic 只缺少专用框架承诺。

正式测试时仍然跑 I₁、I₂、C₁、C₂，并人工按“同处同因 / 同处异因 / 独有”拆原子指控。不要让模型自己给自己的分歧打分；`campaign` 和 `score` 只负责隔离运行、留档和复算。完整公式和判据见 `divergence-test.md`。

## 开发检查

```bash
python -m unittest discover -s test -p 'test_*.py'
```

CI 在 Ubuntu 与 Windows 上分别覆盖 Python 3.10 和 3.14；外部 action 固定到已核对的发布提交 SHA，工作流权限只读。

项目目前刻意只用 Python 标准库。工具边界仍然明确：**组装协议、受限串行执行、完整留档、确定性结构校验、可复算计分**。联网检索和语义配对属于人的判断层，不偷偷塞进 runner。
