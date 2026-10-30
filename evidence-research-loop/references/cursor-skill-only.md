# Codex / Cursor 本地 Skill 执行

Agent 读取本 Skill 后可以直接调用本目录 `scripts/`。这适合代码和 Codex 登录都在用户本机的项目，不要求额外启动 MCP。

同一套 Skill 放在两个目录，推到 GitHub 后两边都能用：

- Cursor：`.cursor/skills/evidence-research-loop/`
- Codex：`.codex/skills/evidence-research-loop/`

## 边界

- 同一项目不得同时由直接脚本和 MCP 两个写者运行；选定一个入口后保持到本轮结束。
- 用户明确要求 MCP、需要远程恢复或需要标准工具边界时，可以使用版本匹配的 ERL MCP；先核对版本与引擎哈希。
- 仅为直接脚本搜索不需要修改 `.cursor/mcp.json`。
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

长任务、候选较多或用户明确要求时，可维护 [进度 Canvas](progress-canvas.md)。Cursor 看板可写到工作区 `canvases/`；需要纳入项目记录时再写 `$project/progress.canvas.tsx`。

Research Graph、Composition Graph、Scientist Runtime 使用同一目录下的 `research_graph.py`、`composition_graph.py`、`scientist_runtime.py`。是否需要进度看板取决于任务复杂度和用户需要。

## AI 搜索

正式搜索使用 `generator.backend=codex`。控制器会检查 `codex login status`（stdout 或 stderr）。未登录时停止，并提示用户在私有终端登录。

`deterministic` 仅用于基础设施测试：任务必须声明 `generator.test_only: true`，且进程设置 `ERL_ALLOW_DETERMINISTIC_TEST_MODE=1`。

## 与 MCP 的关系

发布仓库同时包含 MCP 服务器源码。直接 Skill 路径不启动 MCP；MCP 路径把相同的项目、恢复和审计能力封装为工具。入口不同不改变证据要求，也不能把两个入口的并发写入当作协作。
