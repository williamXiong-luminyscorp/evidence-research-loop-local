import {
  Callout,
  Card,
  CardBody,
  CardHeader,
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

const WINNER = "c-0015-527caa1e";

const PATH = [
  { id: "c-0001-2c214633", short: "c-0001", gen: 0, role: "baseline", parent: null, score: 52.25 },
  { id: "c-0002-cb66967e", short: "c-0002", gen: 1, role: "MFA 特权", parent: "c-0001-2c214633", score: 64.25 },
  { id: "c-0003-82233867", short: "c-0003", gen: 1, role: "MFA 全员", parent: "c-0002-cb66967e", score: 68.25 },
  { id: "c-0005-4f1b6a4c", short: "c-0005", gen: 2, role: "限流 10/分", parent: "c-0003-82233867", score: 75.75 },
  { id: "c-0009-153b43c2", short: "c-0009", gen: 4, role: "会话 30 分", parent: "c-0005-4f1b6a4c", score: 92.0 },
  { id: "c-0011-89a76c13", short: "c-0011", gen: 5, role: "argon2id", parent: "c-0009-153b43c2", score: 96.0 },
  { id: WINNER, short: "c-0015", gen: 7, role: "限流 1/分", parent: "c-0011-89a76c13", score: 97.125 },
];

function WinnerPathDag() {
  const theme = useHostTheme();
  const layout = computeDAGLayout({
    nodes: PATH.map((c) => ({ id: c.id })),
    edges: PATH.filter((c) => c.parent).map((c) => ({ from: c.parent as string, to: c.id })),
    direction: "vertical",
    nodeWidth: 200,
    nodeHeight: 52,
    rankGap: 28,
    nodeGap: 24,
    padding: 8,
  });
  const byId = Object.fromEntries(PATH.map((c) => [c.id, c]));
  return (
    <svg width="100%" height={layout.height} viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label="赢家祖先谱系">
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
              width={200}
              height={52}
              rx={4}
              fill={selected ? theme.fill.tertiary : theme.bg.elevated}
              stroke={selected ? theme.accent.primary : theme.stroke.secondary}
            />
            <text x={node.x + 10} y={node.y + 20} fill={theme.text.primary} fontSize={12}>
              {item.short}  {item.score}
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

export default function LoginErlProgress() {
  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>Login 系统 ERL 进度</H1>
        <Text tone="secondary">
          v2.6.3 · search-cf3fb29edc58 · gpt-6.1-sol · 2026-10-28 21:56–22:01 UTC
        </Text>
        <Row gap={8} wrap>
          <Stat value="已完成" label="搜索状态" tone="success" />
          <Stat value="97.125" label="冻结赢家 security_score" tone="success" />
          <Stat value="52.25 → 97.125" label="相对基线" />
          <Stat value="20 / 20" label="本任务 Codex 调用" />
        </Row>
      </Stack>

      <Callout tone="warning" title="不是全局最优">
        20 次调用、本仿真裁判下实测最好。停止原因：代数预算用尽。holdout 与 dev 是同类登录风险场景、不同 seed。这不是网站实现，也不是渗透测试。
      </Callout>

      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>流程（用了哪些图）</CardHeader>
          <CardBody>
            <TodoListCard
              defaultExpanded
              todos={[
                { id: "lock", content: "冻结策略契约与仿真裁判", status: "completed" },
                { id: "base", content: "基线 bcrypt 策略：security 52.25，摩擦 0", status: "completed" },
                { id: "search", content: "10 代 × 2 候选，20 次 Codex 全部打完", status: "completed" },
                { id: "freeze", content: "冻结 c-0015，对照 baseline，holdout 通过", status: "completed" },
                { id: "tokens", content: "token 账本已写：input 387,638（含缓存 92,416）", status: "completed" },
              ]}
            />
            <Text tone="secondary" size="small">
              图：顶部数字、流程清单、预算条、步骤表、赢家祖先 DAG、每代最好分数折线、token 合计。21 个候选不全画进 DAG，只画赢家一条血缘。
            </Text>
          </CardBody>
        </Card>
        <Stack gap={10}>
          <H2>本任务调用预算</H2>
          <UsageBar total={20} segments={[{ id: "used", value: 20 }]} topLeftLabel="Codex 调用" topRightLabel="20 / 20 已用完" />
          <UsageBar total={7200} segments={[{ id: "wall", value: 299 }]} topLeftLabel="墙钟（秒）" topRightLabel="299 / 7200" />
          <Text tone="secondary" size="small">
            费用 unknown。secret_recorded=false。
          </Text>
        </Stack>
      </Grid>

      <Stack gap={8}>
        <H2>每代最好 security_score（越大越好）</H2>
        <Text tone="secondary" size="small">
          Source: generation_closed best_primary_value · login-system-v1 · 2026-10-28
        </Text>
        <LineChart
          categories={["基线", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10"]}
          series={[{ name: "该代最好 security_score", data: [52.25, 68.25, 75.75, 92.375, 92.375, 96.0, 96.0, 97.125, 97.125, 97.125, 97.125], tone: "success" }]}
          beginAtZero
          showValues
          height={240}
        />
      </Stack>

      <Stack gap={8}>
        <H2>赢家祖先谱系 DAG</H2>
        <Text tone="secondary" size="small">边 = parent_id。蓝框为冻结赢家。全库 21 个可行候选，0 个硬门禁失败。</Text>
        <WinnerPathDag />
      </Stack>

      <Stack gap={8}>
        <H2>Codex token 合计</H2>
        <Row gap={16} wrap>
          <Stat value="387,638" label="input_tokens" />
          <Stat value="92,416" label="cached_input_tokens" />
          <Stat value="5,653" label="output_tokens" />
          <Stat value="1,442" label="reasoning_output_tokens" />
        </Row>
        <Text tone="secondary" size="small">Source: demo/login-system-v1/codex-token-usage.json · 20 次调用都有用量 · 费用未知</Text>
      </Stack>

      <Stack gap={8}>
        <H2>引擎步骤</H2>
        <Table
          headers={["步骤", "谁执行", "结果"]}
          rows={[
            ["锁协议", "evolution_controller v2.6.3", "p-0001"],
            ["基线", "evaluator.py", "c-0001 通过，52.25"],
            ["提案×20", "codex gpt-6.1-sol", "全部 model_call=true"],
            ["评测", "8 个登录风险场景", "21 个硬通过，0 失败"],
            ["冻结+holdout", "同一裁判", "c-0015 vs baseline，均通过"],
          ]}
        />
      </Stack>

      <H3>冻结赢家策略 c-0015</H3>
      <Table
        headers={["字段", "值"]}
        rows={[
          ["password_store", "argon2id"],
          ["min_password_length", "10"],
          ["lockout", "10 次 / 15 分钟"],
          ["session_idle_minutes", "30"],
          ["cookies", "Secure + HttpOnly"],
          ["mfa", "totp_required_all"],
          ["rate_limit_per_minute", "1"],
          ["default_admin_password", "false"],
          ["reset_channel", "email_token_timeboxed"],
          ["transport", "https"],
          ["login_friction_index", "12.5（基线是 0）"],
        ]}
      />
      <Text>
        裁判按主指标只看 security_score，所以选了更严的每分钟 1 次限流。摩擦更高的方案（例如会话 15 分钟）分数略低，没有当赢家。Pareto 上还有更低摩擦、略低安全分的点。
      </Text>
    </Stack>
  );
}
