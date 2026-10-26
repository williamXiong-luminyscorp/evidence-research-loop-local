# Scientist Mode：编排 AI Scientist-v2 式科研流程

## 何时启用

用户要求以下任一目标时启用 Scientist Mode：

- 从研究方向自动形成可证伪假设；
- 检索文献并检查新颖性；
- 自动管理多分支实验、依据失败继续探索；
- 从原始实验记录形成受证据约束的科学结论；
- 生成论文式研究包并接受独立评审；
- 复演或比较 AI Scientist-v2 一类自主科研系统。

普通软件优化、配置搜索和业务策略比较继续使用工程方案进化。Scientist Mode 是 Evidence Research Loop 上的一层科研编排，不替换原有裁判、档案、冻结、恢复、holdout 和审计机制。

## 完整链路

```text
研究方向与约束
  → 构想与假设生成
  → 文献检索、引用和新颖性门禁
  → 渐进实验树与实验管理
  → 科学分析与结论约束
  → 论文组织与独立评审
  → 冻结、独立测试、消融和证据包
```

本 skill 把五个模块定义为完整 Scientist Mode 的默认设计组合，这是产品范围选择，不是实验证明的普遍必要条件。具体任务可以在协议中关闭不适用模块，但要记录理由并收窄交付范围，例如关闭论文模块后只能交付“受支持结论”，不能声称完成论文生成与评审。某模块是否改善真实结果，只能由未预制答案的任务、对照与消融判断。

## 1. 构想与假设生成

**输入**：研究方向、已知约束、可用数据和算力、允许修改的代码或方法边界。

**输出**：候选假设清单。每项至少包括研究问题、机制、可证伪预测、最小实验、预期指标、资源估计和失败判据。

**证据**：`ideas.jsonl`，记录 idea ID、父 idea、来源、生成器/模型、提示哈希、时间、token、费用和淘汰原因。

**门禁**：仅换措辞、无法实验、依赖不可用资源或没有失败条件的想法不得进入实验树。

## 2. 文献检索、引用与新颖性

**输入**：候选假设、关键词、领域和时间范围。

**输出**：可追溯来源、与最近工作的差异、已知反例、复现资产及新颖性判定。

**证据**：`literature-ledger.jsonl`，逐条保存查询、来源 URL/DOI、标题、作者、年份、检索时间、证据摘录位置和支持/反驳关系。优先论文、官方代码和原始数据；搜索摘要不作为最终来源。

**门禁**：找不到来源不等于新颖；新颖性为未知时允许进入探索，但报告必须标为未知。发现等价先前工作时，把候选降为复现或增量比较。

## 3. 渐进实验树与实验管理

`scientist_runtime.py` 负责五阶段顺序、预算、不可变 attempt、恢复和证据账本。需要多个 Agent 并行推进实验、lemma 或工程义务时，由 experiment stage 的适配器创建并驱动 `research_graph.py`；Research Graph 将工作拆成具备依赖、输出契约和 verifier 的节点：

- 根节点是通过文献门禁的假设；
- 子节点只能做一项可解释的主要改动；
- 节点记录父代、实验配置、代码/数据哈希、seed、命令、资源、原始输出和终态；
- 编译失败、环境失败、实验失败和假设被反驳分开归档；
- 下一分支由失败证据、Pareto/精英档案和机制多样性共同决定；
- 达到调用、时间、费用、代数或无改进停止条件即停止。

两套状态机不会自动替用户产生高质量蓝图：experiment adapter 必须明确把哪些 Research Graph 资产、失败和 revision 记录纳入 `experiments[]`。局部节点全部通过后，仍要执行根级集成、冻结和独立测试。

不能手工植入公开赢家后声称自主发现；公开结果只能作为具名正对照。

## 4. 科学分析与结论约束

**输入**：冻结协议下的逐例原始记录、实验树、失败和资源记录。

**输出**：独立可重算的统计摘要、效应量、不确定性、稳健性检查、消融、异常与反证，以及每条结论的证据映射。

**证据**：`analysis-plan.json`、`analysis-results.json`、独立汇总脚本及其输出。分析计划应在查看 holdout 前固定；探索性分析必须标记为探索性。

**门禁**：不得把失败惩罚当实际耗时，不得把重复执行数当独立样本数，不得从有限测试推断普遍正确。缺少可复算原始数据时，结论状态为未验证。

## 5. 论文组织与独立评审

**输入**：受支持结论、方法、原始证据索引、局限和失败。

**输出**：结构化研究报告或论文、引用清单、主张—证据矩阵、评审意见和逐项答复。

**证据**：`paper/`、`claim-evidence-matrix.json`、`reviews/`。评审器不能只读作者摘要；它必须访问冻结协议、关键原始记录和重算结果。作者与评审使用不同上下文，评审不能修改原始实验。

**门禁**：关键主张缺证据、引用不存在、数字无法重算、独立测试不完整或局限被隐藏时拒绝。评审通过表示证据包满足声明范围，不等于研究结论在所有环境中为真。

## 可执行运行时

项目根目录必须有 `research-plan.json`，Schema 位于 `assets/scientist-runtime.schema.json`。计划固定研究问题、主张边界、冻结输入、总 stage call/时间/已知费用预算、required claim，以及五个 stage 的实际 command。命令以参数数组声明，可使用 `{project}`、`{stage}`、`{attempt_id}`、`{attempt_dir}`、`{request}` 和 `{response}` 占位符。

每个 stage command 是可替换的适配器，可以调用模型、搜索 API、实验脚本、统计程序、Research Graph 或人工审核队列。运行时本身不伪造文献或实验结果。它会：

- 在调用前写入 request、attempt 和哈希链事件；
- 使用清理后的环境和剩余 wall-time 执行命令，超时回收进程树；
- 保存 stdout、stderr、raw/normalized response、退出码、耗时、token/费用及哈希；
- 拒绝命令修改计划、协议锁、既有 ledger 和先前输出；
- 保存失败 attempt，恢复时创建新的 attempt 并关联 retry；
- 从 ledger 和 required claims 派生 acceptance，不能由命令自报总分。

运行时以项目级写锁串行化 `start/resume`，并把协议锁哈希锚定到初始化事件；`audit` 会交叉检查 attempt result/event、normalized output、原始实验证据和派生 ledger。哈希链仍是本地篡改检测，不是外部签名或可信时间戳；需要更强对抗性时，应把发布清单写入独立存储或签名服务。

command backend 是可信任务作者可配置的任意代码执行接口。环境变量会被清理，但当前本地运行时不提供完整 OS 文件系统或网络沙箱；适配器在契约上应只读取 request、声明的冻结输入和直接证据。运行不受信任代码时必须使用独立容器/低权限账号、最小挂载、网络策略和命令 allowlist，不能把提示词或哈希检查当成沙箱。

stage 输出契约：

| Stage | 最小输出 | 运行时门禁 |
| --- | --- | --- |
| `ideation` | `ideas[]`：ID、假设、可证伪预测、最小实验 | 至少一项；缺字段失败 |
| `literature` | `sources[]` 与 `novelty_assessment` | 每条来源要有可定位 locator；零来源时新颖性强制降为 `unknown` |
| `experiment` | `experiments[]`：状态、指标、`evidence_files` | `passed` 但没有本次 attempt 的真实文件不能成为有效证据 |
| `analysis` | `claims[]`：状态和 `evidence_ids` | `supported` 但未引用已验证实验/文献时降为 `inconclusive` |
| `review` | `decision` 与 `claim_reviews[]` | `accept` 仍必须满足至少一个 supported claim 和所有 required claim |

### 运行顺序

1. 建立并冻结 `research-plan.json`，明确研究问题、主张边界、stage adapter、预算和停止规则。
2. 冻结任务契约、数据划分、裁判、分析计划、环境身份和允许的外部来源。
3. `validate` 后执行 `start`；若暂停或进程中断，用 `resume` 接续，不覆盖失败。
4. 在开发数据上执行构想、文献和多代实验树；保存全部失败和成本。
5. 冻结入围假设与实现，再揭盲 holdout；已揭盲数据后续只能作为回归集。
6. 独立汇总原始记录，完成消融、压力测试和反证检查。
7. 生成研究包并执行独立评审，最后运行 `audit` 和 `export`。若依据评审修改方案，建立新 plan version。

## 命令与状态

```powershell
$runtime = '<skill>/scripts/scientist_runtime.py'
python $runtime validate C:\work\scientist-project
python $runtime start C:\work\scientist-project
python $runtime status C:\work\scientist-project
python $runtime resume C:\work\scientist-project
python $runtime audit C:\work\scientist-project
python $runtime export C:\work\scientist-project
```

`start/resume` 的终态、`audit.valid` 和 `final-scientist-report.json` 的 `acceptance.accepted` 是三个不同概念：执行完不等于证据有效，证据结构有效也不等于结论被接受。正式结论必须同时报告三者及局限。

旧 `assets/task-templates/scientist-mode` 是确定性的控制器兼容性冒烟测试：它的分数由裁判直接定义，不能证明模块必要性、科研质量或自主发现。`assets/task-templates/scientist-runtime` 是不含预制研究答案的适配器骨架，默认安全失败，直到用户实现真实 stage command。

当前 Scientist Runtime 专项测试覆盖的是：真实 command 调用、不可变 attempt、失败保留与恢复、来源与实验文件门禁、主张—证据映射、评审接受规则和篡改审计。运行时还复用 Evidence Core 已测试的预算和进程树超时执行器，但仍需在完整集成回归中持续核查。尚未由这些测试证明的是：某个模型会提出有价值的新假设、搜索到完整文献、设计有效实验、产生高质量论文，或五模块在任意领域都必要。只有在具名、未预制答案的真实任务上完成对照和独立验收后才能扩大声明。
