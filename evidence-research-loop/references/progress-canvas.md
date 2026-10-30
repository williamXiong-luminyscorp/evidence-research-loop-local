# 可选的搜索进度 Canvas

长时间或多候选搜索中，Canvas 可以帮助用户看到搜索走到哪、用了什么图、花了多少次模型调用。它是展示与沟通资产，不是搜索状态机、审计或接受判定的一部分。环境不支持 Canvas、任务很短或用户只要机器记录时，可以不生成。

参考已完成的看板（Skill 内布局样例）：

- Login：`assets/examples/login-erl-progress.canvas.tsx`
- Circle packing：`assets/examples/shinka-erl-progress.canvas.tsx`

## 生成时应做到

1. **开始搜索前**就建立 Canvas（契约和基线分数已有即可）。状态写成进行中，流程清单用 `TodoListCard`。
2. **第一次模型调用回来后**补谱系、分数、token；不要画空图。
3. **搜索结束后**用最终 `search-state.json`、`final-report.json`、`codex-token-usage.json` 更新同一块看板。
4. 同时写两份：
   - Cursor 可打开：工作区 `canvases/<task>-erl-progress.canvas.tsx`（只从 `cursor/canvas` 导入）
   - 可进 Git：任务目录 `progress.canvas.tsx`（内容相同）
5. 聊天里用 markdown 链接指向 Canvas 文件，方便用户点开。
6. 只记用量（input/output/cached/reasoning tokens）。**禁止**把 access token、API key 画进看板或写入仓库。

## 建议出现的图（有数据才画）

| 图 | 组件 | 作用 |
|---|---|---|
| 顶部数字 | `Stat` | 状态、主指标、相对基线、本任务已用调用 / 预算 |
| 流程清单 | `TodoListCard` | 锁契约、基线、各代、冻结、holdout、token 账本 |
| 预算条 | `UsageBar` | 本任务调用次数、墙钟 |
| 引擎步骤 | `Table` | 谁执行、输入、输出 |
| 谱系 DAG | `computeDAGLayout` + SVG | 父 → 子；候选很多时只画赢家祖先链 |
| 分数折线 | `LineChart` | 每代最好的主指标，轴和来源写清楚 |
| Token | `Stat` 或 `BarChart` | 各次或合计 input/output/reasoning；调用很多时用合计 |

不要用 emoji 当图标。颜色只用 `useHostTheme()` token。结论必须写清：这是**已声明预算和裁判下实测最好**，不是全局最优。

## 调用次数怎么写

`max_generator_calls` 是**这一次任务目录**的上限，不是全账号、也不是每一次点击。看板标题写成「本任务 12 / 20」，不要写成「全局」。
