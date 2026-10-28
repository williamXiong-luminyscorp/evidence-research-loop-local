# 自动多代搜索与任务适配

## 产品边界

`evolution_controller.py` 接受一个已经合格的任务契约、基线和裁判，自动执行：

`选父代 → 生成一个候选 → 校验与登记 → 开发集评测 → 更新档案 → 下一候选/下一代 → 预算停止 → 冻结 → holdout → 导出`

它不自动保证自然语言需求能变成可靠裁判，也不把有限预算下的赢家称为全局最优。裁判和 holdout 必须由任务适配器先建立并做故障测试。

## 建立任务

```powershell
$controller = "<skill>/scripts/evolution_controller.py"
python $controller scaffold python-code C:\work\my-search
python $controller validate C:\work\my-search
```

也可从 `json-strategy` 模板开始。需要编排真实科研流程时，使用 `scientist_runtime.py` 和 `research-plan.json`，按 [Scientist Mode](scientist-mode.md) 接入实际构想、检索、实验、分析和评审 command；复杂的并行子任务使用 [Research Graph](research-graph.md)。旧 `scientist-mode` task 模板只保留为结构冒烟测试，评分关系由裁判预设，不能证明模块必要性、科研质量或自主发现能力。外层候选搜索正式启动前编辑 `task.json`、基线、公开 `brief.md` 和 evaluator。模板的固定接口定义在 `assets/task.schema.json`，核心字段如下：

| 字段 | 含义 |
|---|---|
| `artifact` | 基线位置、候选类型及 generator workspace 中的候选文件名 |
| `public_context_files` | 生成器唯一会收到的任务资料 |
| `contract` | 完整 `solution_evolver` 协议：硬门禁、目标、裁判、数据 split、资源与冻结输入 |
| `generator` | `deterministic`、`command`、`codex` 或 `docker-command` 后端及角色 |
| `budget` | 最大代数、每代候选数、调用数、wall time、时限范围、已知费用和无改进停止 |
| `selection` | 精英数量、最小改进、冻结比较数、holdout 重复次数及是否声明独立测试 |

需求变更可能让上一版本赢家在新契约下正确地失败。此时将它作为真实父代与回归对照，并显式设置 `selection.allow_failing_baseline: true`。控制器仍要求 baseline 输出有限主指标，把失败记录放入失败档案，并从 `best_primary_value: null` 开始；首个通过新硬门禁的候选才成为有效改进。若搜索结束仍无候选通过，状态为 `no_eligible_candidate`，不冻结失败 baseline，也不运行 holdout 或宣布完成。重复 `resume` 直接返回同一终态，不追加第二条终态事件。审计把这种终态记为证据结构 `valid: true`、验收 `accepted: false`，并明确 holdout 未运行的原因。普通优化任务不设置该开关，baseline 仍必须先通过硬门禁。

`freeze_comparators` 是最多保留的对照数量。控制器从通过开发硬门禁的可行档案中选对照；失败 baseline 不会因为排在候选池首位而进入冻结集合。若除赢家外没有其他可行候选，实际冻结对照数可以少于配置值。

新任务只增加任务目录，不修改控制器。候选必须是单文件可执行产物；多文件软件首版可把候选定义成补丁、归档或生成配置，由 evaluator 在隔离目录展开。

## 生成后端

`deterministic` 用预制候选验证控制器，不产生模型费用。它不能证明自主搜索能力，
并且仅允许在任务声明 `generator.test_only: true` 且进程设置
`ERL_ALLOW_DETERMINISTIC_TEST_MODE=1` 时运行；正式搜索默认拒绝该后端。

`codex` 调用本机 Codex CLI。示例：

```json
{
  "backend": "codex",
  "model": "gpt-6-luna",
  "timeout_sec": 900,
  "roles": ["conservative", "mechanism"]
}
```

控制器在验证 `codex` 任务时执行 `codex login status`。未认证会在任何候选生成前
停止，并提示用户在私有终端使用 `codex login --with-access-token` 或
`codex login --with-api-key`；访问令牌不得写入 `task.json`。

控制器将 request、公开任务资料、开发反馈和父代源码嵌入 prompt，使用 ephemeral session、忽略用户配置和项目规则，并显式启用 `read-only` sandbox。模型不使用文件工具；它在符合 `response-schema.json` 的最终 JSON 中返回假设、机制、摘要和完整候选文本，由控制器校验后写入候选文件。

`command` 接入任意本地 Agent 命令。命令参数支持 `{workspace}`、`{request}`、`{response}` 和 `{candidate}`。外部命令必须在 `response` 写合法 JSON，并在 `candidate` 写候选。

`docker-command` 是严格读取隔离入口。配置必须包含镜像引用、实际 `docker image inspect` 得到的完整 `expected_image_id`、资源限制和容器内命令。控制器执行前核对镜像 ID，只挂载 `/workspace`，默认 `--network none`；模型 API 调用需要网络时显式设置网络并只传 `env_allowlist` 中的凭据。普通 `codex` 后端提供工作目录与写权限隔离，但不等于独立容器的读取隔离。

## 运行、查看与恢复

```powershell
python $controller start  C:\work\my-search
python $controller status C:\work\my-search
python $controller resume C:\work\my-search
python $controller export C:\work\my-search
```

`start`、`run` 和 `resume` 都进入同一个幂等状态机。不可修改已启动任务的 `task.json`、控制器、契约或冻结文件；若需修改，复制为新版本任务。每个 generation/slot 使用固定 request ID。恢复规则：

- 已有终态事件：跳过；
- 调用结果、响应和候选都已落盘：登记/评测，不再次调用；
- 已调用但结果未知：记录放弃，不自动重试；
- 已冻结但 holdout 未完成：继续缺失的 candidate/repeat；
- 已完成：只重建报告。

调用计数由 `search-events.jsonl` 重建，而不是信任可能在中断点落后的 state 数字。`generation_call_finished` 先追加并 fsync，随后才更新 state；两者之间崩溃时，恢复只得到一个完成事件和一个完成计数。恢复在预算判断前处理已有 `call-result.json`，因此最后一次已付款、已落盘的结果不会因为调用额度正好耗尽而丢失。

完成状态由冻结矩阵反推。矩阵是 `comparison_candidate_ids × holdout_repeats`；每一格必须存在当前 freeze ID、完整 `run.json.evaluation` 和对应 `holdout_finished`。启动失败、缺失结果或非法结果不会变成“零次运行但完成”。再次 `resume` 会补跑缺失格；审计器也独立按同一矩阵核对。`completed_with_holdout_failures` 表示执行完整但方案未通过独立硬门禁，不能当作验收通过。

如果 holdout 在访问开始记录和 `run.json` 已落盘后中断，原 run 不删除也不填充冻结矩阵。控制器把它标记为 `attempt_terminal.status: interrupted`，在控制器事件与 append-only 访问记录中登记原因，并让新 run 用 `retry_of_run_ids` / `retried_by_run_id` 建立双向关联。审计器要求中断事件、访问事件和成功重试关联三者一致；重试完成后，旧中断是可核查的终止尝试，完整的新 run 才计入冻结矩阵。

`max_wall_time_sec` 从一次性协议初始化与基线校准完成后开始，覆盖候选生成、开发评测、冻结和最终 holdout。每个外部进程的 timeout 取自身上限与剩余预算的较小值，并预留 0.25 秒给事件和状态落盘。超时时，Windows 把外部进程放入启用 `KILL_ON_JOB_CLOSE` 的 Job Object 并终止整个 Job；POSIX 为外部进程建立新 session 并终止其进程组。运行记录保存是否完成进程树终止及相关错误。时限先于其他停止原因；没有足够预算完成冻结/holdout 时保留 `holdout_incomplete`。如果业务要求连首次基线校准也计入时限，应在外层作业给整个命令设置更大的独立硬超时，并在任务契约中同时记录两个范围。

## 独立 seed 核对

模板从冻结的 `seeds.json` 读取 dev/holdout。要声明新独立测试，先运行历史扫描：

```powershell
python <skill>/scripts/check_seed_novelty.py `
  --planned C:\work\my-search\seeds.json `
  --history-root C:\work\prior-evidence `
  --output C:\work\my-search\seed-novelty.json
```

把 `seeds.json`、`seed-novelty.json` 加入 `contract.frozen_input_files`，设置 `contract.seed_ids_file`、`contract.seed_novelty_file` 和 `selection.independent_holdout_claim: true`。控制器会核对 seed 文件哈希、holdout 列表、历史交集和 `valid`；任何不一致都拒绝启动。已经使用过的数据只能标为回归复测。

## 档案与选择

硬门禁失败的候选只能进入失败档案。通过者按目标向量计算 Pareto 前沿；主要指标生成精英列表；每个生成角色保留一个 niche 代表。下一父代来自 Pareto、niche 和精英池，`repair` 优先选择最近失败候选。

候选登记使用 SHA-256 做源码去重。完全重复留下 `generation_duplicate` 事件，不再次执行 evaluator。评测后将契约声明的行为签名哈希分组；行为相同但源码不同的候选仍保留证据，但报告会明确指出相似组。

## 证据目录

- `search-state.json`：原子更新的预算、阶段、当前代、冻结和恢复状态；
- `search-events.jsonl`：append-only、逐条 SHA-256 链接的控制事件；
- `generator-workspaces/gNNN-sNNN/`：request、prompt、候选、响应、stdout/stderr、usage 与调用结果；
- `archive.json`：底层候选谱系和评测引用；
- `search-archive.json`：精英、Pareto、niche、失败和行为相似分组；
- `runs-v2/<run-id>/`：独立 evaluator 目录、命令、版本、退出状态和原始逐例结果；
- `protocol-locks/`、`freezes.json`、`holdout-access.jsonl`：协议锁、冻结版本和不可清除的 holdout 访问历史；
- `final-report.json` / `final-report.md`：机器可读和人工摘要。

## 真实搜索启动门槛

启动付费或账户计量的模型前先记录并告知用户：模型名称、两个任务各自的最大代数、每代候选数、最大调用数、单次超时、总 wall time、token/美元价格及停止规则。API 无法提供美元成本时把费用标为未知，不能写成零；用调用上限硬停止。

真实搜索验收至少检查：没有人工替换任何代候选；每个候选有父代、角色、请求、模型响应和调用记录；错误候选未因软分进入精英；预算停止和恢复事件存在；冻结候选与基线在相同 holdout/资源下执行。无改进时选择基线是有效完成。
