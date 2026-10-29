#!/usr/bin/env python3
"""Juniper's permission bridge: a minimal MCP server (stdio) for Claude Code's
--permission-prompt-tool. When Claude needs permission (to run a command, edit a file, leave plan
mode, or ask you a multiple-choice question), Claude Code calls the `approve` tool here; it asks
Juniper over a private Unix socket and waits for your answer in the overlay.

Standard library only. The socket lives in your Juniper data folder (or /tmp) and only you can open it.
"""
import json
import os
import socket
import sys

SOCK = os.environ.get("JUNIPER_SOCK", "")
SESSION = os.environ.get("JUNIPER_SESSION", "")

TOOL = {
    "name": "approve",
    "description": "Ask the user (via the Juniper overlay) whether a tool call may go ahead.",
    "inputSchema": {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                       "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"],
    },
}


def ask(args):
    """Returns Claude Code's permission decision: {"behavior": "allow", "updatedInput": …} or deny."""
    request = {"session": SESSION, "tool_name": args.get("tool_name", ""), "input": args.get("input") or {},
               "tool_use_id": args.get("tool_use_id", "")}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCK)
            s.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode() or "{}") or {"behavior": "deny", "message": "No answer from Juniper."}
    except OSError as e:
        return {"behavior": "deny", "message": f"Juniper isn't reachable to ask the user ({e})."}


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        continue                                     # notifications need no answer
    if method == "initialize":
        reply(msg_id, {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "juniper", "version": "1"}})
    elif method == "tools/list":
        reply(msg_id, {"tools": [TOOL]})
    elif method == "tools/call":
        decision = ask((msg.get("params") or {}).get("arguments") or {})
        reply(msg_id, {"content": [{"type": "text", "text": json.dumps(decision)}]})
    elif method == "ping":
        reply(msg_id, {})
    else:
        reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
