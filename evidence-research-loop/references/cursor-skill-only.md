# Codex / Cursor Skill-only 执行

Agent 读取本 Skill 后直接调用本目录 `scripts/`，不经过 MCP。这与最初的 Codex Evidence Research Loop 用法相同。

同一套 Skill 放在两个目录，推到 GitHub 后两边都能用：

- Cursor：`.cursor/skills/evidence-research-loop/`
- Codex：`.codex/skills/evidence-research-loop/`

## 禁止

- 调用 ERL MCP 工具（`create_project`、`start_search`、`validate_project` 等）
- 为了跑搜索而重新填写 `.cursor/mcp.json`
- 把 Codex Token 或 API Key 写入任务文件、聊天记录或仓库

## 本机命令

`$skill` 指本 Skill 根目录。`$project` 指任务目录。

```powershell
python $skill\scripts\evolution_controller.py scaffold python-code $project
python $skill\scripts\evolution_controller.py validate $project
python $skill\scripts\evolution_controller.py start $project
python $skill\scripts\evolution_controller.py status $project
python $skill\scripts\evolution_controller.py resume $project
python $skill\scripts\evolution_controller.py export $project
python $skill\scripts\audit_search.py $project --verify-manifest
```

每次 `start` / `resume` / `export` 都维护进度 Canvas，见 [进度 Canvas](progress-canvas.md)。Cursor 看板写到工作区 `canvases/`；Git 副本写到 `$project/progress.canvas.tsx`。

Research Graph、Composition Graph、Scientist Runtime 使用同一目录下的 `research_graph.py`、`composition_graph.py`、`scientist_runtime.py`。同样要有进度看板。

## AI 搜索

正式搜索使用 `generator.backend=codex`。控制器会检查 `codex login status`（stdout 或 stderr）。未登录时停止，并提示用户在私有终端登录。

`deterministic` 仅用于基础设施测试：任务必须声明 `generator.test_only: true`，且进程设置 `ERL_ALLOW_DETERMINISTIC_TEST_MODE=1`。

## 与 MCP 的关系

发布仓库仍可包含 MCP 服务器源码。Skill-only 路径不启动它。证据目录、事件链、冻结和审计仍由 Skill 脚本产生。
