# -*- coding: utf-8 -*-
"""In-kernel Abaqus/CAE bridge (runs inside Abaqus Python 3.10, 2024+).

Started by the MCP server as::

    abaqus cae noGUI=<this file>          # headless kernel
    abaqus cae script=<this file>         # GUI kernel (threaded)

It listens on 127.0.0.1:<port> and executes Python snippets in the CAE kernel,
returning captured stdout plus the JSON-able value of the variable ``result``.

Wire format: 8 ASCII decimal digits (payload length) + UTF-8 JSON, one
request/response per frame. This module must stay compatible with the Abaqus
2024-2026 embedded interpreter (CPython 3.10); do not import third-party
packages.
"""
from __future__ import print_function

import json
import os
import socket
import sys
import threading
import time
import traceback

from abaqus import mdb, session
from abaqusConstants import *  # noqa: F401,F403
from caeModules import *  # noqa: F401,F403
import mesh

# Abaqus executes startup scripts without putting their directory on sys.path.
_HERE = os.environ.get("ABAQUS_BRIDGE_HOME")
if not _HERE:
    try:
        _HERE = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        _HERE = os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import odbAccess
except Exception:  # pragma: no cover - older builds expose it via caeModules
    odbAccess = None

import abq_helpers as abq

HOST = os.environ.get("ABAQUS_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_BRIDGE_PORT", "48901"))
TOKEN = os.environ.get("ABAQUS_BRIDGE_TOKEN", "")
LOG = os.environ.get("ABAQUS_BRIDGE_LOG", "")
MODE = os.environ.get("ABAQUS_BRIDGE_MODE", "headless")
MAX_RESULT_CHARS = 20000
STARTED = time.time()


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    if LOG:
        try:
            with open(LOG, "a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return buf


def read_msg(conn):
    header = recv_exact(conn, 8)
    size = int(header.decode("ascii"))
    payload = recv_exact(conn, size)
    return json.loads(payload.decode("utf-8"))


def send_msg(conn, obj):
    payload = json.dumps(obj, ensure_ascii=False, default=repr).encode("utf-8")
    conn.sendall(("%08d" % len(payload)).encode("ascii") + payload)


class Tee(object):
    def __init__(self):
        self.buf = []

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass

    def text(self):
        return "".join(self.buf)


def _jsonable(value):
    """Return (value, type) for the response, capped in size."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > MAX_RESULT_CHARS:
            return value[:MAX_RESULT_CHARS] + "...[truncated]", "str"
        return value, type(value).__name__
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:
        text = repr(value)
    if len(text) <= MAX_RESULT_CHARS:
        try:
            return json.loads(text), "json"
        except Exception:
            pass
    return text[:MAX_RESULT_CHARS] + "...[truncated]", "jsonstr"


def namespace():
    if os.environ.get("ABAQUS_BRIDGE_RELOAD") == "1":
        try:
            import importlib
            importlib.reload(abq)
        except Exception as exc:
            log("reload failed: %s" % exc)
    ns = dict(globals())
    ns.update({"mdb": mdb, "session": session, "odbAccess": odbAccess, "abq": abq,
               "mesh": mesh, "ElemType": mesh.ElemType,
               "__name__": "__abaqus_bridge__"})
    models = list(mdb.models.keys())
    if models:
        try:
            ns["m"] = mdb.models[models[0]]
            parts = list(ns["m"].parts.keys())
            if parts:
                ns["p"] = ns["m"].parts[parts[0]]
        except Exception:
            pass
    return ns


def handle(req):
    started = time.time()
    code = req.get("code") or ""
    resp = {"id": req.get("id"), "ok": True, "stdout": "", "result": None,
            "resultType": None, "error": None, "traceback": None}
    out = Tee()
    old = sys.stdout
    sys.stdout = out
    try:
        ns = namespace()
        exec(compile(code, "<abaqus-bridge>", "exec"), ns)
        if "result" in ns:
            resp["result"], resp["resultType"] = _jsonable(ns["result"])
    except SystemExit:
        raise
    except Exception as exc:
        resp["ok"] = False
        resp["error"] = "%s: %s" % (type(exc).__name__, exc)
        resp["traceback"] = traceback.format_exc()[-8000:]
    finally:
        sys.stdout = old
    resp["stdout"] = out.text()[-MAX_RESULT_CHARS:]
    resp["elapsed"] = round(time.time() - started, 3)
    return resp


def session_loop(conn):
    while True:
        req = read_msg(conn)
        if TOKEN and req.get("token") != TOKEN:
            send_msg(conn, {"ok": False, "error": "bad token"})
            return
        cmd = req.get("cmd")
        if cmd == "ping":
            send_msg(conn, {"ok": True, "pong": True, "pid": os.getpid(),
                            "mode": MODE, "version": version_info(),
                            "uptime": round(time.time() - STARTED, 1)})
            continue
        if cmd == "shutdown":
            send_msg(conn, {"ok": True})
            log("shutdown requested")
            if MODE == "gui":
                os._exit(0)
            raise SystemExit(0)
        if cmd != "exec":
            send_msg(conn, {"ok": False, "error": "unknown cmd %r" % (cmd,)})
            continue
        resp = handle(req)
        resp["id"] = req.get("id")
        send_msg(conn, resp)


def version_info():
    v = {"abaqus": "?", "python": sys.version.split()[0], "mode": MODE}
    try:
        from abaqus import __version__ as av
        v["abaqus"] = str(av)
    except Exception:
        try:
            import abaqus
            v["abaqus"] = str(getattr(abaqus, "version", "?"))
        except Exception:
            pass
    try:
        v["cae"] = str(session.version.split()[0]) if session.version else "?"
    except Exception:
        pass
    v["models"] = list(mdb.models.keys())
    v["cwd"] = os.getcwd()
    return v


def serve_forever():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(4)
    log("abaqus bridge listening on %s:%d (py %s, mode %s)"
        % (HOST, PORT, sys.version.split()[0], MODE))
    while True:
        conn, addr = srv.accept()
        log("client %s" % (addr,))
        try:
            session_loop(conn)
        except (EOFError, ConnectionResetError):
            pass
        except SystemExit:
            raise
        except Exception as exc:
            log("session error %s" % exc)
            try:
                send_msg(conn, {"ok": False, "error": "bridge: %s" % exc})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def start_gui_thread():
    """GUI mode: serve in a daemon thread and hand the kernel back to CAE."""
    t = threading.Thread(target=serve_forever, name="abaqus-bridge")
    t.daemon = True
    t.start()
    log("GUI bridge thread started; kernel returned to CAE")


if __name__ == "__main__":
    if MODE == "gui":
        start_gui_thread()
    else:
        serve_forever()

