# 设计影响、开源状态与复用边界

Evidence Research Loop 是独立实现，不是下列项目的官方 fork。这里区分“借鉴公开方法”“通过外部接口使用”和“复制源代码”；三者的许可义务不同。

## Prove2Me

- 论文：[Prove2Me: An Open Collaborative Platform for Scaling Math Formalization](https://arxiv.org/abs/2608.28433)
- 公开 workspace：[prove2me/prove2me_workspace](https://github.com/prove2me/prove2me_workspace)
- 本次核查固定 commit：`7368165454e59bb2c2769595eecf06f82cc87b0e`
- 许可证状态：该 workspace 在本次核查时没有 `LICENSE`，GitHub 也未识别许可证。

公开可见、可 clone 或 README 自称 open source，不等于获得复制、修改或再发布源码的许可。因此 ERL 只根据论文公开描述 clean-room 实现以下通用思想：不可变 statement/artifact 分离、依赖图、审计核心、已验证资产复用。没有复制 workspace 的 Skill、脚本或文档。若未来要包含其代码，必须先取得明确许可证或书面许可，并在发布物中登记来源。

`composition_graph.py` 将主张与提交分开，增加带实际验证记录的条件组合、替代路径和工程资产复验。AND-OR 是对论文多证明/多前提行为的实现建模，不是声称复用了官方服务端算法。它接受工程经验验证，不提供 Lean 内核、官方平台、自动 mission 人审或完整 Prove2Me 复现；操作与边界见 [Composition Graph](composition-graph.md)。

Prove2Me 的 [Formalpedia](https://github.com/prove2me/formalpedia) 是另一个 Apache-2.0 仓库；本次核查 commit 为 `082b32d5d4099d88e510efc822346b699a9584fa`。ERL 当前没有打包它。若未来把它作为数学资产源，必须固定 commit、Lean/Mathlib 环境、原证明者与许可证，并保持为可追溯外部资产。

## Goedel-Architect

- 论文：[Goedel-Architect: Streamlining Formal Theorem Proving with Blueprint Generation and Refinement](https://arxiv.org/abs/2606.06468)
- 官方仓库：[goedelarchitect/goedel-architect](https://github.com/goedelarchitect/goedel-architect)
- 本次核查固定 commit：`300d69e97c91a2a498091c898e736af69f391960`
- 许可证：Apache License 2.0。

ERL Research Graph 借鉴其公开的 blueprint DAG、节点级并行证明、结构化失败诊断和失败驱动 refinement，但当前 `research_graph.py` 是通用领域的 clean-room 实现，没有复制 Goedel-Architect 源码。

若未来复制或修改上游文件，发布者必须：

1. 保留 Apache-2.0 许可证和上游版权/归属；
2. 在修改文件中显著说明已修改；
3. 固定所用 commit，并在第三方清单中记录文件来源；
4. 若上游后来出现 `NOTICE`，按许可证要求传递相关 notice；
5. 不把上游商标或项目名称写成官方背书。

## 本项目中的对应关系

| 外部思想 | ERL 独立实现 | 不能由此推出 |
| --- | --- | --- |
| theorem/lemma 或任务依赖图 | 通用 Research Graph node/dependency/output contract | ERL 已形式化某个数学定理 |
| 多 Agent 领取节点 | 原子 claim、lease、heartbeat、attempt | 图初始化后 Agent 一定能找到正确答案 |
| 失败推动蓝图重构 | versioned split/rewire/supersede/invalidate | 任意自然语言失败都能自动得到正确分解 |
| 已验证 lemma 复用 | 内容寻址 asset、contract hash、scope 检查 | 相似自然语言节点可以无条件复用 |
| 协作贡献记录 | node-level contribution/event 哈希链 | 自动决定论文署名、IP、绩效或信用权重 |

## 发布检查

每次发布前检查：第三方文件是否新增、许可证是否改变、固定 commit 是否更新、文档中是否把“设计影响”误写成“包含上游实现”。如果没有复制源代码，就使用“informed by / 受启发”并链接论文；如果复制了，必须进入第三方许可证与文件级修改清单。
