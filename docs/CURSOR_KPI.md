# 在 Cursor 中用 ERL 开发 KPI 系统

KPI 工作区默认可直接使用 Skill 脚本：生成、评测、冻结、Holdout 和审计都在用户登录环境里完成，因此可以使用已有 Codex 登录。用户明确要求标准 MCP 工具、远程恢复或共享执行环境时，也可以使用版本匹配的 ERL MCP；同一项目一次只选一个写入口。

## 1. 安装 Skill

将发布仓库中的 `evidence-research-loop` 文件夹放到 KPI 仓库：

```text
<kpi-repo>/.cursor/skills/evidence-research-loop/
```

目录内必须保留 `SKILL.md`、`references`、`scripts` 和 `assets`。Cursor 也可能发现本机 `~/.codex/skills/evidence-research-loop`；KPI 项目以仓库内 Skill 为准。

只使用本地 Skill 时，`.cursor/mcp.json` 可以不配置 ERL，例如：

```json
{
  "mcpServers": {}
}
```

若改用 `evidence-research-loop-local`，按仓库 MCP 文档配置，并先调用 `get_capabilities` 核对版本和 `engine_hash_valid`。不要让 Skill 脚本和 MCP 同时写同一任务目录。保存配置后执行 Reload Window。

## 2. 调用 Skill

在 Agent 输入框键入 `/`，选择 `evidence-research-loop`；也可以使用 `@evidence-research-loop`。

## 3. KPI 项目第一次提示词

先不要让 Agent 直接生成完整系统。第一条消息使用：

```text
/evidence-research-loop

使用本机 Skill 脚本对当前 KPI 仓库建立工程方案进化项目。本轮保持单一写入口；如果已经通过 MCP 创建项目，就继续使用同一个 MCP 项目而不要并发运行脚本。

目标：为全公司员工年度目标流程开发 KPI 系统。员工提出职责、主要任务和年度目标，与主管共同确认；支持年中检查、批准后的目标变更、年底按成果与证据回顾。

本轮只完成阶段 0 和阶段 1：
1. 读取当前代码、测试和文档，确定真实基线；没有代码则建立最小可运行基线。
2. 建立 Solution Contract，明确稳定接口、可修改范围、开发数据、独立 holdout、预算和停止条件。
3. 设计并故障测试 evaluator；保存正确、错误和边界样例。
4. 把系统拆成 Composition Graph：身份与权限、目标生命周期、主管确认、变更版本、证据附件、年中检查、年底回顾、审计日志、根级集成。
5. 不启动付费模型搜索；先报告拟用模型、最大调用数、单次超时、总时限和费用是否可知。

硬门禁：
- 员工不能批准自己的目标或变更；
- 未批准变更不能影响正式目标；
- 历史版本和审计记录不可静默覆盖；
- 权限必须按员工、主管和 HR 管理角色验证；
- AI 不得自动决定员工绩效、统一权重或公司排名；
- 缺少证据时必须标记不确定，不能捏造完成情况；
- 所有结论区分开发集、回归集、独立测试和真人试点。

最终返回：当前基线、协议路径、裁判校准结果、Composition Graph 状态、未解决决策和下一条可执行命令。
```

这条提示词的目的不是一次生成“最佳 KPI 系统”，而是先让 Cursor 建立可信的基线和裁判。

## 4. 后续运行顺序

阶段 0/1 完成并人工确认契约后，再发送：

```text
/evidence-research-loop

继续当前任务目录。直接 Skill 路径先运行 evolution_controller.py validate；只有协议、基线、裁判和冻结输入都有效时才 start。生成至少两个机制不同的可执行候选，使用预先声明的预算运行多代；完成后运行 audit_search.py。若本轮选用 MCP，则使用对应 validate/start/status/audit 工具完成同一流程。最后分别报告结论、基线、证据、失败和未验证项。
```

Agent 应使用如下脚本顺序：

```text
evolution_controller.py scaffold
→ 写入 task.json / baseline / evaluator
→ evolution_controller.py validate
→ evolution_controller.py start
→ evolution_controller.py status 或 resume
→ audit_search.py
→ evolution_controller.py export
```

大型 KPI 系统内部再使用 Composition Graph 验证子能力和真实集成。Research Graph 可以分配并行开发节点，但节点通过不能替代根级集成、holdout 和人工政策审核。

## 5. 什么时候可以说“做得更好”

至少需要：

- 一个可运行基线和两个机制不同的可运行候选；
- 同一冻结 evaluator 下的开发比较；
- 失败驱动的修复或明确的无改进停止；
- 冻结后未用于调优的 holdout；
- 权限、版本、审计、并发和异常恢复测试；
- 真人可用性、公平性和政策解释仍由人工试点验收。

合成数据和 AI 模拟可以验证离线正确性，不能证明员工更愿意填写、流程更公平或组织绩效提高。

## 6. Cloud Agent

上述本机路径适用于 Cursor Desktop。Cursor Cloud Agent 没有你的本机 Codex 登录；若必须在云端运行，需要单独提供生成后端和凭证，不能假设与本机 Skill 或本地 MCP 配置相同。
