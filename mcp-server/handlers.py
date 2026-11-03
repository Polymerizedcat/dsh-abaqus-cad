# -*- coding: utf-8 -*-
"""Tool implementations for the Abaqus MCP server.

Each handler is a thin layer: it validates arguments, emits a small Python
snippet that calls a function in cae/abq_helpers.py, and returns compact JSON.
Keeping the Abaqus API in the kernel-side helpers makes the wire payload small
and lets an `abaqus_run_python` call do anything the helpers do not cover yet.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import bridge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "cae", "render_template.py")

_session = None


class ToolError(RuntimeError):
    pass


def js(obj):
    return json.dumps(obj, ensure_ascii=True)


def cfg():
    return bridge.load_config()


def sess():
    global _session
    if _session is None:
        _session = bridge.Session(cfg())
    return _session


def run(code, timeout=300):
    return sess().run_json(code, timeout=timeout)


def run_full(code, timeout=300):
    return sess().run(code, timeout=timeout)


def _workdir():
    return cfg()["workDir"]


def _sqlit(value):
    """Python string literal, ASCII only (safe over the bridge)."""
    return json.dumps(str(value), ensure_ascii=True)


def _opt(value):
    """Optional string -> Python None instead of the literal 'None'."""
    return "None" if value in (None, "") else _sqlit(value)


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #


def t_status(args):
    st = sess().status()
    det = bridge.detect_abaqus()
    detected = {"abaqusVersion": det.get("version"),
                "abaqusSupported": det.get("supported"),
                "abaqusMinVersion": det.get("minVersion"),
                "abaqusPython": (det.get("python") or {}).get("version"),
                "solver": det.get("solver"),
                "installRoot": det.get("installRoot"),
                "abaqusReason": det.get("reason")}
    if not st["online"]:
        return dict({"online": False, "mode": None, "workDir": st["workDir"],
                     "backgroundPort": st["port"], "guiPort": st["guiPort"],
                     "hint": ("Ask the user which mode to use, then call abaqus_session "
                              "start with mode=background or mode=gui.")}, **detected)
    info = run("import os\nresult = {'models': abq.models(), 'cwd': os.getcwd(), "
               "'jobs': list(mdb.jobs.keys())}", timeout=30)
    cols = os.listdir(st["workDir"]) if os.path.isdir(st["workDir"]) else []
    jobs = bridge.refresh_jobs()
    cwd = info.get("cwd")
    matches = bool(cwd) and os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(
        os.path.abspath(st["workDir"]))
    return dict({"online": True, "mode": st["mode"], "managed": st["managed"],
            "cwdMatchesWorkDir": matches,
            "backgroundPort": st["port"], "guiPort": st["guiPort"],
            "workDir": st["workDir"],
            "abaqus": (st["gui"] or st["background"] or {}).get("version", {}),
            "models": info.get("models"), "cwd": info.get("cwd"),
            "jobs": list(jobs.keys()), "files": cols[:20]}, **detected)


def t_session(args):
    action = (args.get("action") or "start").lower()
    s = sess()
    if action in ("start", "restart"):
        det = bridge.detect_abaqus()
        if not det.get("found"):
            raise ToolError("Abaqus not found. Install Abaqus 2024+ (CAE + Analysis) "
                            "or set ABAQUS_CMD; run bin/dsh-abaqus-cad.mjs doctor.")
        if not det.get("supported") and os.environ.get("ABAQUS_ALLOW_UNSUPPORTED") != "1":
            raise ToolError("Abaqus %s is unsupported (minimum %s). "
                            "Set ABAQUS_ALLOW_UNSUPPORTED=1 only for experiments."
                            % (det.get("version"), det.get("minVersion")))
    if action == "stop":
        return s.stop(close_cae=bool(args.get("close")))
    if action == "start":
        mode = args.get("mode") or "background"
        info = s.start(mode=mode, launch=bool(args.get("launch")),
                       timeout=int(args.get("timeout", 120)))
        return {"started": True, "mode": info.get("mode"),
                "managed": info.get("managed"), "detail": info}
    if action == "restart":
        s.stop(close_cae=bool(args.get("close")))
        info = s.start(mode=args.get("mode") or "background",
                       launch=bool(args.get("launch")),
                       timeout=int(args.get("timeout", 120)))
        return {"restarted": True, "mode": info.get("mode"),
                "managed": info.get("managed")}
    if action == "install_gui":
        return _install_gui_bridge()
    raise ToolError("action must be start|stop|restart|install_gui")


def _install_gui_bridge():
    """Install the CAE GUI plug-in loader into %USERPROFILE%\abaqus_plugins."""
    import shutil
    home = os.path.expanduser("~")
    plugin_dir = os.path.join(home, "abaqus_plugins")
    os.makedirs(plugin_dir, exist_ok=True)
    gui_dir = os.path.join(home, ".dsh", "abaqus-cad", "gui")
    os.makedirs(gui_dir, exist_ok=True)
    for fname in ("gui_bridge.py", "abq_helpers.py"):
        src = os.path.join(ROOT, "cae", fname)
        if not os.path.exists(src):
            raise ToolError("%s not found in the package" % fname)
        shutil.copyfile(src, os.path.join(gui_dir, fname))
    loader = os.path.join(plugin_dir, "dsh_abaqus_bridge_plugin.py")
    with open(loader, "w", encoding="utf-8") as fh:
        fh.write(
            "# Auto-loaded by Abaqus/CAE from abaqus_plugins (*_plugin.py).\n"
            "import os\n"
            "_CAE_DIR = %r\n"
            "_PLUGIN = %r\n"
            "os.environ.setdefault('ABAQUS_DSH_CAE_DIR', _CAE_DIR)\n"
            "os.environ.setdefault('ABAQUS_DSH_PORT', '48153')\n"
            "if os.path.exists(_PLUGIN) and not globals().get('_DSH_ABAQUS_BRIDGE_LOADED'):\n"
            "    with open(_PLUGIN, 'r', encoding='utf-8') as _fh:\n"
            "        exec(compile(_fh.read(), _PLUGIN, 'exec'), globals())\n"
            % (gui_dir, os.path.join(gui_dir, "gui_bridge.py")))
    return {"installed": True, "plugin": loader, "bridge": gui_dir,
            "detected": bridge.detect_abaqus(),
            "hint": ("Restart Abaqus/CAE once; the bridge auto-starts. Then call "
                     "abaqus_session {action:'start', mode:'gui'}.")}


def t_finish(args):
    """End of task: save (optional), close ODBs, stop the session."""
    s = sess()
    out = {"saved": None, "session": None}
    save = args.get("save")
    st = s.status()
    if save is None and s.mode == "gui" and s.managed:
        # Closing a managed GUI would otherwise pop Abaqus' save prompt.
        save = os.path.join(_workdir(), "abaqus_session.cae")
    if save:
        if isinstance(save, str):
            path = save
        else:
            path = os.path.join(_workdir(), (args.get("name") or "model") + ".cae")
        path = bridge.ascii_path(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        run("mdb.saveAs(pathName=%s)\nresult = {'saved': %s}" % (_sqlit(path), _sqlit(path)),
            timeout=300)
        out["saved"] = path
    try:
        run("abq.odb_close()\nresult = {'closed': True}", timeout=30)
    except Exception:
        pass
    out["session"] = s.stop(close_cae=bool(args.get("close", True)))
    return out


def t_open(args):
    path = args.get("path")
    if not path:
        raise ToolError("path is required")
    path = bridge.ascii_path(path)
    if not os.path.exists(path):
        raise ToolError("file not found: %s" % args.get("path"))
    if args.get("kind") == "odb" or path.lower().endswith(".odb"):
        return run("result = abq.odb_summary(%s)" % _sqlit(path), timeout=600)
    code = ("from abaqus import openMdb\n"
            "openMdb(pathName=%s)\n"
            "result = abq.model_info(abq.model())" % _sqlit(path))
    return run(code, timeout=600)


def t_save(args):
    path = args.get("path")
    if not path:
        name = args.get("name") or "model"
        path = os.path.join(_workdir(), name + ".cae")
    path = bridge.ascii_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run("mdb.saveAs(pathName=%s)\nresult = {'saved': %s}" % (_sqlit(path), _sqlit(path)))
    return {"saved": path}


def t_model_new(args):
    name = args.get("name") or "Model-1"
    return run("m = abq.model(%s, create=True)\nresult = {'model': m.name}" % _sqlit(name))


def t_model_info(args):
    return run("result = abq.model_info(abq.model(%s))" % _sqlit(args.get("model") or ""))


def t_run_python(args):
    code = args.get("code")
    if not code:
        raise ToolError("code is required")
    timeout = int(args.get("timeout", 300))
    res = run_full(code, timeout=timeout)
    return {"stdout": (res.get("stdout") or "")[-8000:],
            "result": res.get("result"), "elapsed": res.get("elapsed")}


# --------------------------------------------------------------------------- #
# geometry / properties
# --------------------------------------------------------------------------- #


def t_part(args):
    kind = (args.get("kind") or "box").lower()
    name = args.get("name") or "Part-1"
    model = args.get("model") or ""
    head = "m = abq.model(%s)\n" % _sqlit(model)
    if kind in ("box", "cuboid"):
        size = args.get("size") or [args.get("L"), args.get("W"), args.get("H")]
        if len(size) != 3 or any(v is None for v in size):
            raise ToolError("box needs size=[L,W,H]")
        call = "abq.part_box(m, %s, %s, %s, %s)" % (
            _sqlit(name), float(size[0]), float(size[1]), float(size[2]))
    elif kind == "cylinder":
        r = args.get("radius")
        h = args.get("height")
        if r is None or h is None:
            raise ToolError("cylinder needs radius and height")
        call = "abq.part_cylinder(m, %s, %s, %s)" % (_sqlit(name), float(r), float(h))
    elif kind == "cone":
        r1 = args.get("r1", args.get("radius1"))
        r2 = args.get("r2", args.get("radius2"))
        h = args.get("height")
        if r1 is None or r2 is None or h is None:
            raise ToolError("cone needs r1, r2, height")
        call = "abq.part_cone(m, %s, %s, %s, %s)" % (
            _sqlit(name), float(r1), float(r2), float(h))
    elif kind in ("tube", "pipe"):
        outer = args.get("outer", args.get("radius"))
        inner = args.get("inner")
        h = args.get("height")
        if outer is None or inner is None or h is None:
            raise ToolError("tube needs outer, inner, height")
        call = "abq.part_tube(m, %s, %s, %s, %s)" % (
            _sqlit(name), float(outer), float(inner), float(h))
    elif kind in ("revolve", "revolution"):
        profile = args.get("profile")
        if not profile:
            raise ToolError("revolve needs profile=[[r,z],...]")
        close = args.get("close")
        close_lit = "None" if close is None else ("True" if close else "False")
        call = "abq.part_revolve(m, %s, %s, %s, %s)" % (
            _sqlit(name), js([[float(a), float(b)] for a, b in profile]),
            float(args.get("angle", 360.0)), close_lit)
    elif kind in ("shell", "plate"):
        size = args.get("size") or [args.get("L"), args.get("W")]
        if len(size) != 2 or any(v is None for v in size):
            raise ToolError("shell needs size=[L,W]")
        call = "abq.part_shell(m, %s, %s, %s)" % (_sqlit(name), float(size[0]), float(size[1]))
    elif kind in ("polygon", "extrude"):
        pts = args.get("points")
        if not pts:
            raise ToolError("polygon needs points=[[x,y],...]")
        call = "abq.part_polygon(m, %s, %s, %s)" % (
            _sqlit(name), js([list(map(float, p)) for p in pts]),
            float(args.get("depth", 1.0)))
    elif kind in ("import", "step", "iges", "sat"):
        path = args.get("path")
        if not path:
            raise ToolError("import needs path")
        call = "abq.part_import(m, %s, %s)" % (_sqlit(name),
                                               _sqlit(bridge.ascii_path(path)))
    else:
        raise ToolError("kind must be box|cylinder|cone|tube|revolve|shell|polygon|import")
    return run(head + "p = " + call + "\nresult = abq.part_info(p)")


def t_material(args):
    name = args.get("name") or "Steel"
    elastic = args.get("elastic")
    if elastic is None and args.get("E") is not None:
        elastic = [float(args["E"]), float(args.get("nu", 0.3))]
    code = ("m = abq.model(%s)\n"
            "abq.material(m, %s, density=%s, elastic=%s, plastic=%s, expansion=%s)\n"
            "result = {'material': %s, 'elastic': %s, 'density': %s}"
            % (_sqlit(args.get("model") or ""), _sqlit(name),
               args.get("density"), js(elastic) if elastic else None,
               js(args.get("plastic")) if args.get("plastic") else None,
               args.get("expansion"), _sqlit(name),
               js(elastic), args.get("density")))
    return run(code)


def t_section(args):
    name = args.get("name") or "Section-1"
    material = args.get("material")
    if not material:
        raise ToolError("material is required")
    kind = args.get("kind") or "solid"
    part = args.get("part")
    code = ("m = abq.model(%s)\n"
            "abq.section(m, %s, %s, %s, %s)\n"
            % (_sqlit(args.get("model") or ""), _sqlit(name), _sqlit(material),
               _sqlit(kind), args.get("thickness")))
    if part:
        code += "abq.assign_section(m.parts[%s], %s, %s)\n" % (
            _sqlit(part), _sqlit(name), _sqlit(kind))
    code += "result = {'section': %s, 'assigned': %s}" % (
        _sqlit(name), bool(part))
    return run(code)


def t_instance(args):
    part = args.get("part")
    if not part:
        raise ToolError("part is required")
    code = ("m = abq.model(%s)\n"
            "inst = abq.instance(m, %s, %s, %s, %s)\n"
            "result = {'instance': inst.name, 'part': %s}"
            % (_sqlit(args.get("model") or ""), _sqlit(part), _sqlit(args.get("name")),
               js(args.get("translate") or [0, 0, 0]),
               js(args["rotate"]) if args.get("rotate") else "None",
               _sqlit(part)))
    return run(code)


def t_set(args):
    name = args.get("name")
    kind = (args.get("kind") or "face").lower()
    spec = args.get("spec") or {"all": True}
    owner = (args.get("owner") or "assembly").lower()
    inst = _sqlit(args["instance"]) if args.get("instance") else "None"
    code = "m = abq.model(%s)\na = m.rootAssembly\n" % _sqlit(args.get("model") or "")
    if kind == "surface":
        code += "s = abq.make_surface(a, %s, %s, %s)\n" % (
            _sqlit(name), js(spec), inst)
        code += "result = {'surface': %s, 'faces': len(s.faces)}" % _sqlit(name)
    else:
        plural = {"face": "faces", "edge": "edges", "vertex": "vertices",
                  "cell": "cells"}[kind]
        if owner == "part":
            part = args.get("part")
            if not part:
                raise ToolError("owner=part needs part")
            code += "owner = m.parts[%s]\n" % _sqlit(part)
            inst = "None"
        else:
            code += "owner = a\n"
        code += "s = abq.make_set(owner, %s, %s, %s, %s)\n" % (
            _sqlit(name), _sqlit(plural), js(spec), inst)
        code += "result = {'set': s.name, 'kind': %s, 'count': len(s.%s)}" % (
            _sqlit(plural), plural)
    return run(code)


def t_mesh(args):
    part = args.get("part")
    if not part:
        raise ToolError("part is required")
    if not args.get("size"):
        raise ToolError("size (global seed) is required")
    technique = _sqlit(args["technique"]) if args.get("technique") else "None"
    shape = _sqlit(args["shape"]) if args.get("shape") else "None"
    code = ("m = abq.model(%s)\n"
            "result = abq.mesh_part(m.parts[%s], %s, %s, %s, %s, %s, %s)"
            % (_sqlit(args.get("model") or ""), _sqlit(part),
               float(args["size"]), js(args.get("elem")) if args.get("elem") else "None",
               float(args.get("deviation", 0.1)), float(args.get("minSize", 0.1)),
               technique, shape))
    return run(code, timeout=600)


def t_inspect(args):
    return run("result = abq.inspect_model(abq.model(%s), %s)" % (
        _sqlit(args.get("model") or ""),
        "False" if args.get("mesh") is False else "True"), timeout=600)


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #


def t_step(args):
    name = args.get("name") or "Step-1"
    kind = (args.get("kind") or "static").lower()
    code = ("m = abq.model(%s)\n"
            "abq.step(m, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n"
            "result = {'step': %s, 'kind': %s}" % (
                _sqlit(args.get("model") or ""), _sqlit(name), _sqlit(kind),
                _sqlit(args.get("previous") or "Initial"),
                "True" if args.get("nlgeom") else "False",
                args.get("timePeriod", 1.0), args.get("initialInc"),
                args.get("minInc"), args.get("maxInc"),
                args.get("maxNumInc", 100), args.get("numEigen", 10),
                _sqlit(name), _sqlit(kind)))
    return run(code)


def _region_code(args, name_hint, kind="face"):
    """Emit statements binding `region`; returns (setup_code, label)."""
    spec = args.get("region")
    model = args.get("model") or ""
    pre = "m = abq.model(%s)\na = m.rootAssembly\n" % _sqlit(model)
    want_surface = args.get("as") == "surface"
    if isinstance(spec, str):
        if want_surface:
            return pre + "region = a.surfaces[%s]\n" % _sqlit(spec), spec
        return pre + "region = a.sets[%s]\n" % _sqlit(spec), spec
    if not isinstance(spec, dict):
        raise ToolError("region must be a set/surface name or selector object")
    if spec.get("surface") is not None or want_surface:
        key = spec.get("surface") if isinstance(spec.get("surface"), str) else spec.get("name")
        plain = not any(k in spec for k in ("spec", "at", "box", "all"))
        if isinstance(key, str) and plain:
            return pre + "region = a.surfaces[%s]\n" % _sqlit(key), key
        geom = spec.get("spec") or {k: v for k, v in spec.items()
                                    if k not in ("surface", "name", "instance", "owner", "part")}
        label = spec.get("name") or (name_hint + "-surf")
        inst = spec.get("instance")
        return (pre + "region = abq.make_surface(a, %s, %s, %s)\n"
                % (_sqlit(label), js(geom), _sqlit(inst) if inst else "None")), label
    if spec.get("set"):
        return pre + "region = a.sets[%s]\n" % _sqlit(spec["set"]), spec["set"]
    geom = spec.get("spec") or {k: v for k, v in spec.items()
                                if k not in ("name", "instance", "owner", "part")}
    inst = spec.get("instance")
    if spec.get("owner") == "part":
        target = "m.parts[%s]" % _sqlit(spec.get("part") or spec.get("name"))
    else:
        target = "a"
    label = spec.get("name") or (name_hint + "-set")
    plural = {"face": "faces", "edge": "edges", "cell": "cells"}.get(kind, "faces")
    code = pre + "region = abq.make_set(%s, %s, %s, %s, %s)\n" % (
        target, _sqlit(label), _sqlit(plural), js(geom),
        _sqlit(inst) if inst else "None")
    return code, label


def t_bc(args):
    name = args.get("name") or "BC-1"
    setup, label = _region_code(args, name, "face")
    code = setup + ("abq.bc(m, %s, region, %s, %s, %s)\n"
                    "result = {'bc': %s, 'region': %s, 'kind': %s}" % (
                        _sqlit(name), _sqlit(args.get("kind") or "encastre"),
                        _sqlit(args.get("step") or "Initial"),
                        js(args.get("values") or {}),
                        _sqlit(name), _sqlit(label), _sqlit(args.get("kind") or "encastre")))
    return run(code)


def t_load(args):
    name = args.get("name") or "Load-1"
    kind = (args.get("kind") or "force").lower()
    load_args = dict(args)
    if kind == "pressure":
        load_args["as"] = "surface"
    setup, label = _region_code(load_args, name, "face")
    code = setup + ("abq.load(m, %s, %s, region, %s, %s)\n"
                    "result = {'load': %s, 'kind': %s, 'region': %s}" % (
                        _sqlit(name), _sqlit(kind), _sqlit(args.get("step") or "Step-1"),
                        js(args.get("values") or {}), _sqlit(name), _sqlit(kind),
                        _sqlit(label)))
    return run(code)


def t_coupling(args):
    """Reference point + coupling; optionally load the RP (correct total force)."""
    name = args.get("name") or "Coupling-1"
    point = args.get("point")
    if not point or len(point) != 3:
        raise ToolError("point=[x,y,z] is required")
    surface = args.get("surface")
    if not surface:
        raise ToolError("surface (existing name or selector) is required")
    kind = args.get("kind") or "kinematic"
    dofs = args.get("dofs") or [1, 2, 3]
    code = "m = abq.model(%s)\na = m.rootAssembly\n" % _sqlit(args.get("model") or "")
    code += "rp = abq.reference_point(m, %s, %s)\n" % (
        _sqlit(name), js([float(v) for v in point]))
    if isinstance(surface, str):
        code += "surf = a.surfaces[%s]\n" % _sqlit(surface)
    elif isinstance(surface, dict):
        inst = surface.get("instance")
        geom = surface.get("spec") or {k: v for k, v in surface.items()
                                       if k not in ("instance", "name")}
        sname = surface.get("name") or (name + "-surf")
        code += "surf = abq.make_surface(a, %s, %s, %s)\n" % (
            _sqlit(sname), js(geom), _sqlit(inst) if inst else "None")
    else:
        raise ToolError("surface must be a name or selector object")
    code += "abq.coupling(m, %s, surf, rp, %s, %s)\n" % (
        _sqlit(name), _sqlit(kind), js([int(v) for v in dofs]))
    result = {"coupling": name, "rp": name + "-RP", "kind": kind,
              "dofs": [int(v) for v in dofs]}
    step = args.get("step") or "Step-1"
    if args.get("force"):
        code += "abq.load(m, %s, 'force', a.sets[rp], %s, %s)\n" % (
            _sqlit(name + "-Force"), _sqlit(step), js(args["force"]))
        result["force"] = args["force"]
        result["forceStep"] = step
    if args.get("moment"):
        code += "abq.load(m, %s, 'moment', a.sets[rp], %s, %s)\n" % (
            _sqlit(name + "-Moment"), _sqlit(step), js(args["moment"]))
        result["moment"] = args["moment"]
    code += "result = %s" % js(result)
    return run(code)


def t_interaction(args):
    kind = (args.get("kind") or "contact").lower()
    name = args.get("name") or "Int-1"
    step = args.get("step") or "Step-1"
    model = _sqlit(args.get("model") or "")
    friction = float(args.get("friction", 0.0) or 0.0)
    if kind in ("general", "general_contact"):
        code = ("m = abq.model(%s)\n"
                "abq.contact_general(m, %s, %s, %s, %s)\n"
                "result = {'interaction': %s, 'kind': 'general'}" % (
                    model, _sqlit(name), _opt(step), _sqlit(args.get("solver") or "std"),
                    friction, _sqlit(name)))
    elif kind == "tie":
        code = ("m = abq.model(%s)\nm.rootAssembly\n"
                "abq.tie(m, %s, %s, a.surfaces[%s], a.surfaces[%s])\n"
                "result = {'constraint': %s, 'kind': 'tie'}" % (
                    model, _sqlit(name), _opt(step),
                    _sqlit(args.get("main")), _sqlit(args.get("secondary")), _sqlit(name)))
    elif kind == "contact":
        code = ("m = abq.model(%s)\na = m.rootAssembly\n"
                "abq.contact_pair(m, %s, %s, a.surfaces[%s], a.surfaces[%s], %s, %s)\n"
                "result = {'interaction': %s, 'kind': 'contact'}" % (
                    model, _sqlit(name), _opt(step),
                    _sqlit(args.get("main")), _sqlit(args.get("secondary")),
                    friction, _sqlit(args.get("solver") or "std"), _sqlit(name)))
    else:
        raise ToolError("kind must be general|contact|tie")
    return run(code)


# --------------------------------------------------------------------------- #
# jobs / odb
# --------------------------------------------------------------------------- #


def _job_name(args):
    return args.get("name") or "Job-1"


def _kernel_cwd():
    try:
        return run("import os\nresult = os.getcwd()", timeout=30)
    except Exception:
        return None


def _sync_inp(name, cwd=None):
    """Copy name.inp from the CAE kernel cwd into the MCP work dir."""
    dst = os.path.join(_workdir(), name + ".inp")
    if os.path.exists(dst):
        return dst
    src_cwd = cwd or _kernel_cwd()
    if not src_cwd:
        return None
    src = os.path.join(src_cwd, name + ".inp")
    if os.path.abspath(src) != os.path.abspath(dst) and os.path.exists(src):
        shutil.copyfile(src, dst)
    return dst if os.path.exists(dst) else None


def _write_input(name):
    """writeInput in the kernel; returns {'cwd': ...} or raises ToolError."""
    try:
        return run(
            "mdb.jobs[%s].writeInput(consistencyChecking=OFF)\n"
            "import os\nresult = {'inp': %s + '.inp', 'cwd': os.getcwd()}"
            % (_sqlit(name), _sqlit(name)), timeout=600)
    except Exception as exc:
        raise ToolError(
            "job %r is not available for write (%s). Call abaqus_job "
            "{action:'create', name:%r, model:'...'} first." % (name, exc, name))


def t_job(args):
    action = (args.get("action") or "status").lower()
    name = _job_name(args)
    wd = _workdir()
    if action == "create":
        model = args.get("model")
        if not model:
            raise ToolError("model is required for create")
        code = ("m = abq.model(%s)\n"
                "abq.job(m, %s, %s, %s, %s)\n"
                "result = {'job': %s}" % (_sqlit(""), _sqlit(name), _sqlit(model),
                                          int(args.get("cpus", 1)),
                                          _sqlit(args.get("description") or ""),
                                          _sqlit(name)))
        return run(code)
    if action == "write":
        res = _write_input(name)
        dst = _sync_inp(name, res.get("cwd"))
        return {"inp": dst or (name + ".inp"), "kernelCwd": res.get("cwd"),
                "synced": bool(dst), "workDir": wd}
    if action == "submit":
        cpus = int(args.get("cpus", 1))
        # Refresh the input from the model when the job exists; if it does not,
        # fall back to an already-written .inp in the work dir.
        kernel_cwd = None
        try:
            res = run("mdb.jobs[%s].writeInput(consistencyChecking=OFF)\n"
                      "import os\nresult = {'cwd': os.getcwd()}" % _sqlit(name),
                      timeout=600)
            kernel_cwd = res.get("cwd")
        except Exception as exc:
            if not os.path.exists(os.path.join(wd, name + ".inp")):
                raise ToolError(
                    "job %r is not available and no .inp exists (%s). "
                    "Call action='create' then action='write'." % (name, exc))
        if not os.path.exists(os.path.join(wd, name + ".inp")):
            _sync_inp(name, kernel_cwd)
        if not os.path.exists(os.path.join(wd, name + ".inp")):
            raise ToolError(
                "input file %s.inp not found in %s (kernel cwd %s). "
                "Call action='write' first." % (name, wd, kernel_cwd))
        return bridge.submit_job(name, wd, cpus=cpus)
    if action == "status":
        return bridge.job_status(name, wd)
    if action == "wait":
        return bridge.wait_job(name, timeout=int(args.get("timeout", 1800)))
    if action == "abort":
        jobs = bridge.refresh_jobs()
        rec = jobs.get(name)
        if not rec or not rec.get("pid"):
            raise ToolError("no running job %r" % name)
        subprocess.run(["taskkill", "/PID", str(rec["pid"]), "/T", "/F"],
                       capture_output=True, text=True)
        rec["state"] = "ABORTED"
        bridge._write_jobs(jobs)
        return {"aborted": name}
    raise ToolError("action must be create|write|submit|status|wait|abort")


def _odb_path(args):
    path = args.get("path") or args.get("job")
    if not path:
        raise ToolError("path or job is required")
    if not path.lower().endswith(".odb"):
        path = os.path.join(_workdir(), path + ".odb")
    path = bridge.ascii_path(path)
    if not os.path.exists(path):
        raise ToolError("ODB not found: %s" % args.get("path"))
    return path


def t_odb(args):
    action = (args.get("action") or "summary").lower()
    path = _odb_path(args)
    if action == "summary":
        return run("result = abq.odb_summary(%s)" % _sqlit(path), timeout=600)
    if action == "extrema":
        return run("result = abq.odb_extrema(%s, %s, %s, %s, %s, %s)" % (
            _sqlit(path), _opt(args.get("step")), int(args.get("frame", -1) or -1),
            _sqlit(args.get("field") or "S"), _sqlit(args.get("component") or "MISES"),
            int(args.get("top", 5))), timeout=900)
    if action == "probe":
        if args.get("node") is None:
            raise ToolError("node is required")
        return run("result = abq.odb_probe(%s, %s, %s, %s, %s)" % (
            _sqlit(path), int(args["node"]), _opt(args.get("step")),
            int(args.get("frame", -1) or -1),
            js(args.get("fields")) if args.get("fields") else "None"), timeout=600)
    if action == "history":
        return run("result = abq.odb_history(%s, %s, %s)" % (
            _sqlit(path), _opt(args.get("step")), _opt(args.get("output"))),
            timeout=600)
    raise ToolError("action must be summary|extrema|probe|history")


# --------------------------------------------------------------------------- #
# plot / export
# --------------------------------------------------------------------------- #


def _print_window(hwnd):
    """Capture a window's own surface with PrintWindow (works when occluded)."""
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        ok = user32.PrintWindow(hwnd, mdc, 2)
        bmi = BMIH()
        bmi.biSize = ctypes.sizeof(BMIH)
        bmi.biWidth = w
        bmi.biHeight = -h
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
        if not ok or not got:
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        # DPI-virtualised windows render at 100% into a scaled buffer; trim the
        # black padding so the PNG is the actual CAE surface.
        try:
            from PIL import ImageChops
            box = ImageChops.difference(img, Image.new("RGB", img.size, (0, 0, 0))).getbbox()
            if box:
                img = img.crop(box)
        except Exception:
            pass
        return img
    except Exception:
        return None


def _capture_cae_window(out, focus=None, pid=None):
    """Capture the visible Abaqus/CAE window after the GUI repaints."""
    try:
        import pygetwindow as gw
        from PIL import ImageGrab
    except Exception as exc:
        raise ToolError("GUI capture needs pygetwindow + Pillow: %s" % exc)
    import win32process
    deadline = time.time() + 10
    win = None
    while time.time() < deadline:
        wins = [w for w in gw.getWindowsWithTitle("Abaqus/CAE") if "Abaqus/CAE" in w.title]
        if pid is not None:
            for w in wins:
                try:
                    _, wpid = win32process.GetWindowThreadProcessId(w._hWnd)
                except Exception:
                    continue
                if wpid == int(pid):
                    win = w
                    break
        elif wins:
            win = wins[0]
        if win is not None:
            break
        time.sleep(0.5)
    if win is None:
        raise ToolError("Abaqus/CAE window not found")
    try:
        if win.isMinimized:
            win.restore()
        win.activate()
        from pywinauto import Application, mouse
        app = Application(backend="uia").connect(handle=win._hWnd, timeout=10)
        main = app.window(handle=win._hWnd)
        main.set_focus()
        main.type_keys("{ESC}")
        time.sleep(0.6)
        main.type_keys("{ESC}")
        probe = main.capture_as_image()
        px = probe.getpixel((int(probe.width * 0.75), int(probe.height * 0.30)))
        if isinstance(px, int):
            px = (px, px, px)
        if all(c > 200 for c in px[:3]):
            mouse.click(button="left",
                        coords=(win.left + int(win.width * 0.808),
                                win.top + int(win.height * 0.272)))
            time.sleep(1.2)
    except Exception:
        pass
    time.sleep(1.2)
    img = _print_window(win._hWnd)
    if img is None:
        try:
            img = main.capture_as_image()
        except Exception:
            img = None
    if img is None:
        img = ImageGrab.grab(bbox=(win.left, win.top, win.left + win.width, win.top + win.height))
    img.save(out)
    return {"image": out, "rendered": True, "mode": "gui", "focus": focus,
            "size": [img.width, img.height]}


def _render_cae(path, out, view="Iso", field="S", step="", frame=-1,
                component="MISES"):
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    src = path
    if path.lower().endswith(".cae"):
        # The live kernel holds the model database open; render a snapshot.
        src = out + ".src.cae"
        try:
            shutil.copyfile(path, src)
        except Exception as exc:
            return {"image": out, "rendered": False, "error": "copy failed: %s" % exc}
    log = out + ".log"
    script = out + ".py"
    text = (tpl.replace("__LOG__", log)
               .replace("__SRC__", src)
               .replace("__OUT__", out)
               .replace("__VIEW__", view)
               .replace("__FIELD__", field)
               .replace("__STEP__", step)
               .replace("__FRAME__", str(int(frame))))
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(text)
    abaqus = cfg().get("abaqusCmd") or bridge.find_abaqus()
    try:
        subprocess.run(["cmd", "/c", abaqus, "cae", "script=" + script],
                       cwd=os.path.dirname(out), timeout=240,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"image": out, "rendered": os.path.exists(out),
                "error": "CAE render timed out"}
    return {"image": out, "rendered": os.path.exists(out),
            "log": bridge._tail(log, 10) if os.path.exists(log) else ""}


def _plot_matplotlib(path, out, step=None, frame=-1, field="U", component=None):
    dump = out + ".field.json"
    run("result = abq.odb_surface_dump(%s, %s, %s, %s, %s, %s)" % (
        _sqlit(path), _sqlit(dump), _opt(step), int(frame), _sqlit(field),
        _sqlit(component)), timeout=900)
    with open(dump, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    pts = data.get("points") or []
    if not pts:
        raise ToolError("no surface data (mesh missing from ODB?)")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.tri import Triangulation
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    zs = [p[2] for p in pts]
    # Project along the axis with the smallest extent (a readable default view).
    extent = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    drop = extent.index(min(extent))
    if drop == 2:
        u, v = xs, ys
    elif drop == 1:
        u, v = xs, zs
    else:
        u, v = ys, zs
    tris = [t for t in data.get("triangles") or []
            if len(t) == 3 and all(0 <= i < len(pts) for i in t)]
    fig, ax = plt.subplots(figsize=(9, 6), dpi=120)
    tri = Triangulation(u, v, tris)
    tc = ax.tripcolor(tri, data.get("values"), shading="gouraud", cmap="turbo")
    fig.colorbar(tc, ax=ax, shrink=0.8, label="%s %s" % (field, component or ""))
    ratio = max(extent) / max(min(extent), 1e-9)
    ax.set_aspect("equal" if ratio < 8 else "auto")
    ax.set_title("%s  step=%s frame=%.4g" % (field, data.get("step"), data.get("frame") or 0))
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return {"image": out, "rendered": True, "mode": "matplotlib",
            "points": len(pts), "triangles": len(tris)}


def t_plot(args):
    raw = args.get("path")
    out = args.get("out") or os.path.join(_workdir(), "plot.png")
    out = bridge.ascii_path(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if raw and str(raw).lower().endswith(".cae"):
        src = bridge.ascii_path(raw)
        if not os.path.exists(src):
            raise ToolError("CAE file not found: %s" % raw)
        st = sess().status()
        if st.get("mode") == "gui":
            gui = st.get("gui") or {}
            pid = (gui.get("result") or {}).get("pid")
            focus = run("result = abq.focus_model(%s, %s, %s)" % (
                _sqlit(args.get("model")) if args.get("model") else "None",
                _sqlit(args.get("view") or "Iso"),
                _sqlit(args.get("part")) if args.get("part") else "None"), timeout=300)
            return _capture_cae_window(out, focus, pid)
        return _render_cae(src, out, args.get("view") or "Iso",
                           args.get("field") or "S", args.get("step") or "",
                           int(args.get("frame", -1) or -1))
    path = _odb_path(args)
    mode = (args.get("mode") or "auto").lower()
    if mode == "mpl":
        return _plot_matplotlib(path, out, args.get("step"), int(args.get("frame", -1) or -1),
                                args.get("field") or "U", args.get("component"))
    res = _render_cae(path, out, args.get("view") or "Iso",
                      args.get("field") or "S", args.get("step") or "",
                      int(args.get("frame", -1) or -1))
    if res.get("rendered") and os.path.getsize(out) > 5000:
        res["mode"] = "cae"
        return res
    if mode == "cae":
        return res
    mpl = _plot_matplotlib(path, out, args.get("step"), int(args.get("frame", -1) or -1),
                           args.get("field") or "U", args.get("component"))
    mpl["caeAttempt"] = res
    return mpl


def t_export(args):
    kind = (args.get("kind") or "report").lower()
    if kind == "inp":
        name = args.get("name") or "Job-1"
        run("mdb.jobs[%s].writeInput(consistencyChecking=OFF)" % _sqlit(name), timeout=600)
        return {"inp": os.path.join(_workdir(), name + ".inp")}
    if kind == "csv":
        path = _odb_path(args)
        field = args.get("field") or "U"
        out = args.get("out") or os.path.join(_workdir(), "export.csv")
        dump = out + ".field.json"
        run("result = abq.odb_surface_dump(%s, %s, %s, %s, %s, %s)" % (
            _sqlit(path), _sqlit(dump), _opt(args.get("step")),
            int(args.get("frame", -1) or -1), _sqlit(field),
            _opt(args.get("component"))), timeout=900)
        with open(dump, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pts = data.get("points") or []
        vals = data.get("values") or []
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("x,y,z,value\n")
            for p, v in zip(pts, vals):
                fh.write("%.6f,%.6f,%.6f,%.6f\n" % (p[0], p[1], p[2], v))
        return {"csv": out, "rows": len(pts)}
    if kind == "report":
        name = args.get("name") or args.get("path")
        st = bridge.job_status(name, _workdir())
        return {"report": st}
    raise ToolError("kind must be inp|csv|report")


HANDLERS = {
    "abaqus_status": t_status,
    "abaqus_session": t_session,
    "abaqus_finish": t_finish,
    "abaqus_open": t_open,
    "abaqus_save": t_save,
    "abaqus_model_new": t_model_new,
    "abaqus_model_info": t_model_info,
    "abaqus_run_python": t_run_python,
    "abaqus_part": t_part,
    "abaqus_material": t_material,
    "abaqus_section": t_section,
    "abaqus_instance": t_instance,
    "abaqus_set": t_set,
    "abaqus_mesh": t_mesh,
    "abaqus_inspect": t_inspect,
    "abaqus_step": t_step,
    "abaqus_bc": t_bc,
    "abaqus_load": t_load,
    "abaqus_interaction": t_interaction,
    "abaqus_coupling": t_coupling,
    "abaqus_job": t_job,
    "abaqus_odb": t_odb,
    "abaqus_plot": t_plot,
    "abaqus_export": t_export,
}


def dispatch(name, args):
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError("unknown tool %r" % name)
    return handler(args or {})
