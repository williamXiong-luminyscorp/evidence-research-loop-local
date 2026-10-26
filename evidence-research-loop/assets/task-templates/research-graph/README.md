# Research Graph 适配器骨架

这个目录只定义通用 DAG 接口，不证明任何节点。复制后必须：

1. 把 `blueprint.json` 的 statement、依赖、scope 和 output contract 改成真实任务；
2. 为每类节点实现独立 verifier，固定命令、版本、资源和通过字段；
3. 用已知正确、错误、缺失和恶意 artifact 故障测试 verifier；
4. 初始化图后让不同 worker 原子领取节点，记录 Agent/模型/来源/费用；
5. goal verified 后另做根级集成、冻结 holdout 和审计。

默认 `verifier_adapter.py` 始终失败，避免把通用格式检查误当领域验收。
