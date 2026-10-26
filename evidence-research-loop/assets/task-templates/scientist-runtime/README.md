# Scientist Runtime 适配器骨架

这个目录不是预制科研答案。它提供 `research-plan.json` 与五阶段 command 的接口骨架，帮助你把真实模型、检索器、实验脚本、统计分析和独立评审接入 `scientist_runtime.py`。

开始前：

1. 把 `research-context.md` 换成真实问题、数据/代码版本和主张边界；
2. 实现 `stage_adapter.py` 的五个 stage，并保存来源 locator、实验原始文件和 usage/cost；
3. 在 `research-plan.json` 中固定预算、required claim、每个 command 和 timeout；
4. 先运行 `validate`，再报告模型和最大费用，最后运行 `start`；
5. 完成后分别查看执行状态、`audit.valid` 和 `acceptance.accepted`。

默认 adapter 会明确失败，不会生成貌似成功的假数据。它的 request/response 字段说明见 `references/scientist-mode.md`。
