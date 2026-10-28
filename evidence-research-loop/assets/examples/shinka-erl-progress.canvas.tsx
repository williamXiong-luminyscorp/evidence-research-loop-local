import {
  BarChart,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Code,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  LineChart,
  Row,
  Stack,
  Stat,
  Table,
  Text,
  TodoListCard,
  UsageBar,
  computeDAGLayout,
  useHostTheme,
} from "cursor/canvas";

const SEARCH_ID = "search-cb112aebc5ab";
const MODEL = "gpt-6.1-sol";
const WINNER = "c-0005-9c57220f";
const BASELINE = "c-0001-10ba405a";

const CANDIDATES = [
  { id: BASELINE, short: "c-0001", gen: 0, role: "baseline", parent: null, score: 0.9598, status: "对照" },
  { id: "c-0002-7bd3b919", short: "c-0002", gen: 1, role: "conservative", parent: BASELINE, score: 2.5414, status: "可行" },
  { id: "c-0003-1257051b", short: "c-0003", gen: 1, role: "mechanism", parent: BASELINE, score: 2.5414, status: "可行·同行为" },
  { id: "c-0004-c0cdb962", short: "c-0004", gen: 2, role: "conservative", parent: "c-0002-7bd3b919", score: 2.5439, status: "精英" },
  { id: WINNER, short: "c-0005", gen: 2, role: "mechanism", parent: "c-0003-1257051b", score: 2.6262, status: "冻结赢家" },
];

const TOKEN_CALLS = [
  { call: "g001-s001", gen: 1, role: "conservative", candidate: "c-0002", input: 20238, output: 872, reasoning: 210, sec: 29.0, score: 2.5414 },
  { call: "g001-s002", gen: 1, role: "mechanism", candidate: "c-0003", input: 17260, output: 2050, reasoning: 316, sec: 45.4, score: 2.5414 },
  { call: "g002-s001", gen: 2, role: "conservative", candidate: "c-0004", input: 20893, output: 2272, reasoning: 822, sec: 45.0, score: 2.5439 },
  { call: "g002-s002", gen: 2, role: "mechanism", candidate: "c-0005", input: 18925, output: 2704, reasoning: 801, sec: 71.4, score: 2.6262 },
];

const TOKEN_TOTAL = {
  input: 77316,
  output: 7898,
  reasoning: 2149,
  cached: 0,
  calls: 4,
  cost: "unknown",
};

function LineageDag() {
  const theme = useHostTheme();
  const layout = computeDAGLayout({
    nodes: CANDIDATES.map((c) => ({ id: c.id })),
    edges: CANDIDATES.filter((c) => c.parent).map((c) => ({ from: c.parent as string, to: c.id })),
    direction: "vertical",
    nodeWidth: 168,
    nodeHeight: 58,
    rankGap: 56,
    nodeGap: 28,
    padding: 12,
  });
  const byId = Object.fromEntries(CANDIDATES.map((c) => [c.id, c]));
  return (
    <svg width="100%" height={layout.height} viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label="候选父子关系">
      {layout.edges.map((edge) => (
        <line
          key={`${edge.from}-${edge.to}`}
          x1={edge.sourceX}
          y1={edge.sourceY}
          x2={edge.targetX}
          y2={edge.targetY}
          stroke={theme.stroke.primary}
          strokeWidth={1}
        />
      ))}
      {layout.nodes.map((node) => {
        const item = byId[node.id];
        const selected = node.id === WINNER;
        return (
          <g key={node.id}>
            <rect
              x={node.x}
              y={node.y}
              width={168}
              height={58}
              rx={4}
              fill={selected ? theme.fill.tertiary : theme.bg.elevated}
              stroke={selected ? theme.accent.primary : theme.stroke.secondary}
            />
            <text x={node.x + 10} y={node.y + 20} fill={theme.text.primary} fontSize={12}>
              {item.short}  {item.score.toFixed(4)}
            </text>
            <text x={node.x + 10} y={node.y + 38} fill={theme.text.secondary} fontSize={11}>
              G{item.gen} · {item.role}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default function ShinkaErlProgress() {
  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>ShinkaEvolve 内核复现进度</H1>
        <Text tone="secondary">
          ERL Skill-only · Circle Packing n=26 · search {SEARCH_ID} · 2026-10-28 18:31–18:34 UTC
        </Text>
        <Row gap={8} wrap>
          <Stat value="已完成" label="搜索状态" tone="success" />
          <Stat value="2.626" label="冻结赢家半径和（dev=holdout）" tone="success" />
          <Stat value="+1.666" label="相对基线 0.960 的提升" />
          <Stat value="4 / 4" label="Codex 调用（gpt-6.1-sol）" />
        </Row>
      </Stack>

      <Callout tone="info" title="这是什么，什么不是">
        用当前 Evidence Research Loop 引擎复现 ShinkaEvolve 的第一题：在单位正方形里装 26 个圆，最大化半径和。
        已跑通种群、冻结裁判、档案、失败驱动迭代、冻结后 holdout。没有复现岛屿迁移、UCB1 多模型、嵌入去重或论文 150/200 代规模；不得称为 Shinka 论文 SOTA。holdout 是同一几何约束再测，不是新的装箱实例。
      </Callout>

      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>流程到哪一步</CardHeader>
          <CardBody>
            <TodoListCard
              defaultExpanded
              todos={[
                { id: "lock", content: "冻结契约与几何裁判（n=26，单位正方形，无重叠）", status: "completed" },
                { id: "base", content: "评测官方种子 baseline：半径和 0.960，硬门禁通过", status: "completed" },
                { id: "g1", content: "第 1 代 Codex：2 个候选，网格构造把分数抬到 2.541", status: "completed" },
                { id: "g2", content: "第 2 代 Codex：局部 refinement + NumPy 优化，赢家 2.626", status: "completed" },
                { id: "freeze", content: "冻结 c-0005，对照 baseline，holdout 两次均硬通过", status: "completed" },
                { id: "tokens", content: "每次 Codex 调用落盘 token 账本（不含 access token）", status: "completed" },
              ]}
            />
          </CardBody>
        </Card>
        <Stack gap={10}>
          <H2>调用预算</H2>
          <UsageBar
            total={4}
            segments={[{ id: "used", value: 4 }]}
            topLeftLabel="模型调用"
            topRightLabel="4 / 4 已用完"
          />
          <UsageBar
            total={1200}
            segments={[{ id: "wall", value: 202 }]}
            topLeftLabel="墙钟（秒）"
            topRightLabel="202 / 1200"
          />
          <Text tone="secondary" size="small">
            停止原因 generation_budget_exhausted。费用未知：task.json 未声明 pricing_usd_per_million_tokens，账本记 cost_usd=null。Access token 未写入任何文件。
          </Text>
        </Stack>
      </Grid>

      <Stack gap={8}>
        <H2>引擎怎么走</H2>
        <Text tone="secondary">
          生成器只看到 brief、父代代码和开发反馈；裁判与 holdout 不进 workspace。Codex 提案 → 独立 evaluator.py 几何检查 → 通过才进档案。
        </Text>
        <Table
          headers={["步骤", "谁执行", "输入", "输出"]}
          rows={[
            ["1 锁协议", "evolution_controller", "task.json + evaluator.py", "protocol p-0001"],
            ["2 基线", "冻结裁判", "baseline.py", "c-0001 分数 0.960"],
            ["3 提案", "codex exec gpt-6.1-sol", "父代 + brief", "完整 candidate.py"],
            ["4 评测", "evaluator.py", "run_packing()", "correct.json + combined_score"],
            ["5 档案", "控制器", "硬门禁通过者", "精英 / Pareto / 行为相似组"],
            ["6 冻结+holdout", "同一裁判", "c-0005 与 baseline", "两边 hard_pass=true"],
          ]}
        />
      </Stack>

      <Stack gap={8}>
        <H2>候选谱系（父 → 子）</H2>
        <Text tone="secondary" size="small">
          边表示 parent_id。c-0002 与 c-0003 行为签名相同（同网格构造），控制器记入 behavior_similar_groups，第 2 代分别从它们突变。
        </Text>
        <LineageDag />
      </Stack>

      <Grid columns={2} gap={16}>
        <Stack gap={8}>
          <H2>半径和（越大越好）</H2>
          <Text tone="secondary" size="small">
            Source: demo/shinka-circle-packing-v1 冻结 dev 指标 · 2026-10-28
          </Text>
          <LineChart
            categories={["基线", "第1代最好", "第2代最好"]}
            series={[{ name: "combined_score（半径和）", data: [0.96, 2.541, 2.626], tone: "success" }]}
            beginAtZero
            showValues
            height={220}
          />
        </Stack>
        <Stack gap={8}>
          <H2>每次 Codex 的 token</H2>
          <Text tone="secondary" size="small">
            Source: Codex turn.completed usage · 账本 codex-token-usage.jsonl · 不含密钥
          </Text>
          <BarChart
            categories={["g001-s001", "g001-s002", "g002-s001", "g002-s002"]}
            series={[
              { name: "input_tokens", data: [20238, 17260, 20893, 18925] },
              { name: "output_tokens", data: [872, 2050, 2272, 2704] },
              { name: "reasoning_output_tokens", data: [210, 316, 822, 801] },
            ]}
            stacked
            height={220}
          />
        </Stack>
      </Grid>

      <Stack gap={8}>
        <H2>Codex 调用账本</H2>
        <Row gap={16}>
          <Stat value={TOKEN_TOTAL.input.toLocaleString()} label="input_tokens 合计" />
          <Stat value={TOKEN_TOTAL.output.toLocaleString()} label="output_tokens 合计" />
          <Stat value={TOKEN_TOTAL.reasoning.toLocaleString()} label="reasoning_output_tokens 合计" />
          <Stat value="未知" label="USD 费用" tone="warning" />
        </Row>
        <Table
          striped
          headers={["调用", "代", "角色", "候选", "input", "output", "reasoning", "秒", "半径和"]}
          columnAlign={["left", "right", "left", "left", "right", "right", "right", "right", "right"]}
          rows={TOKEN_CALLS.map((row) => [
            row.call,
            String(row.gen),
            row.role,
            row.candidate,
            row.input.toLocaleString(),
            row.output.toLocaleString(),
            String(row.reasoning),
            row.sec.toFixed(1),
            row.score.toFixed(4),
          ])}
          rowTone={[undefined, undefined, undefined, "success"]}
        />
        <Text tone="secondary" size="small">
          认证方式：本机 <Code>codex login</Code>。账本字段 secret_recorded=false。缓存 token 本次全为 0。
        </Text>
      </Stack>

      <Divider />
      <H3>赢家机制（c-0005）</H3>
      <Text>
        父代 c-0003 依赖可选 SciPy，本机没有就退回网格。第 2 代改成纯 NumPy 的 augmented Lagrangian + Adam，保留可行网格兜底，18 秒搜索预算。dev 与 holdout 半径和均为 2.6261647756111874。
      </Text>
      <Text tone="secondary" size="small">
        证据：final-report.json、search-events.jsonl、codex-token-usage.json、artifacts/c-0005-9c57220f.py。模型 {MODEL}。
      </Text>
    </Stack>
  );
}
