#!/usr/bin/env python3
"""
Stdio MCP server that shows a caller's OpenAI tools to `claude --print`.

server.py writes the caller's tool list to a JSON file and starts claude with
this script as its only MCP server. Claude then sees those tools as real,
native tools and calls them with real tool_use blocks.

It never executes anything. `tools/call` gets no reply: server.py reads the
tool_use off claude's stream-json output, kills the process group, and hands
the call back to the caller as OpenAI `tool_calls`. The caller runs the tool
and sends the result on its next request.

Usage: tool_bridge_mcp.py <tools.json>
  tools.json = [{"name": ..., "description": ..., "inputSchema": {...}}, ...]

Stdlib only, newline-delimited JSON-RPC as the MCP stdio transport specifies.
Exits on stdin EOF, so it dies with claude even if the group kill misses it.
"""
import json
import sys


def main() -> None:
    with open(sys.argv[1]) as f:
        tools = json.load(f)

    def reply(msg_id, result) -> None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claude-code-api-caller-tools", "version": "1"},
            })
        elif method == "tools/list":
            reply(msg_id, {"tools": tools})
        elif method == "tools/call":
            # Deliberately unanswered. The wrapper stops claude before this
            # could matter; answering would let claude continue with a fake result.
            continue
        elif msg_id is not None:
            reply(msg_id, {})


if __name__ == "__main__":
    main()
