from __future__ import annotations

"""Remote v2.5 feature smoke for Research Graph and Scientist Runtime.

This is a transport/deployment test, not a scientific result.  It uploads a
one-node frozen graph and a five-stage deterministic Scientist fixture, runs
both through the public MCP surface, and requires independent audits to accept
the recorded evidence.
"""

import argparse
import asyncio
import json
import textwrap
import time

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


GRAPH_VERIFIER = r'''
import json
import pathlib
import sys

artifact = pathlib.Path(sys.argv[1])
result_dir = pathlib.Path(sys.argv[2])
result_dir.mkdir(parents=True, exist_ok=True)
passed = artifact.read_text(encoding="utf-8").strip() == "verified"
(result_dir / "result.json").write_text(
    json.dumps({"hard_pass": passed, "classification": "verified" if passed else "wrong"}),
    encoding="utf-8",
)
'''


SCIENTIST_WORKER = r'''
import json
import pathlib
import sys

stage, request_path, response_path, attempt_dir_text = sys.argv[1:]
attempt_dir = pathlib.Path(attempt_dir_text)
json.loads(pathlib.Path(request_path).read_text(encoding="utf-8"))
usage = {
    "model": "deterministic-remote-smoke",
    "model_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "known_cost_usd": 0.0,
}
if stage == "ideation":
    response = {
        "stage": stage,
        "ideas": [{
            "idea_id": "idea-smoke",
            "hypothesis": "the recorded fixture delta is positive",
            "falsifiable_prediction": "delta is greater than zero",
            "minimum_experiment": "record and inspect the fixture delta",
        }],
        "usage": usage,
    }
elif stage == "literature":
    response = {
        "stage": stage,
        "sources": [{
            "source_id": "source-smoke",
            "title": "Deployment smoke source",
            "locator": "urn:erl:remote-smoke",
            "source_type": "test-fixture",
            "supports": ["idea-smoke"],
            "refutes": [],
        }],
        "novelty_assessment": {"status": "unknown", "reason": "transport smoke only"},
        "usage": usage,
    }
elif stage == "experiment":
    evidence = attempt_dir / "raw-observations.json"
    evidence.write_text(json.dumps({"delta": 1.0}), encoding="utf-8")
    response = {
        "stage": stage,
        "experiments": [{
            "experiment_id": "exp-smoke",
            "hypothesis_id": "idea-smoke",
            "status": "passed",
            "metrics": {"delta": 1.0},
            "evidence_files": [evidence.name],
        }],
        "usage": usage,
    }
elif stage == "analysis":
    response = {
        "stage": stage,
        "claims": [{
            "claim_id": "claim-smoke",
            "claim_type": "empirical",
            "statement": "the recorded fixture delta is positive",
            "status": "supported",
            "evidence_ids": ["exp-smoke"],
        }],
        "usage": usage,
    }
elif stage == "review":
    response = {
        "stage": stage,
        "decision": "accept",
        "claim_reviews": [{
            "claim_id": "claim-smoke",
            "decision": "accept",
            "reason": "the raw fixture records delta=1.0",
        }],
        "usage": usage,
    }
else:
    raise SystemExit(2)
pathlib.Path(response_path).write_text(json.dumps(response), encoding="utf-8")
'''


def graph_blueprint() -> dict:
    return {
        "schema_version": 1,
        "graph_id": "remote-v25-smoke-graph",
        "title": "Remote v2.5 Research Graph transport smoke",
        "goal_node_ids": ["goal"],
        "nodes": [{
            "id": "goal",
            "type": "deployment-smoke",
            "statement": "the submitted artifact contains the frozen smoke token",
            "dependencies": [],
            "scope": "deployment-smoke",
            "priority": 1,
            "max_attempts": 1,
            "output_contract": {"artifact_type": "text", "schema_version": 1},
            "verifier": {
                "id": "remote-smoke-verifier-v1",
                "command": ["python", "{project}/graph_verifier.py", "{artifact}", "{result_dir}"],
                "frozen_files": ["graph_verifier.py"],
                "timeout_sec": 30,
                "result_file": "result.json",
                "pass_field": "hard_pass",
            },
        }],
    }


def scientist_plan() -> dict:
    command = [
        "python", "{project}/scientist_worker.py", "{stage}",
        "{request}", "{response}", "{attempt_dir}",
    ]
    stages = {
        name: {
            "enabled": True,
            "instructions": f"Execute the deterministic deployment smoke for {name}.",
            "max_attempts": 1,
            "backend": {
                "kind": "command",
                "command": command,
                "timeout_sec": 30,
                "may_use_model": False,
                "env_allowlist": [],
            },
        }
        for name in ("ideation", "literature", "experiment", "analysis", "review")
    }
    return {
        "schema_version": 1,
        "plan_version": "remote-v25-smoke-v1",
        "research_question": "Does the deterministic deployment fixture record a positive delta?",
        "claim_boundary": "Only the uploaded deployment fixture and its recorded evidence.",
        "frozen_input_files": ["research-context.md"],
        "budget": {
            "max_stage_calls": 5,
            "max_wall_time_sec": 120,
            "max_known_cost_usd": 0.01,
        },
        "acceptance": {"required_claim_ids": ["claim-smoke"]},
        "stages": stages,
    }


async def call(client: Client, tool: str, arguments: dict) -> dict:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        raise RuntimeError(f"{tool} failed: {result.content}")
    if not isinstance(result.structured_content, dict):
        raise RuntimeError(f"{tool} returned no structured object")
    return result.structured_content


async def run(url: str, token: str, prefix: str) -> dict:
    graph_project = f"{prefix}-graph"
    scientist_project = f"{prefix}-scientist"
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(180.0)
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=180) as client:
            await call(client, "create_project", {
                "project_id": graph_project, "template": "research-graph"
            })
            await call(client, "write_files", {
                "project_id": graph_project,
                "overwrite": True,
                "files": [
                    {"path": "research-blueprint.json", "content": json.dumps(graph_blueprint())},
                    {"path": "graph_verifier.py", "content": textwrap.dedent(GRAPH_VERIFIER)},
                ],
            })
            initialized = await call(client, "initialize_research_graph", {
                "project_id": graph_project,
                "blueprint_path": "research-blueprint.json",
            })
            if not initialized.get("initialized"):
                raise RuntimeError(f"graph init rejected: {initialized}")
            claimed = await call(client, "claim_research_node", {
                "project_id": graph_project, "worker_id": "remote-smoke-agent"
            })
            claim = claimed.get("result") or {}
            submitted = await call(client, "submit_research_node", {
                "project_id": graph_project,
                "node_id": claim["node_id"],
                "worker_id": "remote-smoke-agent",
                "lease_token": claim["lease"]["token"],
                "contributor_kind": "tool",
                "contributor_id": "smoke_v25_features.py",
                "artifact_content": "verified\n",
                "roles": ["deployment-smoke"],
                "source_ids": ["remote-smoke-fixture"],
                "cost_usd": 0.0,
            })
            graph_audit = await call(client, "audit_research_graph", {
                "project_id": graph_project
            })
            if not submitted.get("verified") or not graph_audit["result"].get("valid") \
                    or not graph_audit["result"].get("accepted"):
                raise RuntimeError(
                    f"graph smoke was not accepted; submit={submitted}; audit={graph_audit}"
                )

            await call(client, "create_project", {
                "project_id": scientist_project, "template": "scientist-runtime"
            })
            await call(client, "write_files", {
                "project_id": scientist_project,
                "overwrite": True,
                "files": [
                    {"path": "research-plan.json", "content": json.dumps(scientist_plan())},
                    {"path": "research-context.md", "content": "Deterministic remote transport smoke.\n"},
                    {"path": "scientist_worker.py", "content": textwrap.dedent(SCIENTIST_WORKER)},
                ],
            })
            validated = await call(client, "validate_scientist_project", {
                "project_id": scientist_project
            })
            if not validated.get("valid"):
                raise RuntimeError(f"scientist plan rejected: {validated}")
            started = await call(client, "start_scientist_study", {
                "project_id": scientist_project
            })
            job_id = started["job"]["job_id"]
            deadline = time.monotonic() + 120
            status = {}
            while time.monotonic() < deadline:
                status = await call(client, "get_scientist_status", {
                    "project_id": scientist_project, "job_id": job_id
                })
                if status.get("status") == "finished":
                    break
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError(f"scientist smoke did not finish: {status}")
            if (status.get("result") or {}).get("exit_code") != 0:
                raise RuntimeError(f"scientist worker failed: {status}")
            scientist_audit = await call(client, "audit_scientist_project", {
                "project_id": scientist_project
            })
            scientist_export = await call(client, "export_scientist_report", {
                "project_id": scientist_project
            })
            audit_result = scientist_audit.get("result") or {}
            if not audit_result.get("valid") or not audit_result.get("accepted"):
                raise RuntimeError(f"scientist smoke was not accepted: {scientist_audit}")

            return {
                "schema_version": 1,
                "purpose": "remote deployment transport smoke; not scientific evidence",
                "protocol_version": client.protocol_version,
                "graph": {
                    "project_id": graph_project,
                    "valid": graph_audit["result"]["valid"],
                    "accepted": graph_audit["result"]["accepted"],
                    "event_count": graph_audit["result"]["event_count"],
                    "asset_count": graph_audit["result"]["asset_count"],
                },
                "scientist": {
                    "project_id": scientist_project,
                    "job_id": job_id,
                    "valid": audit_result["valid"],
                    "accepted": audit_result["accepted"],
                    "execution_status": audit_result["execution_status"],
                    "event_count": audit_result["event_count"],
                    "model_calls": 0,
                    "known_cost_usd": 0.0,
                    "exported": scientist_export.get("exit_code") == 0,
                },
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("token")
    parser.add_argument("project_prefix")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.url, args.token, args.project_prefix)), indent=2))


if __name__ == "__main__":
    main()
