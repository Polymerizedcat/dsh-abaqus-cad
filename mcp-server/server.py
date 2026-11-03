# -*- coding: utf-8 -*-
"""Abaqus MCP server: two discovery tools over the Abaqus/CAE kernel bridge.

Advertises exactly `search_tools` and `call_tool` so an idle session costs a few
hundred tokens; the 21-tool catalogue lives in catalog.py and is searched on
demand. Transport is MCP stdio (newline-delimited JSON-RPC 2.0), implemented
with the standard library only so no virtualenv or pip install is required.

Run:  python server.py
Test: python server.py --search "网格"
      python server.py --call abaqus_status "{}"
"""
import json
import os
import sys
import traceback

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels for screenshots
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import catalog  # noqa: E402
import handlers  # noqa: E402

SERVER_NAME = "abaqus-cae"
SERVER_VERSION = "0.1.0"
PROTOCOL = "2024-11-05"


def log(msg):
    sys.stderr.write("[abaqus-mcp] %s\n" % msg)
    sys.stderr.flush()


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "Intent or Abaqus command, Chinese or English: 网格, 静力, 接触, Mises, step, mesh..."},
        "top_k": {"type": "integer", "default": 6, "minimum": 1, "maximum": 25},
    },
    "required": ["query"],
}

CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Exact tool name returned by search_tools"},
        "arguments": {"type": "object", "description": "Tool arguments (see the args line)"},
    },
    "required": ["name"],
}

TOOLS_LIST = [
    {
        "name": "search_tools",
        "description": ("Search the Abaqus tool catalogue by intent (Chinese or English). "
                        "Returns exact tool names, one-line purpose and arguments. "
                        "Call this before call_tool; do not guess names."),
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "call_tool",
        "description": ("Run one Abaqus tool by exact name. Every tool returns compact JSON. "
                        "Use abaqus_run_python only when no catalogue tool covers the task."),
        "inputSchema": CALL_SCHEMA,
    },
]


def tool_text(tool, top_k):
    lines = ["%d matches (top_k=%d):" % (len(tool), top_k)]
    for t in tool:
        lines.append(catalog.describe(t))
    return "\n".join(lines)


def do_search(args):
    query = args.get("query") or ""
    top_k = int(args.get("top_k") or 6)
    picked = catalog.search(query, top_k=top_k)
    return tool_text(picked, top_k)


def do_call(args):
    name = args.get("name")
    if not name:
        raise handlers.ToolError("name is required")
    if name == "search_tools":
        return do_search(args.get("arguments") or {})
    if name == "call_tool":
        raise handlers.ToolError("call_tool cannot call itself")
    if catalog.by_name(name) is None:
        near = catalog.search(name, top_k=4)
        raise handlers.ToolError(
            "unknown tool %r. Close matches:\n%s"
            % (name, "\n".join(catalog.describe(t) for t in near)))
    result = handlers.dispatch(name, args.get("arguments") or {})
    return json.dumps(result, ensure_ascii=False, default=repr)


def handle_request(msg):
    method = msg.get("method")
    params = msg.get("params") or {}
    rid = msg.get("id")

    if method == "initialize":
        version = params.get("protocolVersion") or PROTOCOL
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": ("Abaqus/CAE automation. Call search_tools first, then call_tool. "
                             "The CAE kernel starts on demand. Units are user-defined; "
                             "keep one consistent system (mm-tonne-s or m-kg-s)."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS_LIST}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_tools":
                text = do_search(args)
            elif name == "call_tool":
                text = do_call(args)
            else:
                raise handlers.ToolError("unknown tool %r" % name)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }}
        except Exception as exc:  # tool failures are results, not transport errors
            detail = "%s: %s" % (type(exc).__name__, exc)
            log("tool error: " + detail)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": detail}],
                "isError": True,
            }}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    if method == "logging/setLevel":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def read_message(stream):
    """One JSON-RPC message: newline-delimited, or Content-Length framed."""
    line = stream.readline()
    if not line:
        return None
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text.lower().startswith("content-length:"):
        try:
            length = int(text.split(":", 1)[1].strip())
        except ValueError:
            return None
        while True:
            head = stream.readline()
            if not head or not head.strip():
                break
        body = stream.read(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(text)


def write_message(obj, stream):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(data + b"\n")
    stream.flush()


def serve():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    log("ready (pid %d, python %s)" % (os.getpid(), sys.version.split()[0]))
    while True:
        try:
            msg = read_message(stdin)
        except Exception as exc:
            log("bad frame: %s" % exc)
            break
        if msg is None:
            break
        if "id" not in msg:
            continue  # notification
        try:
            resp = handle_request(msg)
        except Exception as exc:
            log(traceback.format_exc())
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        try:
            write_message(resp, stdout)
        except Exception as exc:
            log("write failed: %s" % exc)
            break


def cli(argv):
    if "--search" in argv:
        q = argv[argv.index("--search") + 1] if len(argv) > argv.index("--search") + 1 else ""
        print(do_search({"query": q, "top_k": 10}))
        return 0
    if "--call" in argv:
        i = argv.index("--call")
        name = argv[i + 1]
        raw = argv[i + 2] if len(argv) > i + 2 else "{}"
        print(do_call({"name": name, "arguments": json.loads(raw)}))
        return 0
    if "--list" in argv:
        for t in catalog.TOOLS:
            print(catalog.describe(t))
        return 0
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
