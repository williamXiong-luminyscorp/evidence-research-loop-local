# Research Graph：可执行的内层研究协作图

Research Graph 把一个完整候选内部的工作拆成带依赖和验收契约的节点。它与候选谱系并存：候选谱系比较整体方案；Research Graph 负责让多个 Agent 组合完成一个方案。

它是通用 clean-room 实现。设计受到 Prove2Me 的不可变问题/证明分离和可复用验证资产、Goedel-Architect 的 blueprint/refinement 思想启发；没有复制 Prove2Me workspace 或 Goedel-Architect 源码。来源和许可证边界见 [设计影响与第三方边界](design-influences.md)。

## 蓝图

蓝图遵循 `assets/research-graph.schema.json`。每个节点至少声明：

- `id/type/statement`：节点身份、类型和要成立的主张；
- `dependencies`：只有全部依赖已验证，节点才会进入 ready 队列；
- `output_contract` 与 `scope`：决定已验证资产能否安全复用；
- `verifier`：参数数组形式的冻结验证命令、时限和结果文件；命令中的本地文件会自动冻结哈希，间接导入的脚本/配置用 `frozen_files` 明确登记，确需传递的环境变量用 `env_allowlist` 列出；
- `priority/max_attempts`：调度优先级和失败上限。

验证命令支持 `{project}`、`{artifact}`、`{attempt_dir}`、`{result_dir}` 和 `{node_id}`。命令非零退出、超时、缺失/非法结果或 `pass_field` 不为 `true` 均失败。

## 运行

```powershell
$graph = "<skill>/scripts/research_graph.py"
python $graph init C:\work\research C:\work\blueprint.json
python $graph claim C:\work\research --worker agent-a --lease-seconds 900
python $graph execute C:\work\research node-id C:\work\candidate.zip `
  --worker agent-a --token <lease-token> `
  --contributor-kind agent --contributor-id agent-a --model <model>
python $graph status C:\work\research
python $graph audit C:\work\research
```

`claim` 是文件锁保护的原子操作，同一节点不能同时分配给两个 worker。长任务使用 `heartbeat` 续租。worker 中断后执行 `resume`：已经落盘的 verifier 终态会被登记；过期且没有终态的 attempt 会保留为 interrupted，再依据 attempt 上限重新开放或失败。

### 多 Agent 调度循环

协调者重复执行以下循环，直到 goal verified、没有 ready 节点或达到预算：

1. 读取 `status`，只向 Agent 公开它领取的节点、直接依赖资产和必要公共上下文；
2. 每个空闲 Agent 用唯一 worker ID 调用 `claim`，保存 node ID 和 lease token；返回的 `dependency_assets` 只包含该节点的直接已验证依赖；
3. Agent 工作期间周期性 `heartbeat`，完成后提交不可变 artifact 并调用 `execute`；
4. verifier 失败时保留 attempt，并用 `fail` 的 failure class/reason 驱动下一次局部修复；
5. 同一类失败反复出现或 statement 本身错误时，协调者提交 blueprint revision，而不是无限重试；
6. goal 节点 verified 后仍运行整个候选的根级集成、holdout 和审计。

Research Graph 管理并发一致性和证据，不会自己调用模型生成 artifact。Codex Skill、MCP 客户端或领域调度器负责启动 Agent 并把领取结果交给它们；因此“图已初始化”不能写成“自主研究已完成”。

## 失败与蓝图修订

节点失败时，下游节点变为 `stale`，旧证据不删除。使用版本化 revision JSON 做拆分或重连：

```json
{
  "expected_revision": 1,
  "reason": "原实现节点过大；根据失败记录拆成两个可独立验证的任务",
  "add_nodes": [
    {"id": "part-a", "type": "implementation", "statement": "...", "dependencies": [], "output_contract": {}, "verifier": {"id": "tests", "command": ["python", "verify.py", "{artifact}"]}},
    {"id": "part-b", "type": "implementation", "statement": "...", "dependencies": [], "output_contract": {}, "verifier": {"id": "tests", "command": ["python", "verify.py", "{artifact}"]}}
  ],
  "dependency_updates": [
    {"node_id": "integration", "dependencies": ["part-a", "part-b"]}
  ],
  "supersede": [
    {"node_id": "old-large-node", "replacement_ids": ["part-a", "part-b"]}
  ],
  "invalidate": []
}
```

运行 `python research_graph.py revise <project> <revision.json>` 后生成不可覆盖的 blueprint revision。未受影响且仍符合契约的 verified 节点保留；依赖改变的节点必须重新验证。

## 资产与 credit

通过 verifier 的产物进入 `verified-assets.json` 和内容寻址文件目录。`reuse` 要求 statement、直接依赖资产身份、`output_contract`、verifier contract 和 scope 全部兼容；只因输出都是 JSON/文本不能复用。若节点语义或输入改变，应重新运行目标 verifier。

原始 lease token 只返回给领取者；event、attempt 和 status 只记录不可逆 token 指纹。过期 lease 在 heartbeat、execute 和 fail 时都会被拒绝并进入恢复流程。verifier 子进程使用清理后的最小环境，且冻结文件在每次执行前及审计时重新校验。

每次成功、失败和资产复用都写入哈希链 `contributions.jsonl`，记录节点、attempt、人员/Agent/模型、角色、来源、产物哈希、验证器、费用和耗时。`graph-events.jsonl` 独立保存调度及蓝图事件。`audit` 同时复核两条链、蓝图版本、资产哈希、attempt 终态和根目标状态；`valid` 表示证据结构完整，`accepted` 还要求全部 goal 节点已验证。

Credit 账本回答“谁/什么系统对哪个已验证或失败节点做了什么”，不回答作者排序、所有权、奖励分配或工作质量的全部问题。外部模型和来源的许可证、条款与引用仍须单独记录。

## 领域适配边界

- 数学：节点是 theorem/lemma，verifier 是 Lean；
- 软件：节点是需求、实现、迁移和集成义务，verifier 是编译、测试、安全和性能工具；
- ERP：节点是业务规则、接口和对账义务，verifier 在测试租户和数据库副本运行；
- KPI/HR：可自动验证计算、权限和工作流，但公平性、政策解释及人员决策必须是人工门禁节点；
- 产业研究：节点是带时间范围的事实或预测，verifier 检查来源、冲突、新鲜度或回测。

局部节点通过不能取代根级集成测试、冻结 holdout 和人工批准。Research Graph 提供内层协作；原有 Evidence Core 继续负责整体候选的独立验收。
