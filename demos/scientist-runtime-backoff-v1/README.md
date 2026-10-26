# Scientist Runtime：retry/backoff 实证示例

`validated-run/` 不是预设评分器。它在运行时对 `protocol.json` 中的每个策略和每个 seed 生成逐例原始观察，再由 analysis 与 review 两个阶段分别从原始记录重算。源码中没有写入预期赢家。

根目录还保留第一次运行的失败证据：原 adapter 错把 5 个命令参数检查成 6 个，ideation 以非零状态退出，Runtime 将项目判为 `blocked`。没有删除或改写该 attempt；修复写入新的 `validated-run/`，plan version 提升为 `backoff-study-v1.1`。

研究问题：在冻结的瞬态服务仿真中，哪个已声明 retry delay 策略在满足成功率和平均尝试次数约束时，具有最低的实际成功请求 p95 完成耗时？

## 冻结边界

- 2,400 个 seed；
- 5 个预先声明的策略，共 12,000 条逐例记录；
- 随机故障模型、约束、选择顺序和分位数定义均在启动前写入 `protocol.json`；
- 五个阶段最多各调用一次，总时限 180 秒；
- `model_calls=0`、已知费用 `$0`。

失败请求没有伪装成某个耗时惩罚值：成功率是约束，p95 只基于实际成功请求耗时。逐 seed 的成功/失败、尝试次数、实际耗时和故障持续时间都保存在 experiment attempt 的 `raw-observations.json`。

## 复核命令

在 MCP server 根目录运行：

```powershell
$runtime = ".\evidence-research-loop\scripts\scientist_runtime.py"
python $runtime start ".\demos\scientist-runtime-backoff-v1\validated-run"
python $runtime audit ".\demos\scientist-runtime-backoff-v1\validated-run"
python ".\demos\scientist-runtime-backoff-v1\validated-run\independent_recompute.py"
```

审查时应同时检查：

- `final-scientist-report.json` 的 `execution_status`；
- `scientist-audit.json` 的 `valid` 与 `accepted`；
- `experiment-ledger.jsonl` 指向的原始证据哈希；
- `independent-recompute.json` 是否与正式 analysis 完全一致；
- `novelty-assessment.json` 仍为 `unknown`，因为这个零网络运行只记录来源定位符，没有证明新颖性。

## 能力边界

这个运行验证的是：真实命令执行、冻结协议、逐例实验、从证据计算选择、独立复算、费用记录和审计链。它没有使用模型生成研究想法，因此不证明模型创造力、跨领域迁移或生产环境性能；它也不把模拟结果外推为实际服务的保证。

## 本次实际结果

- 执行：5/5 stage command 完成，模型调用 0，已知费用 `$0`；
- 原始观察：12,000 条（5 个策略 × 2,400 个 seed）；
- 约束合格：`fixed_50ms`、`exponential_20ms`、`fixed_75ms`；
- 按冻结规则选中：`fixed_50ms`，实际成功请求 p95 为 `143.515047201 ms`；
- 相对第二名 `exponential_20ms` 的 p95 差为 `38.077146444 ms`；
- 独立复算与 analysis 完全一致；Runtime audit 为 `valid: true / accepted: true`；
- 新颖性仍为 `unknown`。

这里的“选中”只适用于冻结模拟协议。失败请求没有被计为 30 秒之类的虚构实际耗时；它们进入成功率约束并单独保留。
