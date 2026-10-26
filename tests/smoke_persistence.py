from __future__ import annotations

import argparse
import asyncio
import json

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def run(url: str, token: str, project_id: str) -> dict:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(180.0)
    ) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=180) as client:
            projects_result = await client.call_tool("list_projects", {})
            projects = projects_result.structured_content
            if project_id not in {row["project_id"] for row in projects["projects"]}:
                raise RuntimeError(f"persisted project not found: {project_id}")
            read_result = await client.call_tool(
                "read_file", {"project_id": project_id, "relative_path": "evidence-manifest.json"}
            )
            if read_result.is_error:
                raise RuntimeError(str(read_result.content))
            manifest_file = read_result.structured_content
            manifest = json.loads(manifest_file["content"])
            return {
                "project_id": project_id,
                "project_count": projects["count"],
                "manifest_sha256": manifest_file["sha256"],
                "manifest_file_count": manifest["file_count"],
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
