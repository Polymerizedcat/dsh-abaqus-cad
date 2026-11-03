# -*- coding: utf-8 -*-
"""Host-side bridge: manage the Abaqus/CAE kernel process and solver jobs.

The CAE side (cae/cae_server.py) listens on a loopback TCP port and executes
Python snippets in the CAE kernel. This module owns that process, converts
non-ASCII paths to ASCII junctions (Abaqus' C++ layer cannot open them), and
runs solver jobs as ordinary subprocesses so the kernel stays free.

No third-party packages: the MCP server must start on a bare Python 3.8+.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DSH_DIR = os.path.join(HOME, ".dsh")
CONFIG_PATH = os.path.join(DSH_DIR, "abaqus-cad.json")
STATE_DIR = os.path.join(HOME, ".dsh", "abaqus-cad")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
JOBS_PATH = os.path.join(STATE_DIR, "jobs.json")
LINK_ROOT = os.path.join(HOME, ".dsh", "abaqus-cad", "links")


class BridgeError(RuntimeError):
    pass


def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass
    if os.environ.get("ABAQUS_MCP_WORKDIR"):
        cfg["workDir"] = os.environ["ABAQUS_MCP_WORKDIR"]
    if os.environ.get("ABAQUS_CMD"):
        cfg["abaqusCmd"] = os.environ["ABAQUS_CMD"]
    if os.environ.get("ABAQUS_MCP_PORT"):
        cfg["port"] = int(os.environ["ABAQUS_MCP_PORT"])
    cfg.setdefault("port", 48901)
    cfg.setdefault("guiPort", 48153)
    cfg.setdefault("mode", "headless")
    cfg.setdefault("timeout", 300)
    if not cfg.get("workDir"):
        cfg["workDir"] = os.path.join("D:\\" if os.path.isdir("D:\\") else HOME, "Abaqus", "work")
    cfg["workDir"] = os.path.abspath(cfg["workDir"])
    cfg["abaqusCmd"] = cfg.get("abaqusCmd") or find_abaqus()
    return cfg


def save_config(cfg):
    os.makedirs(DSH_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def find_abaqus():
    env = os.environ.get("ABAQUS_CMD")
    if env and os.path.exists(env):
        return env
    which = shutil.which("abaqus") or shutil.which("abaqus.bat")
    if which:
        return which
    for drive in ("C:", "D:", "E:"):
        cand = os.path.join(drive + os.sep, "SIMULIA", "Commands", "abaqus.bat")
        if os.path.exists(cand):
            return cand
    return "abaqus"


# --------------------------------------------------------------------------- #
# Abaqus installation / version detection
# --------------------------------------------------------------------------- #

_DETECT_CACHE = {}


def _embedded_python(root):
    smapy = os.path.join(root, "tools", "SMApy")
    if not os.path.isdir(smapy):
        return None
    dirs = [d for d in os.listdir(smapy) if d.startswith("python")]
    dirs.sort(key=lambda d: (d != "python3.10", d))
    for d in dirs:
        exe = os.path.join(smapy, d, "python.exe")
        if not os.path.exists(exe):
            continue
        version = None
        try:
            r = subprocess.run([exe, "-c", "import sys;print(sys.version.split()[0])"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                version = (r.stdout or "").strip().splitlines()[0]
        except Exception:
            pass
        return {"path": exe, "version": version, "dir": d}
    return None


def list_installations():
    out = []
    for drive in ("C:", "D:", "E:", "F:"):
        root = os.path.join(drive + os.sep, "SIMULIA", "EstProducts")
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not (len(name) == 4 and name.isdigit()):
                continue
            win = os.path.join(root, name, "win_b64")
            bin_dir = os.path.join(win, "code", "bin")
            if not os.path.isdir(win):
                continue
            out.append({
                "version": int(name), "root": win, "bin": bin_dir,
                "solver": (os.path.exists(os.path.join(bin_dir, "pre.exe"))
                           and os.path.exists(os.path.join(bin_dir, "standard.exe"))),
            })
    out.sort(key=lambda x: -x["version"])
    return out


def detect_abaqus(cfg=None, force=False):
    cfg = cfg or load_config()
    cmd = cfg.get("abaqusCmd") or find_abaqus()
    min_version = int(os.environ.get("ABAQUS_MIN_VERSION", cfg.get("minVersion", 2024)))
    key = (cmd, min_version)
    if not force and key in _DETECT_CACHE:
        return _DETECT_CACHE[key]

    installs = list_installations()
    text = ""
    version = None
    if cmd:
        try:
            res = subprocess.run(["cmd", "/c", cmd, "information=versions"],
                                 capture_output=True, text=True, timeout=30,
                                 cwd=os.environ.get("TEMP", os.getcwd()),
                                 encoding="utf-8", errors="replace")
            text = (res.stdout or "") + "\n" + (res.stderr or "")
        except Exception as exc:
            text = "detect failed: %s" % exc
        m = re.search(r"Official Version:\s*Abaqus\s*(\d{4})", text, re.I) \
            or re.search(r"\bAbaqus\s+(\d{4})\b", text, re.I)
        if m:
            version = int(m.group(1))
    if version is None and installs:
        version = installs[0]["version"]

    install = next((i for i in installs if i["version"] == version), None) or (installs[0] if installs else None)
    python = _embedded_python(install["root"]) if install else None
    supported = bool(version and version >= min_version)
    result = {
        "found": bool(cmd or installs),
        "command": cmd,
        "version": version,
        "minVersion": min_version,
        "supported": supported,
        "reason": None if supported else (
            "Abaqus not found" if not (cmd or installs)
            else "Abaqus %s is older than %s" % (version, min_version)),
        "python": python,
        "solver": (bool(install["solver"]) if install else None),
        "installRoot": install["root"] if install else None,
        "installations": [{"version": i["version"], "solver": i["solver"]} for i in installs],
        "rawOutput": text[-4000:],
    }
    _DETECT_CACHE[key] = result
    return result


# --------------------------------------------------------------------------- #
# ASCII path shim
# --------------------------------------------------------------------------- #


def ascii_path(path):
    """Return a path Abaqus can open. Non-ASCII parents become NTFS junctions."""
    path = os.path.abspath(path)
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    parent = os.path.dirname(path)
    base = os.path.basename(path)
    key = hashlib.sha1(parent.encode("utf-8")).hexdigest()[:10]
    link = os.path.join(LINK_ROOT, "d" + key)
    if not os.path.isdir(link):
        os.makedirs(LINK_ROOT, exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", link, parent],
                       capture_output=True, text=True)
    if not os.path.isdir(link):
        raise BridgeError("cannot create ASCII junction for %s" % parent)
    return os.path.join(link, base)


def ascii_workdir(cfg):
    wd = os.path.abspath(cfg["workDir"])
    try:
        wd.encode("ascii")
        os.makedirs(wd, exist_ok=True)
        return wd
    except UnicodeEncodeError:
        link = os.path.join(LINK_ROOT, "work")
        os.makedirs(LINK_ROOT, exist_ok=True)
        if not os.path.isdir(link):
            subprocess.run(["cmd", "/c", "mklink", "/J", link, wd],
                           capture_output=True, text=True)
        os.makedirs(wd, exist_ok=True)
        return link


# --------------------------------------------------------------------------- #
# kernel bridge
# --------------------------------------------------------------------------- #


_HINTS = [
    ("实体旋转特征创建失败",
     "Revolve profile rejected. Use abaqus_part kind='revolve' with [[r,z],...]; "
     "keep the first/last points on the axis (r=0) and do NOT draw the axis segment."),
    ("Feature creation failed",
     "Geometry selection was empty. Check that the 'at' point lies on the chosen "
     "face/edge and that the instance name is correct."),
    ("input file",
     "Job input is missing. Call abaqus_job {action:'write'} first; the bridge now "
     "copies the .inp from the CAE working directory into D:\\Abaqus\\work."),
    ("NameError: name 'OFF'",
     "Kernel namespace lost abaqusConstants. Restart the session with "
     "abaqus_session {action:'restart'} (both bridges now inject the constants)."),
    ("KeyError",
     "Object name not found. Use abaqus_model_info or abaqus_inspect to list "
     "existing models/jobs/steps before referring to them."),
]


def _error_hint(message):
    text = str(message or "")
    for needle, hint in _HINTS:
        if needle.lower() in text.lower():
            return hint
    return None


class KernelClient(object):
    def __init__(self, port=48901, timeout=300):
        self.port = int(port)
        self.timeout = timeout
        self._seq = 0

    def _read(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise BridgeError("CAE kernel closed the connection")
            buf += chunk
        return buf

    def call(self, obj, timeout=None):
        timeout = timeout or self.timeout
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.settimeout(timeout)
        try:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            sock.sendall(b"%08d" % len(payload) + payload)
            header = self._read(sock, 8)
            size = int(header.decode("ascii"))
            return json.loads(self._read(sock, size).decode("utf-8"))
        finally:
            sock.close()

    def ping(self, timeout=3):
        try:
            return self.call({"cmd": "ping"}, timeout=timeout)
        except Exception:
            return None

    def run(self, code, timeout=None):
        self._seq += 1
        res = self.call({"id": self._seq, "cmd": "exec", "code": code}, timeout=timeout)
        if not res.get("ok"):
            err = res.get("error") or "CAE kernel error"
            tb = (res.get("traceback") or "").strip().splitlines()
            tail = " | ".join(tb[-4:]) if tb else ""
            hint = _error_hint(err)
            if hint:
                err = err + "\nHint: " + hint
            raise BridgeError(err + ("\n" + tail if tail else ""))
        return res

    def shutdown(self):
        try:
            self.call({"cmd": "shutdown"}, timeout=5)
        except Exception:
            pass


class Session(object):
    """Owns one CAE session: headless kernel (background) or GUI bridge (gui).

    background  : this server launches `abaqus cae noGUI=...` and kills it later.
    gui         : attaches to a GUI bridge running inside an already-open CAE
                  (installed by lib/index.js as %USERPROFILE%\abaqus_plugins).
                  `launch=True` starts a visible CAE itself and marks it managed.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.client = KernelClient(self.cfg["port"], self.cfg.get("timeout", 300))
        self.gui = KernelClient(self.cfg.get("guiPort", 48153),
                                self.cfg.get("timeout", 300))
        self.proc = None
        self.mode = None
        self.managed = False
        self._load_state()

    def _load_state(self):
        """Remember that this session launched a visible CAE, across MCP restarts."""
        try:
            with open(SESSION_PATH, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            return
        if rec.get("mode") == "gui" and rec.get("managed"):
            self.managed = True
            self.mode = "gui"

    # -- probing ---------------------------------------------------------- #
    def probe(self):
        return {"background": self.client.ping(timeout=2),
                "gui": self.gui.ping(timeout=2)}

    def status(self):
        p = self.probe()
        mode = self.mode or ("gui" if p["gui"] else ("background" if p["background"] else None))
        return {"online": bool(mode), "mode": mode, "active": self.mode, "port": self.cfg["port"],
                "guiPort": self.cfg.get("guiPort", 48153),
                "workDir": self.cfg["workDir"],
                "background": p["background"], "gui": p["gui"],
                "managed": self.managed}

    def ensure(self, mode=None):
        if mode is None:
            probe = self.probe()
            mode = "gui" if probe["gui"] else "background"
        return self.start(mode)

    # -- lifecycle -------------------------------------------------------- #
    def start(self, mode="background", launch=False, timeout=120):
        mode = (mode or "background").lower()
        if mode in ("gui", "attach", "manual"):
            info = self.gui.ping()
            if info:
                self.mode = "gui"
                self.managed = False
                self._save_session("gui", self.cfg["workDir"])
                return dict(info, mode="gui", managed=False)
            if not launch:
                raise BridgeError(
                    "No Abaqus GUI bridge on 127.0.0.1:%s.\n"
                    "Open Abaqus/CAE, then either restart it (the bridge auto-loads "
                    "from %%USERPROFILE%%\\abaqus_plugins) or use "
                    "Plug-ins > DSH Abaqus > Start GUI Bridge; or call this tool with "
                    "launch=true to open a visible CAE." % self.cfg.get("guiPort", 48153))
            self._launch_gui(timeout)
            info = self.gui.ping()
            if not info:
                raise BridgeError("Visible CAE started but the GUI bridge did not answer.")
            self.mode = "gui"
            self.managed = True
            self._save_session("gui", self.cfg["workDir"])
            return dict(info, mode="gui", managed=True)

        info = self.client.ping()
        if info:
            self.mode = "background"
            return dict(info, mode="background", managed=True)
        self._launch_background(timeout)
        self.mode = "background"
        self.managed = True
        return dict(self.client.ping(), mode="background", managed=True)

    def _launch_background(self, timeout):
        abaqus = self.cfg.get("abaqusCmd") or find_abaqus()
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "cae", "cae_server.py")
        if not os.path.exists(script):
            raise BridgeError("cae_server.py missing at %s" % script)
        workdir = ascii_workdir(self.cfg)
        os.makedirs(os.path.join(STATE_DIR, "logs"), exist_ok=True)
        log = os.path.join(STATE_DIR, "logs", "cae.log")
        env = dict(os.environ)
        env.update({
            "ABAQUS_BRIDGE_PORT": str(self.cfg["port"]),
            "ABAQUS_BRIDGE_MODE": "headless",
            "ABAQUS_BRIDGE_LOG": log,
            "ABAQUS_BRIDGE_HOME": os.path.dirname(script),
            "PYTHONUTF8": "1",
        })
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(
            ["cmd", "/c", abaqus, "cae", "noGUI=" + script],
            cwd=workdir, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, creationflags=creation,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client.ping(timeout=2):
                self._save_session("background", workdir)
                return
            if self.proc.poll() is not None:
                raise BridgeError(
                    "Abaqus/CAE exited during startup (exit %s). See %s"
                    % (self.proc.returncode, log))
            time.sleep(1.0)
        raise BridgeError("Background CAE did not open its bridge within %ss" % timeout)

    def _launch_gui(self, timeout):
        abaqus = self.cfg.get("abaqusCmd") or find_abaqus()
        workdir = ascii_workdir(self.cfg)
        os.makedirs(os.path.join(STATE_DIR, "logs"), exist_ok=True)
        log = os.path.join(STATE_DIR, "logs", "cae_gui.log")
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            self.proc = subprocess.Popen(
                ["cmd", "/c", abaqus, "cae"], cwd=workdir,
                stdout=fh, stderr=subprocess.STDOUT, creationflags=creation,
            )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.gui.ping(timeout=2):
                return
            if self.proc.poll() is not None:
                raise BridgeError("Visible CAE exited during startup (exit %s); see %s"
                                  % (self.proc.returncode, log))
            time.sleep(1.5)
        raise BridgeError(
            "Visible CAE started but the GUI bridge did not load within %ss. "
            "Check that %%USERPROFILE%%\\abaqus_plugins\\dsh_abaqus_bridge_plugin.py "
            "exists and the CAE log at %s." % (timeout, log))

    def _save_session(self, mode, workdir):
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(SESSION_PATH, "w", encoding="utf-8") as fh:
                json.dump({"pid": self.proc.pid if self.proc else None,
                           "port": self.cfg["port"], "mode": mode,
                           "managed": self.managed, "workDir": workdir,
                           "started": time.time()}, fh)
        except Exception:
            pass

    def stop(self, close_cae=False):
        out = {"stopped": True, "closedCae": False}
        mode = self.mode
        do_gui = mode == "gui" or (mode is None and self.gui.ping())
        do_bg = mode == "background" or (mode is None and self.client.ping())
        gui_pid = None
        if do_gui:
            gui = self.gui.ping()
            if gui:
                gui_pid = (gui.get("result") or {}).get("pid")
                try:
                    self.gui.call({"cmd": "stop"}, timeout=10)
                    out["guiBridge"] = "stopped"
                except Exception as exc:
                    out["guiBridge"] = "stop failed: %s" % exc
                if close_cae and self.managed:
                    try:
                        self.gui.call({"cmd": "close"}, timeout=10)
                    except Exception:
                        pass
                    deadline = time.time() + 12
                    while gui_pid and time.time() < deadline:
                        if _proc_exit_code(gui_pid) is not None:
                            break
                        time.sleep(0.5)
                    if gui_pid and _proc_exit_code(gui_pid) is None:
                        subprocess.run(["taskkill", "/PID", str(gui_pid), "/T", "/F"],
                                       capture_output=True)
                    out["closedCae"] = True
                else:
                    out["caeLeftOpen"] = True
                    out["note"] = ("CAE was not started by this plugin, so it is "
                                   "left open; the bridge server is stopped.")
        if do_bg:
            bg = self.client.ping()
            if bg:
                self.client.shutdown()
                out["background"] = "stopped"
        if self.proc is not None and self.mode == "background":
            try:
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.proc is not None and self.mode == "gui" and close_cae and self.managed:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                   capture_output=True)
                except Exception:
                    pass
        try:
            os.remove(SESSION_PATH)
        except Exception:
            pass
        self.proc = None
        self.managed = False
        self.mode = None
        return out

    # -- convenience ------------------------------------------------------ #
    def _client(self):
        if self.mode == "gui":
            return self.gui
        if self.mode == "background":
            return self.client
        return self.gui if self.gui.ping(timeout=1) else self.client

    def run(self, code, timeout=None):
        return self._client().run(code, timeout=timeout)

    def run_json(self, code, timeout=None):
        return self._client().run(code, timeout=timeout).get("result")


# --------------------------------------------------------------------------- #
# solver jobs
# --------------------------------------------------------------------------- #


def _read_jobs():
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_jobs(jobs):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(JOBS_PATH, "w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=2, ensure_ascii=False)


def _tail(path, n=80):
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def job_status(name, workdir):
    refresh_jobs()
    jobs = _read_jobs()
    rec = jobs.get(name, {})
    wd = rec.get("workDir") or workdir
    out = {"name": name, "workDir": wd, "state": rec.get("state", "UNKNOWN")}
    lck = os.path.join(wd, name + ".lck")
    sta = os.path.join(wd, name + ".sta")
    msg = os.path.join(wd, name + ".msg")
    log = os.path.join(wd, name + ".log")
    if os.path.exists(msg):
        text = _tail(msg, 400)
        errors = [ln.strip() for ln in text.splitlines()
                  if "*** ERROR" in ln or "ERROR:" in ln][-5:]
        warnings = [ln.strip() for ln in text.splitlines()
                    if "*** WARNING" in ln][-5:]
        out["errors"] = errors
        out["warnings"] = warnings
    if os.path.exists(sta):
        text = _tail(sta, 12)
        out["sta"] = text.strip().splitlines()[-4:]
    if os.path.exists(lck):
        out["state"] = "RUNNING"
    if os.path.exists(log):
        tail = _tail(log, 60)
        if ("could not locate the pre executable" in tail
                or "could not locate the standard executable" in tail
                or "This may indicate that this component is not installed" in tail):
            out["solverMissing"] = True
            out["state"] = "ABORTED"
            out["errors"] = [ln for ln in tail.splitlines() if "Abaqus Error" in ln][:3]
            out["hint"] = ("Abaqus solver is not installed on this machine "
                           "(pre.exe missing). CAE modelling still works; "
                           "install the Abaqus analysis solver to submit jobs.")
        elif "exited with errors" in tail:
            out["state"] = "ABORTED"
            out["errors"] = [ln for ln in tail.splitlines() if "Error" in ln][:5]
    out["odb"] = name + ".odb" if os.path.exists(os.path.join(wd, name + ".odb")) else None
    # Abaqus' launcher can exit 0 even when the solver aborted, and vice versa.
    # The .sta file is authoritative for a completed analysis.
    if os.path.exists(sta):
        stat = _tail(sta, 60)
        if "THE ANALYSIS HAS COMPLETED SUCCESSFULLY" in stat:
            out["state"] = "COMPLETED"
        elif "THE ANALYSIS HAS NOT BEEN COMPLETED" in stat or "ANALYSIS ABORTED" in stat:
            out["state"] = "ABORTED"
    return out


def submit_job(name, workdir, cpus=1, abaqus=None, cfg=None):
    cfg = cfg or load_config()
    jobs = _read_jobs()
    if name in jobs and jobs[name].get("state") == "RUNNING":
        raise BridgeError("job %r is already running" % name)
    wd = os.path.abspath(workdir)
    files = [name + ".inp", name + ".lck"]
    if not os.path.exists(os.path.join(wd, files[0])):
        raise BridgeError("input file %s not found in %s; call job action=write first"
                          % (files[0], wd))
    os.makedirs(os.path.join(STATE_DIR, "logs"), exist_ok=True)
    log_path = os.path.join(wd, name + ".log")
    abaqus = abaqus or cfg.get("abaqusCmd") or find_abaqus()
    cmd = ["cmd", "/c", abaqus, "job=" + name, "input=" + name,
           "cpus=" + str(int(cpus)), "interactive"]
    creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, cwd=wd, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=creation)
    jobs[name] = {"pid": proc.pid, "workDir": wd, "state": "RUNNING",
                  "started": time.time(), "returncode": None, "cpus": int(cpus)}
    _write_jobs(jobs)
    return {"name": name, "pid": proc.pid, "state": "RUNNING", "input": name + ".inp"}


def _proc_exit_code(pid):
    """Return None while the process runs, else its exit code (Windows/Linux)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                err = ctypes.get_last_error()
                # 87 = ERROR_INVALID_PARAMETER: the pid no longer exists.
                return 1 if err == 87 else None
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            if not ok:
                return None
            return None if code.value == 259 else int(code.value)
        os.kill(int(pid), 0)
        return None
    except Exception:
        return None


def refresh_jobs():
    """Poll child processes; remember exits even between MCP calls."""
    jobs = _read_jobs()
    changed = False
    for name, rec in jobs.items():
        if rec.get("state") != "RUNNING" or rec.get("pid") is None:
            continue
        code = _proc_exit_code(rec["pid"])
        if code is not None:
            rec["state"] = "COMPLETED" if code == 0 else "ABORTED"
            rec["returncode"] = code
            changed = True
    if changed:
        _write_jobs(jobs)
    return jobs


def wait_job(name, timeout=1800, poll=2.0):
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        st = job_status(name, load_config()["workDir"])
        if st["state"] in ("COMPLETED", "ABORTED"):
            return st
        time.sleep(poll)
    st = job_status(name, load_config()["workDir"])
    st["timedOut"] = True
    return st
