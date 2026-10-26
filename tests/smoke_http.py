from __future__ import annotations

import argparse
import asyncio
import json

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def smoke(url: str, token: str) -> dict:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport) as client:
            listed = await client.list_tools()
            capabilities = await client.call_tool("get_capabilities", {})
            return {
                "protocol_version": client.protocol_version,
                "tool_count": len(listed.tools),
                "tools": [tool.name for tool in listed.tools],
                "engine_hash_valid": capabilities.structured_content["engine_hash_valid"],
                "server_version": capabilities.structured_content["server_version"],
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("token")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(args.url, args.token)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

