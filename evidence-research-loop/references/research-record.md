# 方案进化记录与恢复

开始新搜索或跨会话恢复时读取。优先沿用项目已有格式；没有现成结构时，在工作区建立独立研究目录。记录应支持另一位执行者从下一条命令继续，而不依赖聊天记忆。

## 最少产物

| 产物 | 最少内容 |
|---|---|
| 状态记录 | 用户决定、模式、范围、阶段、已完成证据、阻碍、下一命令 |
| Solution Contract | artifact、稳定接口、可变边界、workload、hard gates、目标向量、预算、行为签名 |
| 证据表 | 来源、阅读范围、发现、局限、对应机制假设 |
| 实验协议 | 基线、裁判、数据划分、统计/选择规则、重复、预算、停止条件 |
| 候选档案 | ID、父方案、机制、作者/模型、实现路径/哈希、行为签名、开发结果、资源、状态 |
| 运行档案 | 命令、时间、退出状态、代码/数据/配置版本、种子、原始输出、指标、成本 |
| Archive | 精英、Pareto、多样性 niche 和失败档案；记录加入/淘汰理由 |
| 冻结记录 | 时间、协议/裁判/数据/候选哈希、入围规则、测试访问与修订历史 |
| 结果报告 | 对照、谱系、改进、独立测试、消融、失败、结论、限制、迁移规则 |

## 状态字段

```yaml
research_id: null
mode: engineering_evolution # 或 empirical_research
user_decision: null
scope: null
data_provenance: null
solution_contract:
  artifact: null
  stable_interface: null
  mutable_boundary: null
  workload: null
  hard_gates: []
  objective_vector: []
  resource_envelope: null
  behavior_signature: null
protocol_path: null
protocol_hash: null
baseline_ids: []
archive_path: null
budget_and_stop_rule: null
stages:
  evidence_and_contract: {status: not_started, evidence: []}
  judge_and_baselines: {status: not_started, evidence: []}
  generation_0: {status: not_started, evidence: []}
  failure_driven_iterations: {status: not_started, evidence: []}
  archive_selection: {status: not_started, evidence: []}
  freeze: {status: not_started, evidence: []}
  independent_test: {status: not_started, evidence: []}
  ablation_and_stress: {status: not_started, evidence: []}
  conclusion_and_transfer: {status: not_started, evidence: []}
test_access_log: []
unresolved_limits: []
next_action: null
```

状态使用 `not_started / running / completed / partial / blocked / not_applicable`。阶段完成必须有真实证据。`not_applicable` 要记录原因，不能用来绕过必要的端到端比较。

## Candidate 记录

```yaml
id: c-000
parents: []
generation: 0
role: baseline # conservative / mechanism / exploratory / repair / crossover / simplify
hypothesis: null
mechanism: null
source_ids: []
author_or_model: null
artifact_path: null
artifact_hash: null
behavior_signature: null
hard_pass: null
objective_vector: {}
failure_class: null
runtime_and_cost: {}
archive_status: pending # elite / pareto / niche / failed / rejected_duplicate / retired
decision_reason: null
evidence_paths: []
```

## 恢复规则

恢复时核对记录指向的文件、哈希、工作树和运行产物。先确认独立测试是否已经被访问；一旦被用于改进，它必须降级为回归集。对新目标说明哪些旧结果仍适用，哪些必须重跑。

结果表至少列出候选、父代、集合、独立案例数、重复次数、hard-pass、原始目标向量、严重错误、失败/缺失、耗时/成本和证据路径。将实际执行结果与计划严格分开。能复算分数不代表 workload 真实或协议有效，这些要分别说明。
