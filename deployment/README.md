# Azure deployment

Current verified deployment: 2026-10-26.

| Item | Value |
| --- | --- |
| Azure tenant | `6263c045-9d6c-40bc-922c-dbdd2c400fb8` |
| Subscription | `CSP Subscription 1` / `9069dbca-8580-437f-8cac-294d94584d02` |
| Resource group | `lmyrg` |
| Container Apps environment | `bc-diagnostics-env` |
| Container App | `evidence-research-loop-mcp` |
| MCP endpoint | `https://evidence-research-loop-mcp.nicetree-1d19d2fd.eastus.azurecontainerapps.io/mcp` |
| Revision | `evidence-research-loop-mcp--v251-20261026` |
| Locked image | `erlmcpacr.azurecr.io/evidence-research-loop-mcp@sha256:065257eba239f1a3a540d46d2dbbb21ac00e11a68dc2cb7e6a43d74f14f34d5f` |
| Storage | `erlmcpdata20261019` / `erl-projects`, mounted at `/data` |
| Replica policy | min 1, max 1 |
| Server / engine | `1.1.1` / `evidence-research-loop-v2.5.1` |
| MCP tools | 29 |

The full machine-readable result is
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
- `/healthz` reports matching expected/observed engine hash for 63 skill files.
- MCP protocol `2026-07-28` exposes 29 tools; requests without the bearer token
  return HTTP 401.
- Outer deterministic project `azure-v251-e2e-20261026c` completed with audit
  valid/accepted and a verified evidence manifest.
- A real remote Research Graph submission produced one verified asset and audit
  valid/accepted.
- A real remote command-backed Scientist Runtime completed five stages with
  zero model calls and audit valid/accepted.
- After restarting the accepted revision, the outer 148-file manifest and both
  feature projects were independently re-read/re-audited. Manifest SHA-256:
  `cbd6522d9fddaa8840860407c44471dc4158714e442d159392ac395083ff48f7`.

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
  --revision evidence-research-loop-mcp--v251-20261026

.\.venv\Scripts\python.exe .\tests\smoke_http.py $url $token
.\.venv\Scripts\python.exe .\tests\smoke_persistence.py `
  $url $token azure-v251-e2e-20261026c
.\.venv\Scripts\python.exe .\tests\smoke_v25_feature_persistence.py `
  $url $token `
  azure-v251-feature-20261026c-graph `
  azure-v251-feature-20261026c-scientist
```

Use a new project ID if rerunning `smoke_e2e.py` or
`smoke_v25_features.py`; project IDs are immutable by design.
