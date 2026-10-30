# Azure deployment

Current verified deployment: 2026-10-30.

| Item | Value |
| --- | --- |
| Azure tenant | `6263c045-9d6c-40bc-922c-dbdd2c400fb8` |
| Subscription | `CSP Subscription 1` / `9069dbca-8580-437f-8cac-294d94584d02` |
| Resource group | `lmyrg` |
| Container Apps environment | `bc-diagnostics-env` |
| Container App | `evidence-research-loop-mcp` |
| MCP endpoint | `https://evidence-research-loop-mcp.nicetree-1d19d2fd.eastus.azurecontainerapps.io/mcp` |
| Revision | `evidence-research-loop-mcp--v265-20261030` |
| Locked image | `erlmcpacr.azurecr.io/evidence-research-loop-mcp@sha256:92a2867b40aa25cb7433726ca31a7e2693188846a1d0d415377d6574c4860e41` |
| Storage | `erlmcpdata20261019` / `erl-projects`, mounted at `/data` |
| Replica policy | min 1, max 1 |
| Server / engine | `1.2.4` / `evidence-research-loop-v2.6.5` |
| MCP tools | 40 |

The full machine-readable result is
[`deployment-record-v2.6.5.json`](deployment-record-v2.6.5.json). The prior
v2.5.1 record remains unchanged in
[`deployment-record-v2.5.1.json`](deployment-record-v2.5.1.json). The original
v2.4.2 record remains unchanged in [`deployment-record.json`](deployment-record.json);
its digest `sha256:3a06637f…54b4` is the known rollback binary. Intermediate
v2.5 images and failed remote feature projects are retained as diagnostic
evidence rather than rewritten as successes.

The bearer token is not stored in this repository. It remains an Azure
Container App secret named `mcp-auth-token` and a Windows user environment
variable named `EVIDENCE_RESEARCH_LOOP_MCP_TOKEN`.

## Verified outcomes

- Revision is `Healthy`, `Provisioned`, and receives 100% traffic.
- `/healthz` reports matching expected/observed engine hash for 80 skill files.
- MCP protocol `2026-07-28` exposes 40 tools; requests without the bearer token
  return HTTP 401.
- Release self-test run `20261030T215948Z-b1bb6291` passed 62/62 checks:
  50 engine, 9 server and 3 Composition MCP tests.
- Remote Research Graph project `azure-v265-feature-20261030a-graph` produced
  one verified asset and audit valid/accepted.
- Remote command-backed Scientist project
  `azure-v265-feature-20261030a-scientist` completed five stages with zero model
  calls and audit valid/accepted.
- After restarting the accepted revision, both new v2.6.5 projects and the old
  v2.5.1 Graph/Scientist projects were independently re-read and re-audited as
  valid/accepted.

These are deployment and evidence-plumbing checks. The deterministic Scientist
fixture does not establish novelty, model creativity, autonomous decomposition,
cross-domain generalization, or production suitability.

## Recheck

Run from the MCP server root. Every Azure command must explicitly use the ERL
subscription because the local CLI default belongs to a different tenant.

```powershell
$subscription = '9069dbca-8580-437f-8cac-294d94584d02'
$url = 'https://evidence-research-loop-mcp.nicetree-1d19d2fd.eastus.azurecontainerapps.io/mcp'
$token = [Environment]::GetEnvironmentVariable('EVIDENCE_RESEARCH_LOOP_MCP_TOKEN', 'User')

az containerapp revision show --subscription $subscription `
  --resource-group lmyrg --name evidence-research-loop-mcp `
  --revision evidence-research-loop-mcp--v265-20261030

.\.venv\Scripts\python.exe .\tests\smoke_http.py $url $token
.\.venv\Scripts\python.exe .\tests\smoke_persistence.py `
  $url $token azure-v251-e2e-20261026c
.\.venv\Scripts\python.exe .\tests\smoke_v25_feature_persistence.py `
  $url $token `
  azure-v265-feature-20261030a-graph `
  azure-v265-feature-20261030a-scientist
```

Use a new project ID if rerunning `smoke_e2e.py` or
`smoke_v25_features.py`; project IDs are immutable by design.
