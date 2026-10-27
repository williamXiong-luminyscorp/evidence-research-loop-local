# 在 Cursor 中用 ERL 开发 KPI 系统

这份指南把两个不同层次连接起来：

- **Skill** 告诉 Cursor Agent 怎样定义基线、冻结裁判、生成候选、保留失败和限制结论；
- **MCP** 实际创建项目、写入任务文件、启动/恢复搜索、保存证据并执行审计。

只安装 Skill 会得到研究方法，但不会自动获得持久化 MCP 项目和标准工具调用。只连接 MCP 会得到工具，但 Agent 未必主动遵守 ERL 的研究边界。KPI 项目建议两者同时使用。

## 1. 在 KPI 项目配置本地 MCP

在 Windows 用户环境中设置 ERL MCP 目录，然后完全退出并重启 Cursor：

```powershell
[Environment]::SetEnvironmentVariable(
  'ERL_MCP_HOME',
  'C:\Users\WilliamXiong\OneDrive - Luminys System Corp\Documents\ChatGPT\ai agent\supply-chain-lab\shinka-general-researcher\mcp-server-v1',
  'User'
)
```

在 KPI 仓库创建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "evidence-research-loop-local": {
      "type": "stdio",
      "command": "${env:ERL_MCP_HOME}\\.venv\\Scripts\\python.exe",
      "args": ["${env:ERL_MCP_HOME}\\run_local.py"],
      "env": {
        "ERL_PROJECTS_ROOT": "${workspaceFolder}\\.erl\\projects",
        "ERL_LOCAL_ALLOWED_ROOTS": "${workspaceFolder}"
      }
    }
  }
}
```

`${workspaceFolder}` 是当前 KPI 仓库。这样 MCP 只能 attach 当前仓库，并把运行证据保存在该仓库的 `.erl/projects`，不会默认读取其他本地目录。

保存后完全重启 Cursor。在 **Customize → MCPs** 确认 `evidence-research-loop-local` 已连接。也可以在 Cursor CLI 执行：

```text
agent mcp list
agent mcp list-tools evidence-research-loop-local
```

若连接失败，在 Windows 打开 Cursor Output 面板（`Ctrl+Shift+U`），选择 **MCP Logs** 查看启动错误。

## 2. 调用 Skill

Cursor 会自动发现本机 `~/.codex/skills/evidence-research-loop`。在 Agent 输入框键入 `/`，选择 `evidence-research-loop`；也可以使用 `@evidence-research-loop`。

如果 Customize → Skills 中没有看到它，将发布仓库里的 `evidence-research-loop` 文件夹复制到 KPI 仓库：

```text
<kpi-repo>/.cursor/skills/evidence-research-loop/
```

目录内必须保留 `SKILL.md`、`references`、`scripts` 和 `assets`。复制后重启 Cursor。

## 3. KPI 项目第一次提示词

先不要让 Agent 直接生成完整系统。第一条消息使用：

```text
/evidence-research-loop

使用 evidence-research-loop-local MCP 对当前 KPI 仓库建立工程方案进化项目。

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

继续当前 MCP 项目。先调用 validate_project；只有协议、基线、裁判和冻结输入都有效时才启动搜索。生成至少两个机制不同的可执行候选，使用预先声明的预算运行多代；完成后调用 audit_project 和 finalize_evidence。最后分别报告结论、基线、证据、失败和未验证项。
```

Cursor Agent 应使用如下 MCP 顺序：

```text
get_capabilities
→ attach_local_project 或 create_project/write_files
→ validate_project
→ start_search
→ get_status
→ resume_search（需要时）
→ audit_project
→ finalize_evidence
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

## 6. 本地与 Cloud Agent 的区别

上述 `stdio` 配置适用于 Cursor Desktop 和本机 Cursor CLI。Cursor Cloud Agent 不能直接运行你电脑上的本地 Python 路径；要使用 Cloud Agent，必须把 Skill 放进仓库或同步到 Cursor，并连接已部署的远程 MCP。远程 MCP 版本必须先单独核对，不能默认与本地 v2.6.1 相同。
