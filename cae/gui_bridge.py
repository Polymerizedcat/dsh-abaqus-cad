# -*- coding: utf-8 -*-
"""DSH Abaqus GUI bridge (runs inside the Abaqus/CAE *GUI* process).

Why this file exists: Abaqus/CAE has no COM or remote-kernel API, and a plain
``abaqus cae script=server.py`` thread starves because the kernel's idle loop
holds the GIL. The supported way to drive an already-open GUI is an AFX
plug-in: the GUI process owns a socket server and uses ``sendCommand`` to run
code in the kernel's main thread, then reads a JSON response file.

Adapted from the MIT-licensed CAE-Agent-Hub Abaqus MCP plugin (v5 socket
bridge) and Whfkl/Abaqus-Control-MCP; this version speaks the length-prefixed
JSON protocol used by dsh-abaqus-cad and reuses abq_helpers.

Loaded by %USERPROFILE%\\abaqus_v6.env::onCaeStartup().
"""
from __future__ import print_function

import base64
import json
import os
import queue
import socket
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import uuid

try:
    from abaqusGui import (
        AFXForm,
        AFXMode,
        FXMAPFUNC,
        SEL_COMMAND,
        SEL_TIMEOUT,
        getAFXApp,
        sendCommand,
        showAFXErrorDialog,
    )
except Exception as exc:  # pragma: no cover - only when imported outside CAE
    raise SystemExit("not running inside Abaqus/CAE GUI: %s" % exc)

__version__ = "0.2.0"
HOST = os.environ.get("ABAQUS_DSH_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_DSH_PORT", "48153"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_DSH_TIMEOUT", "600"))
CAE_DIR = os.environ.get("ABAQUS_DSH_CAE_DIR") or os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.environ.get(
    "ABAQUS_DSH_LOG",
    os.path.join(os.path.expanduser("~"), ".dsh", "abaqus-cad", "logs", "gui_bridge.log"),
)
MAX_RESULT_CHARS = 20000


def _log(message):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _announce(message):
    print(message)
    try:
        getAFXApp().getAFXMainWindow().writeToMessageArea(message)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# wire protocol (same as cae_server.py: 8 ASCII digits + JSON)
# --------------------------------------------------------------------------- #


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return buf


def _read_msg(sock):
    header = _recv_exact(sock, 8)
    size = int(header.decode("ascii"))
    return json.loads(_recv_exact(sock, size).decode("utf-8"))


def _send_msg(sock, obj):
    payload = json.dumps(obj, ensure_ascii=False, default=repr).encode("utf-8")
    sock.sendall(("%08d" % len(payload)).encode("ascii") + payload)


# --------------------------------------------------------------------------- #
# kernel execution
# --------------------------------------------------------------------------- #


def _kernel_script(code, response_path):
    """Build the code passed to sendCommand. Runs in the CAE kernel."""
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    resp = base64.b64encode(response_path.encode("utf-8")).decode("ascii")
    cae_dir = base64.b64encode(CAE_DIR.encode("utf-8")).decode("ascii")
    return r'''
import base64
import contextlib
import io
import json
import sys
import traceback

_code = base64.b64decode("%s").decode("utf-8")
_resp = base64.b64decode("%s").decode("utf-8")
_cae = base64.b64decode("%s").decode("utf-8")
if _cae not in sys.path:
    sys.path.insert(0, _cae)

_payload = {"ok": True, "stdout": "", "result": None, "error": None, "traceback": None}
_buf = io.StringIO()
try:
    from abaqus import mdb, session
    from abaqusConstants import *
    import mesh as _mesh
    import abq_helpers as abq
    _ns = {"mdb": mdb, "session": session, "abq": abq, "mesh": _mesh,
           "ElemType": _mesh.ElemType, "__name__": "__gui_bridge__"}
    for _k, _v in list(globals().items()):
        if _k.isupper():
            _ns[_k] = _v
    with contextlib.redirect_stdout(_buf):
        exec(compile(_code, "<gui-bridge>", "exec"), _ns)
    if "result" in _ns:
        _r = _ns["result"]
        try:
            json.dumps(_r)
            _payload["result"] = _r
        except Exception:
            _payload["result"] = repr(_r)
except Exception as _exc:
    _payload["ok"] = False
    _payload["error"] = "%%s: %%s" %% (type(_exc).__name__, _exc)
    _payload["traceback"] = traceback.format_exc()[-8000:]
finally:
    _payload["stdout"] = _buf.getvalue()[-%d:]
    with open(_resp, "w") as _fh:
        json.dump(_payload, _fh)
''' % (encoded, resp, cae_dir, MAX_RESULT_CHARS)


def _run_kernel_code(code, timeout):
    tmp_dir = os.path.join(os.path.expanduser("~"), ".dsh", "abaqus-cad", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    response_path = os.path.join(tmp_dir, "gui_%s.json" % uuid.uuid4().hex)
    script = _kernel_script(code, response_path)
    _log("sendCommand start resp=%s" % response_path)
    sendCommand(script, False, False)
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if os.path.exists(response_path):
            try:
                with open(response_path, "r") as handle:
                    payload = json.load(handle)
            finally:
                try:
                    os.remove(response_path)
                except Exception:
                    pass
            _log("kernel done ok=%s" % payload.get("ok"))
            return payload
        time.sleep(0.03)
    raise TimeoutError("Abaqus kernel did not answer within %ss" % timeout)


# --------------------------------------------------------------------------- #
# GUI-thread dispatcher
# --------------------------------------------------------------------------- #


class _Request(object):
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.result = None
        self.error = None


_REQUESTS = queue.Queue()
_SERVER = None
_SERVER_THREAD = None
_DISPATCHER = None
_PROCESSED = 0
_STARTED = None


def _handle_on_gui_thread(item):
    global _PROCESSED
    method = item.method
    params = item.params
    timeout = float(params.get("timeout") or DEFAULT_TIMEOUT)

    if method == "ping":
        _PROCESSED += 1
        return {
            "pong": True, "mode": "gui", "pid": os.getpid(),
            "python": sys.version.split()[0], "bridge": __version__,
            "processed": _PROCESSED,
            "uptime": int(time.time() - _STARTED) if _STARTED else 0,
            "cwd": os.getcwd(), "log": LOG_PATH,
        }
    if method == "exec":
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("params.code must be a non-empty string")
        payload = _run_kernel_code(code, timeout)
        _PROCESSED += 1
        return payload
    if method == "stop":
        threading.Thread(target=stop_bridge, name="dsh-gui-stop").start()
        return {"stopped": True}
    if method == "close":
        threading.Thread(target=_close_cae, name="dsh-gui-close").start()
        return {"closing": True}
    raise ValueError("unknown method %r" % method)


def _close_cae():
    try:
        _log("close requested")
        window = getAFXApp().getAFXMainWindow()
        window.close()
    except Exception:
        _log("close failed: %s" % traceback.format_exc())
        os._exit(0)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _read_msg(self.request)
            request_id = message.get("id")
            method = message.get("cmd") or message.get("method")
            params = message.get("params") or message
            if _DISPATCHER is None:
                raise RuntimeError("GUI dispatcher not ready")
            wait = float(params.get("timeout") or DEFAULT_TIMEOUT) + 10.0
            item = _Request(method, params)
            _REQUESTS.put(item)
            if not item.event.wait(wait):
                raise TimeoutError("timed out waiting for the CAE GUI thread")
            if item.error is not None:
                raise item.error
            result = item.result
            if method == "exec":
                _send_msg(self.request, {"id": request_id, "ok": bool(result.get("ok")),
                                         "stdout": result.get("stdout", ""),
                                         "result": result.get("result"),
                                         "error": result.get("error"),
                                         "traceback": result.get("traceback")})
            else:
                _send_msg(self.request, {"id": request_id, "ok": True, "result": result})
        except Exception as exc:
            _log("handler error: %s" % exc)
            try:
                _send_msg(self.request, {"id": request_id, "ok": False,
                                         "error": "%s: %s" % (type(exc).__name__, exc),
                                         "traceback": traceback.format_exc()})
            except Exception:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_bridge():
    global _SERVER, _SERVER_THREAD, _STARTED
    if _SERVER is not None:
        return "already running on %s:%s" % (HOST, PORT)
    _SERVER = _Server((HOST, PORT), _Handler)
    _SERVER_THREAD = threading.Thread(target=_SERVER.serve_forever,
                                      name="dsh-gui-bridge", daemon=True)
    _SERVER_THREAD.start()
    _STARTED = time.time()
    _log("listening on %s:%s" % (HOST, PORT))
    return "DSH bridge listening on %s:%s" % (HOST, PORT)


def stop_bridge():
    global _SERVER, _SERVER_THREAD
    server = _SERVER
    if server is None:
        return "not running"
    try:
        server.shutdown()
        server.server_close()
    finally:
        _SERVER = None
        _SERVER_THREAD = None
    _log("stopped")
    return "stopped"


def _status():
    return {"version": __version__, "endpoint": "%s:%s" % (HOST, PORT),
            "running": _SERVER is not None, "processed": _PROCESSED,
            "log": LOG_PATH}


# --------------------------------------------------------------------------- #
# AFX menu + polling
# --------------------------------------------------------------------------- #


class _BridgeForm(AFXForm):
    ID_START = AFXForm.ID_LAST + 11
    ID_STOP = AFXForm.ID_LAST + 12
    ID_STATUS = AFXForm.ID_LAST + 13
    ID_POLL = AFXForm.ID_LAST + 14

    def __init__(self, owner, action):
        global _DISPATCHER
        AFXForm.__init__(self, owner)
        self.action = action
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, _BridgeForm.onActivate)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_START, _BridgeForm.onStart)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STOP, _BridgeForm.onStop)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STATUS, _BridgeForm.onStatus)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, _BridgeForm.onPoll)
        _DISPATCHER = self

    def getFirstDialog(self):
        return None

    def schedule_poll(self):
        try:
            getAFXApp().addTimeout(50, self, self.ID_POLL)
        except Exception as exc:
            _log("schedule_poll failed: %s" % exc)

    def onActivate(self, sender, sel, ptr):
        return self.onStart(sender, sel, ptr)

    def onStart(self, sender, sel, ptr):
        try:
            _announce(start_bridge())
            self.schedule_poll()
            _announce("DSH bridge: %s" % json.dumps(_status()))
        except Exception as exc:
            _log("start failed: %s" % exc)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), "DSH bridge failed: %s" % exc)
        return 1

    def onStop(self, sender, sel, ptr):
        _announce(stop_bridge())
        return 1

    def onStatus(self, sender, sel, ptr):
        _announce(json.dumps(_status(), indent=2))
        return 1

    def onPoll(self, sender, sel, ptr):
        processed = 0
        while processed < 8:
            try:
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            try:
                item.result = _handle_on_gui_thread(item)
            except Exception as exc:
                item.error = exc
                _log("gui handler error: %s" % exc)
            finally:
                item.event.set()
            processed += 1
        if _SERVER is not None:
            self.schedule_poll()
        return 1


def _register_menu():
    window = getAFXApp().getAFXMainWindow()
    toolset = window.getPluginToolset()
    modules = ["Part", "Property", "Assembly", "Step", "Interaction",
               "Load", "Mesh", "Job", "Visualization"]
    names = [
        ("start", "DSH Abaqus|Start GUI Bridge"),
        ("status", "DSH Abaqus|Bridge Status"),
        ("stop", "DSH Abaqus|Stop GUI Bridge"),
    ]
    for action, text in names:
        toolset.registerGuiMenuButton(
            object=_BridgeForm(toolset, action), buttonText=text,
            version=__version__, applicableModules=modules,
            description="dsh-abaqus-cad GUI bridge (%s)" % action,
        )
    _log("menu registered")


if not globals().get("_DSH_ABAQUS_BRIDGE_LOADED"):
    _register_menu()
    globals()["_DSH_ABAQUS_BRIDGE_LOADED"] = True
    # Auto-start: onCaeStartup runs after the GUI app exists, so the timeout
    # dispatcher is available. If scheduling fails, the Plug-ins menu still
    # lets the user start it manually.
    try:
        _announce(start_bridge())
        if _DISPATCHER is not None:
            _DISPATCHER.schedule_poll()
    except Exception:
        _log("auto-start failed: %s" % traceback.format_exc())
