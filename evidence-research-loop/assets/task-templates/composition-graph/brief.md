# 组合图基础设施示例

这是确定性接口冒烟模板，已提供已知正确的配置夹具，不是模型搜索、科研成果或未见数据泛化实验。没有优化分数。

目标是把 `scale: x -> 2*x` 和 `offset: x -> x+3` 组合成 `2*x+3`。两个模块各自通过后，还要验证组合顺序；反向组合会得到 `2*x+6`。实际验收从引擎绑定的子产物读取配置，不能只读组件名称或成功标记。

初始化使用 `composition-blueprint.json`。先提交 `submit-reduction.json`，此时只能条件通过；分别提交 `submit-scale.json` 和 `submit-offset.json`，再使用返回的 evidence_id 建立 validate 请求，才能执行实际组合、集成和 holdout。

```json
{
  "kind": "validate",
  "reduction_id": "本次 reduction 返回的 evidence_id",
  "bindings": {
    "scale": "本次 scale 返回的 evidence_id",
    "offset": "本次 offset 返回的 evidence_id"
  }
}
```

命令和状态说明见 skill 的 `references/composition-graph.md`。检查 `audit` 输出中的 `valid` 与 `accepted`，不能只看进程退出码。

用于实际项目时，替换这里的目标、候选、验证器和数据；先冻结契约，再开始搜索。这里的 holdout 是固定具名检查，反复运行后仍是回归数据，不提供独立泛化证据。模板不包含 Refutation 验证器或人工语义审核，因此不能据此声称这些流程已完成。
