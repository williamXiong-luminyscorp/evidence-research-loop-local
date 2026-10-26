from __future__ import annotations

"""Re-audit existing Research Graph and Scientist projects after a service restart."""

import argparse
import asyncio
import json

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def call(client: Client, tool: str, arguments: dict) -> dict:
    result = await client.call_tool(tool, arguments)
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def run(url: str, token: str, graph_project: str, scientist_project: str) -> dict:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(180.0)
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=180) as client:
            graph = await call(client, "audit_research_graph", {"project_id": graph_project})
            scientist = await call(
                client, "audit_scientist_project", {"project_id": scientist_project}
            )
            exported = await call(
                client, "export_scientist_report", {"project_id": scientist_project}
            )
            graph_audit = graph.get("result") or {}
            scientist_audit = scientist.get("result") or {}
            if not graph_audit.get("valid") or not graph_audit.get("accepted"):
                raise RuntimeError(f"persisted graph audit failed: {graph}")
            if not scientist_audit.get("valid") or not scientist_audit.get("accepted"):
                raise RuntimeError(f"persisted scientist audit failed: {scientist}")
            return {
                "schema_version": 1,
                "purpose": "post-restart feature persistence smoke",
                "protocol_version": client.protocol_version,
                "graph": {
                    "project_id": graph_project,
                    "valid": True,
                    "accepted": True,
                    "event_count": graph_audit.get("event_count"),
                    "asset_count": graph_audit.get("asset_count"),
                },
                "scientist": {
                    "project_id": scientist_project,
                    "valid": True,
                    "accepted": True,
                    "execution_status": scientist_audit.get("execution_status"),
                    "event_count": scientist_audit.get("event_count"),
                    "exported": exported.get("exit_code") == 0,
                },
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("token")
    parser.add_argument("graph_project")
    parser.add_argument("scientist_project")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(
        args.url, args.token, args.graph_project, args.scientist_project
    )), indent=2))


if __name__ == "__main__":
    main()
