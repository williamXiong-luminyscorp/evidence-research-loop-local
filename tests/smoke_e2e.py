from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def call(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(f"{name} failed: {result.content}")
    return result.structured_content


async def run(url: str, token: str, project_id: str) -> dict:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(180.0)
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=180) as client:
            created = await call(client, "create_project", {"project_id": project_id, "template": "python-code"})
            validation = await call(client, "validate_project", {"project_id": project_id})
            if not validation["valid"]:
                raise RuntimeError(json.dumps(validation, indent=2))
            started = await call(client, "start_search", {"project_id": project_id})
            job_id = started["job"]["job_id"]
            deadline = time.time() + 120
            status = None
            while time.time() < deadline:
                status = await call(client, "get_status", {"project_id": project_id, "job_id": job_id})
                if status["status"] == "finished":
                    break
                await asyncio.sleep(0.5)
            if status is None or status["status"] != "finished":
                raise TimeoutError(json.dumps(status, indent=2))
            if status["result"]["exit_code"] != 0 or status["search"]["status"] != "completed":
                raise RuntimeError(json.dumps(status, indent=2))
            audit = await call(client, "audit_project", {"project_id": project_id})
            finalized = await call(client, "finalize_evidence", {"project_id": project_id})
            return {
                "created": created["created"],
                "job_id": job_id,
                "job_exit_code": status["result"]["exit_code"],
                "search_status": status["search"]["status"],
                "audit_valid": audit["result"]["valid"],
                "audit_accepted": audit["result"]["accepted"],
                "manifest_verified": finalized["verify"]["result"]["valid"],
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("token")
    parser.add_argument("project_id")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.url, args.token, args.project_id)), indent=2))


if __name__ == "__main__":
    main()

