# Scientist Runtime 适配器骨架

这个目录不是预制科研答案。它提供 `research-plan.json` 与五阶段 command 的接口骨架，帮助你把真实模型、检索器、实验脚本、统计分析和独立评审接入 `scientist_runtime.py`。

开始前：

1. 把 `research-context.md` 换成真实问题、数据/代码版本和主张边界；
2. 实现 `stage_adapter.py` 的五个 stage，并保存来源 locator、实验原始文件和 usage/cost；
3. 在 `research-plan.json` 中固定预算、required claim、每个 command 和 timeout；
4. 先运行 `validate`，再报告模型和最大费用，最后运行 `start`；
5. 完成后分别查看执行状态、`audit.valid` 和 `acceptance.accepted`。

默认 adapter 会明确失败，不会生成貌似成功的假数据。它的 request/response 字段说明见 `references/scientist-mode.md`。

## 可选 Codex 多视角适配器

`codex_panel_adapter.py` 是一个需要显式配置的参考实现，不会由模板默认启用。使用时：

1. 把 `panel-config.json` 加入 `research-plan.json.frozen_input_files`；
2. 把模型名、绝对调用上限、各阶段角色和说明改成真实预算；
3. 将 stage command 从 `stage_adapter.py` 改为 `codex_panel_adapter.py`；
4. 需要不同输出字段时，为每个 stage 提供冻结的严格 JSON Schema，并在
   `panel-config.json` 中设置 `schema_file`；
5. 启动前向用户报告模型、最大调用数、单次/总时限和已知费用；价格未知时记录
   `null`，不能写成零。

适配器会逐个执行独立 panel role，保存每次 prompt、事件、stderr 和响应，再用一次
synthesis 调用形成 stage 输出。调用总数按 `角色数 + 1` 记录，并在启动每个阶段前核对
`absolute_max_model_calls`。多角色一致不等于证据；综合器必须按来源质量处理分歧。

冻结输入只允许 UTF-8 文本，并受 `max_frozen_input_bytes` 限制。默认运行仍是本机
Codex `read-only` sandbox，不等于完整网络或文件系统隔离。敏感数据发送到外部模型前
必须获得对应任务的明确授权。
