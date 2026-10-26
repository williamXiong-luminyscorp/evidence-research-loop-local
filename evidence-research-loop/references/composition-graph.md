# Composition Graph：带组合证据的工程资产图

当多个子模块需要组成一个系统，或同一目标有多条替代实现路径时使用。普通任务 DAG 说明先做什么；Composition Graph 记录每条组合路径依据什么接受，条件变化后哪些证据仍然有效。

本模块独立实现 Prove2Me 公开方法中的主张与提交分离、条件归约、多路径和可复用证据语义。当前是工程经验验证后端，不是 Lean 证明内核，不是 Prove2Me 平台或 Goedel-Architect 搜索引擎的完整复现。它不自行生成候选或重构蓝图；Agent 负责提案，冻结验证器负责验收。

## 选择与组合

- 一个 Claim 可以有多个 Submission/Reduction，任一有效完整路径可支持该 Claim：OR。
- 一条 Reduction 的所有必要前提必须满足：AND。它还必须有独立执行的组合产物与验证证据，不能只有前提 ID。
- 条件组合检查通过，只表示在声明假设下检查通过。实际子产物尚未就绪、绑定已变化或根级集成未通过时，不能接受父目标。
- 使用 mock 的组合检查不能替代实际模块的集成。根目标还需要冻结的独立验收；测试揭盲后用于改进的输入转为回归数据，按主流程建立新独立测试版本。

每份证据都要保留保证类别与范围。有限测试、仿真或有界模型检查不能写成一般数学证明；哈希审计只证明记录一致性，不能证明契约或测试充分。

## 创建和执行

蓝图格式见 [`assets/composition-graph.schema.json`](../assets/composition-graph.schema.json)。每项 Claim 必须声明 `statement`、`contract`、`environment`、`scope`、`guarantee: "empirical"` 和 `verifiers`。目标 Claim 必须同时提供 `integration` 与 `holdout` 验证器；需要人工语义确认的 Claim 增加 `requires_human_review: true`。正式主张或环境改变时创建新的项目版本，初始化后的 Claim 不可改写。

```powershell
$graph = "<skill>/scripts/composition_graph.py"
python $graph init C:\work\composition C:\work\composition-blueprint.json
python $graph submit C:\work\composition C:\work\submission.json
python $graph status C:\work\composition
python $graph audit C:\work\composition
python $graph resume C:\work\composition
```

`submit` 接受四种请求，`artifact` 路径相对于请求 JSON 文件：

```json
{"kind":"direct","claim_id":"component-a","artifact":"candidate.json"}
```

```json
{"kind":"reduction","claim_id":"system","premises":["component-a","component-b"],"artifact":"composition.json"}
```

`reduction` 返回本次 `evidence_id`。其组合检查通过后仍只是 `conditionally_validated`。真实子产物各自通过后，提交精确绑定；不能只绑定 Claim ID 或“最新版本”：

```json
{"kind":"validate","reduction_id":"ev-reduction","bindings":{"component-a":"ev-a","component-b":"ev-b"}}
```

已配置反例验证器时，可以提交：

```json
{"kind":"refutation","claim_id":"component-a","artifact":"counterexample.json"}
```

上述 `ev-*` 为说明标记，实际必须使用引擎返回的证据 ID。请求可附带 `author` 对象保存来源；未执行外部模型时不要填成模型发现。

### 验证器输入

每个 verifier 使用参数数组 `command`，支持 `{python}`、`{project}`、`{artifact}`、`{request}`、`{result_dir}`、`{attempt_dir}` 和 `{claim_id}`。默认读取新结果目录内的 `result.json`，要求 `passed` 严格等于 `true`；也可显式配置 `result_file`、`pass_field`、`timeout_sec`、`frozen_files` 和 `env_allowlist`。每阶段默认时限 30 秒，上限 120 秒。裁判脚本及间接依赖必须冻结；非零退出、超时、缺失/非法输出及哈希变化均不能通过。

`{request}` 是本次阶段的只读输入说明，含 `phase`、精确 `claim`、候选 `artifact`、`premise_claims` 及 `child_artifacts`。条件检查阶段不提供真实子产物。`validate` 阶段的 `child_artifacts` 按前提 Claim ID 给出实际文件路径、证据 ID、产物哈希与 Claim 身份；验证器必须读取并使用这些实际产物，而非仅检查 ID 或成功标记。命令后端不是完整 OS 沙箱；不受信任的候选仍需独立容器和最小权限环境。

### 读结果

`audit.valid` 仅表示记录结构与文件完整性通过；`audit.accepted` 还要求所有目标具备有效的完整路径。必须同时检查二者，不能把退出码 0 当成整体完成。Claim 状态可能是 `open`、`conditionally_validated`、`accepted`、`refuted` 或 `evidence_conflict`；提交失败不抹掉其他提交。

`resume` 保留已开始但无终态的尝试并处理已死亡执行者，不自动重试模型或把中断改成成功。重试要创建新提交。需要撤销一份证据时使用 `revoke <project> <evidence_id> --reason "原因"`；这不会删除原始记录。

## 失败的传播范围

一次失败首先属于这次提交。编译失败或超时不等于需求被反驳。要作为 Refutation，反例必须经冻结验证器确认，且确实在目标主张的输入域和环境内。

子能力被反驳或其证据撤销，只影响引用它的组合路径；父目标的独立成功路径仍可保留。不要因一条路径失败就把父目标判假，也不要改写旧主张来掩盖反例。需求或正式主张改变时创建新身份；历史提交与失败保留。

证据发生冲突时停止接受相应主张并保留两侧证据，不按评分选一侧。循环相互引用不能形成完成证据。

## 资产检索与复用

搜索用于发现可能可用的资产，不授予复用资格。复用需要精确核对目标契约、适用范围、环境、验证器以及依赖闭包；自然语言相似或文件形状相同都不够。实际目标侧重新验收的结果与原项目来源分别保存，不能把导入时的成功标记当成新项目测试结果。

```powershell
python $graph export C:\work\composition component-a C:\work\registry\component-a.json
python $graph search C:\work\registry "component"
python $graph import C:\work\next-project component-a C:\work\registry\component-a.json
```

当前 `search` 是关键词候选检索，不是语义向量搜索。导入时源项目必须仍可审计且源证据有效；目标项目先具备兼容的前提资产，再实际执行目标侧验证。此格式不是脱离源项目即可信任的签名证书。

目标侧重验产生独立的本地证据，来源关联用于追溯，不是永久的远程状态依赖。源证据后来撤销，不会自动删除已独立重验的目标证据；若撤销原因揭示共同缺陷或适用范围变化，应在目标侧保留反例并撤销相应本地证据。

## 语义审核边界

Canonical milestone 是人与 Agent 共同确定的精确验收目标。需要模仿 Prove2Me 的盲式 read-back 时，独立 Agent 只能看到契约/代码与必要依赖，不能看到原始意图；它说明“实际保证什么”，由人对照原始需求确认。Agent 不能代填人的审核记录。

本模块支持记录 read-back 和审核声明，但不验证人的身份，也没有自动完成盲式回译与人工审核的服务：

```powershell
python $graph readback C:\work\composition goal C:\work\readback.txt --author auditor-a --source-blind-declared
python $graph attest C:\work\composition goal <返回的readback_id> --reviewer <实际审核者> --decision approve
```

`source_blind_declared` 是调用者声明，结果仍记录 `independence_verified: false`；审核声明记录 `human_identity_verified: false`。只有人明确给出决定后才可以登记，不能由 Agent 自行填入人名和 `approve`。未具备审批身份验证的服务，不能把这个记录接口当作访问控制。记录人审结果不等于机器已证明需求表达忠实；缺少该步骤时，报告应明确未验证语义忠实性。KPI/HR 中人员评价、公平性与政策解释仍按任务契约保留人工门禁。

## 与其他执行器配合

外层 `evolution_controller.py` 比较整体候选；`research_graph.py` 分配任务与记录尝试；Composition Graph 管理候选内部的组合证据与替代路径。Goedel 风格的失败驱动蓝图改写属于规划器的行为，不能从图的存在推断该能力已实现或已验收。

可执行接口示例在 [`assets/task-templates/composition-graph`](../assets/task-templates/composition-graph/brief.md)。它包含已知配置和固定测试，仅供基础设施冒烟，不用于证明自主研究、未见数据泛化或机制收益。
