# -*- coding: utf-8 -*-
"""Kernel-side helpers for the Abaqus CAD bridge (CPython 3.10, Abaqus 2024+).

Imported by cae_server.py inside Abaqus/CAE. Every function is stateless and
raises Abaqus' own exceptions; the bridge turns them into an error payload so
the agent sees the exact Abaqus message.

Keep this file dependent only on the Abaqus Python API and the standard library.
"""
from __future__ import print_function

import heapq
import math
import os
import sys
import time

import abaqusConstants as _const
import mesh
import odbAccess
from abaqus import mdb, session
from abaqusConstants import *  # noqa: F401,F403

_ODBS = {}

# --------------------------------------------------------------------------- #
# model / cleanup
# --------------------------------------------------------------------------- #


def models():
    return list(mdb.models.keys())


def model(name=None, create=True):
    if not name:
        names = list(mdb.models.keys())
        if not names:
            if not create:
                raise ValueError("no model in the MDB")
            return mdb.Model(name="Model-1")
        return mdb.models[names[0]]
    if name in mdb.models:
        return mdb.models[name]
    if not create:
        raise KeyError("model %r not found; have %r" % (name, models()))
    return mdb.Model(name=name)


def _drop(container, name):
    try:
        if name in container:
            del container[name]
    except Exception:
        pass


def part_info(p):
    return {
        "name": p.name,
        "type": str(p.type),
        "cells": len(p.cells),
        "faces": len(p.faces),
        "edges": len(p.edges),
        "vertices": len(p.vertices),
        "nodes": len(p.nodes),
        "elements": len(p.elements),
        "sets": list(p.sets.keys()),
        "surfaces": list(p.surfaces.keys()),
    }


def model_info(m):
    a = m.rootAssembly
    parts = {}
    for name in m.parts.keys():
        parts[name] = part_info(m.parts[name])
    steps = []
    for sname in m.steps.keys():
        st = m.steps[sname]
        steps.append({"name": sname, "type": type(st).__name__,
                      "previous": getattr(st, "previous", None)})
    insts = {}
    for iname in a.instances.keys():
        inst = a.instances[iname]
        insts[iname] = {"part": inst.partName}
    return {
        "model": m.name,
        "parts": parts,
        "instances": insts,
        "materials": list(m.materials.keys()),
        "sections": list(m.sections.keys()),
        "steps": steps,
        "loads": list(m.loads.keys()),
        "bcs": list(m.boundaryConditions.keys()),
        "interactions": list(m.interactions.keys()),
        "sets": list(a.sets.keys()),
        "surfaces": list(a.surfaces.keys()),
        "jobs": list(mdb.jobs.keys()),
        "units": "user-defined (Abaqus has no unit system)",
    }


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #


def _new_part(m, name):
    _drop(m.parts, name)
    return m.Part(name=name, dimensionality=THREE_D, type=DEFORMABLE_BODY)


def part_box(m, name, L, W, H):
    _drop(m.parts, name)
    s = m.ConstrainedSketch(name="__profile__", sheetSize=max(L, W, H) * 2.0 + 1.0)
    s.rectangle(point1=(0.0, 0.0), point2=(float(L), float(W)))
    p = _new_part(m, name)
    p.BaseSolidExtrude(sketch=s, depth=float(H))
    p.Set(cells=p.cells, name="ALL")
    return p


def part_cylinder(m, name, radius, height):
    _drop(m.parts, name)
    r = float(radius)
    s = m.ConstrainedSketch(name="__profile__", sheetSize=2.0 * r * 2.0 + 1.0)
    s.CircleByCenterPerimeter(center=(0.0, 0.0), point1=(r, 0.0))
    p = _new_part(m, name)
    p.BaseSolidExtrude(sketch=s, depth=float(height))
    p.Set(cells=p.cells, name="ALL")
    return p


def _revolve_profile(m, name, profile, angle=360.0, close=None):
    pts = [(float(r), float(z)) for r, z in profile]
    if len(pts) < 2:
        raise ValueError("revolve profile needs at least 2 [r,z] points")
    if any(r < 0.0 for r, _ in pts):
        raise ValueError("revolve profile radii must be >= 0")
    if close is None:
        close = not (abs(pts[0][0]) < 1e-9 and abs(pts[-1][0]) < 1e-9)
    size = max(max(abs(r) for r, _ in pts), max(abs(z) for _, z in pts)) * 2.0 + 1.0
    sk = m.ConstrainedSketch(name="__profile__", sheetSize=size)
    # A solid revolve requires a construction line that defines the axis.
    sk.ConstructionLine(point1=(0.0, 0.0), point2=(0.0, 1.0))
    for i in range(len(pts)):
        sk.Line(point1=pts[i], point2=pts[(i + 1) % len(pts)])
    part = _new_part(m, name)
    part.BaseSolidRevolve(sketch=sk, angle=float(angle))
    part.Set(cells=part.cells, name="ALL")
    return part


def part_revolve(m, name, profile, angle=360.0, close=None):
    """Revolve a [r,z] profile around the sketch Y axis (z = height)."""
    return _revolve_profile(m, name, profile, angle, close)


def part_cone(m, name, r1, r2, height):
    r1, r2, h = float(r1), float(r2), float(height)
    pts = []
    if r1 > 0:
        pts.append((0.0, 0.0))
    pts.append((r1, 0.0))
    pts.append((r2, h))
    if r2 > 0:
        pts.append((0.0, h))
    return _revolve_profile(m, name, pts, 360.0, close=False)


def part_tube(m, name, outer, inner, height):
    outer, inner, h = float(outer), float(inner), float(height)
    if inner <= 0 or outer <= inner:
        raise ValueError("tube needs outer > inner > 0")
    return _revolve_profile(m, name, [(inner, 0.0), (outer, 0.0),
                                      (outer, h), (inner, h)], 360.0, close=True)


def inspect_model(m, include_mesh=True):
    """Compact inventory of sets/surfaces/loads/BCs/constraints/steps/mesh."""
    a = m.rootAssembly
    sets = []
    for n in a.sets.keys():
        st = a.sets[n]
        sets.append({"name": n, "faces": len(getattr(st, "faces", ()) or ()),
                     "edges": len(getattr(st, "edges", ()) or ()),
                     "nodes": len(getattr(st, "nodes", ()) or ())})
    surfaces = [{"name": n, "faces": len(a.surfaces[n].faces)}
                for n in a.surfaces.keys()]
    loads = [{"name": n, "type": type(m.loads[n]).__name__,
              "step": getattr(m.loads[n], "createStepName", None)}
             for n in m.loads.keys()]
    bcs = [{"name": n, "type": type(m.boundaryConditions[n]).__name__,
            "step": getattr(m.boundaryConditions[n], "createStepName", None)}
           for n in m.boundaryConditions.keys()]
    constraints = [{"name": n, "type": type(m.constraints[n]).__name__}
                   for n in m.constraints.keys()]
    steps = [{"name": n, "type": type(m.steps[n]).__name__,
              "nlgeom": bool(getattr(m.steps[n], "nlgeom", False))}
             for n in m.steps.keys()]
    out = {"model": m.name, "parts": list(m.parts.keys()),
           "instances": list(a.instances.keys()), "sets": sets,
           "surfaces": surfaces, "loads": loads, "bcs": bcs,
           "constraints": constraints, "steps": steps,
           "materials": list(m.materials.keys()),
           "sections": list(m.sections.keys())}
    if include_mesh:
        mesh = {}
        for pn in m.parts.keys():
            pt = m.parts[pn]
            types = {}
            count = 0
            for el in pt.elements:
                count += 1
                t = str(el.type)
                types[t] = types.get(t, 0) + 1
                if count >= 20000:
                    break
            mesh[pn] = {"nodes": len(pt.nodes), "elements": len(pt.elements),
                        "types": types, "sampled": count < len(pt.elements)}
        out["mesh"] = mesh
    return out


def part_shell(m, name, L, W):
    _drop(m.parts, name)
    s = m.ConstrainedSketch(name="__profile__", sheetSize=max(L, W) * 2.0 + 1.0)
    s.rectangle(point1=(0.0, 0.0), point2=(float(L), float(W)))
    p = _new_part(m, name)
    p.BaseShell(sketch=s)
    p.Set(faces=p.faces, name="ALL")
    return p


def part_polygon(m, name, points, depth):
    _drop(m.parts, name)
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        raise ValueError("polygon needs at least 3 points")
    s = m.ConstrainedSketch(name="__profile__", sheetSize=1000.0)
    for i in range(len(pts)):
        s.Line(point1=pts[i], point2=pts[(i + 1) % len(pts)])
    p = _new_part(m, name)
    p.BaseSolidExtrude(sketch=s, depth=float(depth))
    p.Set(cells=p.cells, name="ALL")
    return p


def part_import(m, name, path):
    _drop(m.parts, name)
    if not os.path.exists(path):
        raise IOError("geometry file not found: %s" % path)
    p = m.PartFromGeometryFile(name=name, geometryFile=path, dimensionality=THREE_D,
                               type=DEFORMABLE_BODY, combine=False)
    if len(p.cells):
        p.Set(cells=p.cells, name="ALL")
    else:
        p.Set(faces=p.faces, name="ALL")
    return p


# --------------------------------------------------------------------------- #
# material / section / assembly
# --------------------------------------------------------------------------- #


def material(m, name, density=None, elastic=None, plastic=None, expansion=None):
    if name in m.materials:
        mat = m.materials[name]
    else:
        mat = m.Material(name=name)
    if density is not None:
        mat.Density(table=((float(density),),))
    if elastic is not None:
        e, nu = float(elastic[0]), float(elastic[1])
        mat.Elastic(table=((e, nu),))
    if plastic:
        mat.Plastic(table=tuple(tuple(float(x) for x in row) for row in plastic))
    if expansion is not None:
        mat.Expansion(table=((float(expansion),),))
    return mat


def section(m, name, material, kind="solid", thickness=None):
    _drop(m.sections, name)
    if kind == "shell":
        return m.HomogeneousShellSection(name=name, material=material,
                                         thickness=float(thickness or 1.0))
    return m.HomogeneousSolidSection(name=name, material=material, thickness=None)


def assign_section(p, section_name, kind="auto"):
    if kind == "auto":
        kind = "shell" if len(p.cells) == 0 else "solid"
    if kind == "shell":
        if "ALL" not in p.sets:
            p.Set(faces=p.faces, name="ALL")
        region = p.sets["ALL"]
    else:
        if "ALL" not in p.sets:
            p.Set(cells=p.cells, name="ALL")
        region = p.sets["ALL"]
    p.SectionAssignment(region=region, sectionName=section_name, offset=0.0,
                        offsetType=MIDDLE_SURFACE, thicknessAssignment=FROM_SECTION)
    return p


def instance(m, part_name, name=None, translate=(0.0, 0.0, 0.0), rotate=None):
    a = m.rootAssembly
    pname = part_name
    iname = name or (pname + "-1")
    if iname in a.instances:
        return a.instances[iname]
    if pname not in m.parts:
        raise KeyError("part %r not found; have %r" % (pname, list(m.parts.keys())))
    inst = a.Instance(name=iname, part=m.parts[pname], dependent=ON)
    if any(float(v) != 0.0 for v in translate):
        a.translate(instanceList=(iname,), vector=tuple(float(v) for v in translate))
    if rotate:
        axis_point = tuple(float(v) for v in rotate.get("point", (0.0, 0.0, 0.0)))
        axis_dir = tuple(float(v) for v in rotate.get("axis", (0.0, 0.0, 1.0)))
        angle = float(rotate.get("angle", 0.0))
        a.rotate(instanceList=(iname,), axisPoint=axis_point,
                 axisDirection=axis_dir, angle=angle)
    return inst


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


def _center(g):
    try:
        return tuple(g.pointOn[0])
    except Exception:
        return None


def pick(container, spec):
    """Turn a compact selector into a tuple of Abaqus geometry objects.

    spec: {"all": true} | {"at": [[x,y,z], ...]} | {"box": [[lo],[hi]], "tol": t}
    """
    if spec is None or spec.get("all"):
        return container
    if "at" in spec:
        pts = spec["at"]
        if pts and isinstance(pts[0], (int, float)):
            pts = [pts]
        return container.findAt(tuple(tuple(float(v) for v in p) for p in pts))
    if "box" in spec:
        lo, hi = spec["box"]
        lo = [float(v) for v in lo]
        hi = [float(v) for v in hi]
        tol = float(spec.get("tol", 1e-4))
        pts = []
        for g in container:
            c = _center(g)
            if c is None:
                continue
            if all(lo[i] - tol <= c[i] <= hi[i] + tol for i in range(3)):
                pts.append(tuple(c))
        if not pts:
            raise ValueError("no geometry centre inside box %r" % (spec["box"],))
        return container.findAt(tuple(pts))
    raise ValueError("unsupported selector: %r" % (spec,))


def make_set(owner, name, kind, spec, instance=None):
    src = owner
    if instance and hasattr(owner, "instances"):
        src = owner.instances[instance]
    geom = pick(getattr(src, kind), spec)
    _drop(owner.sets, name)
    owner.Set(name=name, **{kind: geom})
    return owner.sets[name]


def make_surface(owner, name, spec, instance=None):
    src = None
    if instance:
        src = owner.instances[instance]
    elif len(owner.instances):
        src = owner.instances[list(owner.instances.keys())[0]]
    if src is None:
        raise ValueError("assembly has no instance to select surface faces from")
    geom = pick(src.faces, spec)
    _drop(owner.surfaces, name)
    owner.Surface(name=name, side1Faces=geom)
    return owner.surfaces[name]


# --------------------------------------------------------------------------- #
# step / bc / load / interaction
# --------------------------------------------------------------------------- #


def step(m, name, kind="static", previous="Initial", nlgeom=False, time_period=1.0,
         initial_inc=None, min_inc=None, max_inc=None, max_inc_num=100, num_eigen=10):
    _drop(m.steps, name)
    st = None
    if kind == "static":
        kw = {"name": name, "previous": previous,
              "nlgeom": ON if nlgeom else OFF, "timePeriod": float(time_period)}
        if initial_inc is not None:
            kw["initialInc"] = float(initial_inc)
        if min_inc is not None:
            kw["minInc"] = float(min_inc)
        if max_inc is not None:
            kw["maxInc"] = float(max_inc)
        if max_inc_num:
            kw["maxNumInc"] = int(max_inc_num)
        st = m.StaticStep(**kw)
    elif kind == "frequency":
        st = m.FrequencyStep(name=name, previous=previous, numEigen=int(num_eigen),
                             eigensolver=LANCZOS)
    elif kind == "buckle":
        st = m.BuckleStep(name=name, previous=previous, numEigen=int(num_eigen),
                          vectors=max(int(num_eigen) * 2, 2))
    elif kind == "explicit":
        st = m.ExplicitDynamicsStep(name=name, previous=previous,
                                    timePeriod=float(time_period))
    else:
        raise ValueError("unknown step kind %r (static|frequency|buckle|explicit)" % kind)
    return st.name


def bc(m, name, region, kind="encastre", step="Initial", values=None):
    kind = (kind or "encastre").lower()
    _drop(m.boundaryConditions, name)
    if kind == "encastre":
        m.EncastreBC(name=name, createStepName=step, region=region)
    elif kind == "pinned":
        m.PinnedBC(name=name, createStepName=step, region=region)
    elif kind in ("xsymm", "ysymm", "zsymm"):
        getattr(m, kind.upper() + "BC")(name=name, createStepName=step, region=region)
    elif kind in ("displacement", "velocity"):
        vals = dict(values or {})
        kw = {}
        for i in range(1, 7):
            key = "u%d" % i
            if key in vals and vals[key] is not None:
                kw[key] = float(vals[key])
        cls = m.DisplacementBC if kind == "displacement" else m.VelocityBC
        cls(name=name, createStepName=step, region=region, **kw)
    else:
        raise ValueError("unknown bc kind %r" % kind)
    return name


def load(m, name, kind, region, step, values=None, amplitude=None):
    kind = (kind or "force").lower()
    vals = dict(values or {})
    _drop(m.loads, name)
    amp = amplitude or UNSET
    if kind in ("force", "concentrated_force", "cf"):
        m.ConcentratedForce(name=name, createStepName=step, region=region,
                            cf1=float(vals.get("cf1", 0.0)),
                            cf2=float(vals.get("cf2", 0.0)),
                            cf3=float(vals.get("cf3", 0.0)), amplitude=amp)
    elif kind == "pressure":
        m.Pressure(name=name, createStepName=step, region=region,
                   magnitude=float(vals.get("magnitude", 0.0)), amplitude=amp)
    elif kind == "gravity":
        m.Gravity(name=name, createStepName=step, region=region,
                  comp1=float(vals.get("comp1", 0.0)),
                  comp2=float(vals.get("comp2", 0.0)),
                  comp3=float(vals.get("comp3", -1.0)),
                  magnitude=float(vals.get("magnitude", 9810.0)), amplitude=amp)
    elif kind == "moment":
        m.Moment(name=name, createStepName=step, region=region,
                 cm1=float(vals.get("cm1", 0.0)),
                 cm2=float(vals.get("cm2", 0.0)),
                 cm3=float(vals.get("cm3", 0.0)), amplitude=amp)
    elif kind == "body_force":
        m.BodyForce(name=name, createStepName=step, region=region,
                    bf1=float(vals.get("bf1", 0.0)),
                    bf2=float(vals.get("bf2", 0.0)),
                    bf3=float(vals.get("bf3", 0.0)), amplitude=amp)
    elif kind == "velocity":
        # Initial velocity (Explicit): predefined field, step must be Initial.
        m.Velocity(name=name, region=region,
                   velocity1=float(vals.get("v1", 0.0)),
                   velocity2=float(vals.get("v2", 0.0)),
                   velocity3=float(vals.get("v3", 0.0)))
    else:
        raise ValueError("unknown load kind %r" % kind)
    return name


def contact_property(m, name, friction=0.0):
    _drop(m.interactionProperties, name)
    cp = m.ContactProperty(name=name)
    cp.NormalBehavior(pressureOverclosure=HARD, allowSeparation=ON)
    if friction and float(friction) > 0.0:
        cp.TangentialBehavior(formulation=PENALTY, directionality=ISOTROPIC,
                              table=((float(friction),),),
                              maximumElasticSlip=FRACTION, fraction=0.005)
    else:
        cp.TangentialBehavior(formulation=FRICTIONLESS)
    return name


def contact_pair(m, name, step, main, secondary, friction=0.0, kind="std"):
    prop = name + "-Prop"
    contact_property(m, prop, friction)
    _drop(m.interactions, name)
    if kind == "exp":
        m.SurfaceToSurfaceContactExp(name=name, createStepName=step, main=main,
                                     secondary=secondary, sliding=FINITE,
                                     interactionProperty=prop,
                                     mechanicalConstraint=KINEMATIC)
    else:
        m.SurfaceToSurfaceContactStd(name=name, createStepName=step, main=main,
                                     secondary=secondary, sliding=FINITE,
                                     interactionProperty=prop, adjustMethod=NONE)
    return name


def contact_general(m, name, step, kind="std", friction=0.0):
    _drop(m.interactions, name)
    if kind == "exp":
        m.ContactExp(name=name, createStepName=step)
    else:
        # General contact (Standard) is defined in the Initial step and stays
        # active for every subsequent step.
        m.ContactStd(name=name, createStepName="Initial")
    return name


def reference_point(m, name, point):
    """Create an assembly reference point and a one-item set for it."""
    a = m.rootAssembly
    rp = a.ReferencePoint(point=tuple(float(v) for v in point))
    feat = a.referencePoints[rp.id]
    setname = name + "-RP"
    _drop(a.sets, setname)
    a.Set(name=setname, referencePoints=(feat,))
    return setname


def coupling(m, name, surface, rp_set, kind="kinematic", dofs=(1, 2, 3)):
    """Couple a surface to a reference-point set (kinematic or distributing)."""
    _drop(m.constraints, name)
    kw = {
        "name": name,
        "controlPoint": m.rootAssembly.sets[rp_set],
        "surface": surface,
        "influenceRadius": WHOLE_SURFACE,
        "couplingType": KINEMATIC if str(kind).lower().startswith("kin") else DISTRIBUTING,
    }
    if str(kind).lower().startswith("dist"):
        kw["weightingMethod"] = UNIFORM
    for i in range(1, 4):
        kw["u%d" % i] = ON if i in dofs else OFF
        kw["ur%d" % i] = ON if (i + 3) in dofs else OFF
    m.Coupling(**kw)
    return name


def tie(m, name, step, main, secondary):
    _drop(m.constraints, name)
    m.Tie(name=name, main=main, secondary=secondary,
          positionToleranceMethod=COMPUTED, adjust=ON, tieRotations=ON, thickness=ON)
    return name


# --------------------------------------------------------------------------- #
# mesh / job
# --------------------------------------------------------------------------- #


def mesh_part(p, size, elem_code=None, deviation=0.1, min_size=0.1,
              technique=None, shape=None):
    if technique or shape:
        kw = {}
        if technique:
            kw["technique"] = getattr(_const, str(technique).upper())
        if shape:
            kw["elemShape"] = getattr(_const, str(shape).upper())
        p.setMeshControls(p.cells, **kw)
    p.seedPart(size=float(size), deviationFactor=float(deviation),
               minSizeFactor=float(min_size))
    if elem_code:
        code = getattr(_const, elem_code, None)
        if code is None:
            raise ValueError("unknown element code %r" % elem_code)
        region = p.cells if len(p.cells) else p.faces
        et = mesh.ElemType(elemCode=code, elemLibrary=STANDARD)
        p.setElementType(regions=(region,), elemTypes=(et,))
    p.generateMesh()
    return {"part": p.name, "nodes": len(p.nodes), "elements": len(p.elements),
            "elem_code": elem_code}


def job(m, name, model_name, cpus=1, description=""):
    _drop(mdb.jobs, name)
    j = mdb.Job(name=name, model=model_name, numCpus=int(cpus),
                numDomains=int(cpus), description=description or name)
    return j.name


def job_write_input(name, consistency=False):
    if name not in mdb.jobs:
        raise KeyError("job %r not found" % name)
    mdb.jobs[name].writeInput(consistencyChecking=OFF if not consistency else ON)
    return name + ".inp"


def focus_model(model_name=None, view="Iso", part_name=None):
    """Display a model in the live GUI viewport (no image is written here)."""
    vp = session.viewports["Viewport: 1"]
    m = model(model_name)
    obj = None
    if part_name and part_name in m.parts:
        obj = m.parts[part_name]
    elif m.parts:
        obj = m.parts[list(m.parts.keys())[0]]
    if obj is None:
        obj = m.rootAssembly
    vp.setValues(displayedObject=obj)
    try:
        vp.view.setValues(session.views[view])
    except Exception:
        vp.view.setValues(session.views["Iso"])
    vp.view.fitView()
    return {"model": m.name, "view": view, "displayed": True}


# --------------------------------------------------------------------------- #
# ODB post-processing
# --------------------------------------------------------------------------- #


def odb_open(path, read_only=True):
    path = os.path.abspath(path)
    if path in _ODBS:
        return _ODBS[path]
    odb = odbAccess.openOdb(path=path, readOnly=read_only)
    _ODBS[path] = odb
    return odb


def odb_close(path=None):
    if path is None:
        for key in list(_ODBS.keys()):
            try:
                _ODBS[key].close()
            except Exception:
                pass
        _ODBS.clear()
        return True
    path = os.path.abspath(path)
    odb = _ODBS.pop(path, None)
    if odb:
        odb.close()
    return True


def _step_name(odb, step=None):
    names = list(odb.steps.keys())
    if not names:
        raise ValueError("ODB has no steps")
    if step is None or step == "":
        return names[-1]
    if isinstance(step, int) or (isinstance(step, str) and step.strip().lstrip("-").isdigit()):
        idx = int(step)
        # Integers are 1-based (step=1 -> first step); 0 is accepted as first.
        if 1 <= idx <= len(names):
            return names[idx - 1]
        if idx == 0:
            return names[0]
        if -len(names) <= idx < 0:
            return names[idx]
    text = str(step)
    if text in odb.steps:
        return text
    raise KeyError("step %r not found; available steps: %r (an integer is a 1-based step number)"
                   % (step, names))


def _frame(st, frame):
    if not len(st.frames):
        raise ValueError("step %r has no frames" % st.name)
    idx = int(frame)
    return st.frames[idx] if idx >= 0 else st.frames[len(st.frames) + idx]


def _repo_keys(obj, name):
    try:
        return list(getattr(obj, name).keys())[:50]
    except Exception:
        return []


def odb_summary(path):
    odb = odb_open(path)
    steps = []
    for sname in odb.steps.keys():
        st = odb.steps[sname]
        hist = []
        for rname in st.historyRegions.keys():
            reg = st.historyRegions[rname]
            for oname in reg.historyOutputs.keys():
                hist.append(rname + ":" + oname)
        item = {"name": sname, "frames": len(st.frames), "history": hist[:20]}
        if len(st.frames):
            fr = st.frames[-1]
            item["time"] = fr.frameValue
            item["fields"] = sorted(fr.fieldOutputs.keys())
        steps.append(item)
    return {
        "path": path,
        "steps": steps,
        "instances": list(odb.rootAssembly.instances.keys()),
        "parts": list(odb.parts.keys()),
        "assembly_sets": _repo_keys(odb.rootAssembly, "nodeSets")
        + _repo_keys(odb.rootAssembly, "elementSets")
        + _repo_keys(odb.rootAssembly, "surfaces"),
    }


_COMPONENT_ALIASES = {
    "MAGNITUDE": "Magnitude", "MAG": "Magnitude", "MISES": "MISES",
    "MAXPRINCIPAL": "Max. Principal", "MINPRINCIPAL": "Min. Principal",
    "MAXINPLANE": "Max. In-Plane Principal", "MININPLANE": "Min. In-Plane Principal",
}


def _component(fo, component):
    if component is None:
        return fo
    wanted = str(component)
    candidates = [wanted]
    alias = _COMPONENT_ALIASES.get(wanted.upper())
    if alias:
        candidates.insert(0, alias)
    candidates += [wanted.upper(), wanted.lower()]
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        try:
            return fo.getScalarField(componentLabel=cand)
        except Exception:
            pass
        try:
            return fo.getSubset(component=cand)
        except Exception:
            pass
    return fo


def _value_magnitude(v):
    d = v.data
    if isinstance(d, (int, float)):
        return float(d)
    try:
        return float(v.magnitude)
    except Exception:
        return math.sqrt(sum(float(x) * float(x) for x in d))


def odb_extrema(path, step=None, frame=-1, field="S", component=None, top=5):
    odb = odb_open(path)
    sname = _step_name(odb, step)
    st = odb.steps[sname]
    fr = _frame(st, frame)
    if field not in fr.fieldOutputs:
        raise KeyError("field %r not in frame; have %r"
                       % (field, sorted(fr.fieldOutputs.keys())))
    fo = fr.fieldOutputs[field]
    if component:
        fo = _component(fo, component)
    heap = []
    count = 0
    for v in fo.values:
        count += 1
        signed = _value_magnitude(v)
        mag = abs(signed)
        item = (mag, signed, v)
        if len(heap) < int(top):
            heapq.heappush(heap, item)
        elif mag > heap[0][0]:
            heapq.heapreplace(heap, item)
    out = []
    for mag, signed, v in sorted(heap, key=lambda t: -t[0]):
        row = {"value": round(signed, 6), "magnitude": round(mag, 6),
               "node": getattr(v, "nodeLabel", None),
               "element": getattr(v, "elementLabel", None),
               "ip": getattr(v, "integrationPoint", None)}
        try:
            inst = v.instance
            if inst is not None and row["node"] is not None:
                row["instance"] = inst.name
                row["xyz"] = [round(float(c), 4) for c in inst.nodes[row["node"]].coordinates]
        except Exception:
            pass
        out.append(row)
    return {"step": sname, "frame": fr.frameValue, "field": field,
            "component": component, "count": count, "positions": out}


def odb_probe(path, node, step=None, frame=-1, fields=None):
    odb = odb_open(path)
    sname = step or list(odb.steps.keys())[-1]
    fr = _frame(odb.steps[sname], frame)
    names = fields or [n for n in fr.fieldOutputs.keys() if n in
                       ("U", "S", "RF", "PE", "PEEQ", "V", "A", "NT", "CF")]
    out = {}
    for fname in names:
        try:
            fo = fr.fieldOutputs[fname].getSubset(position=NODAL)
        except Exception:
            fo = fr.fieldOutputs[fname]
        for v in fo.values:
            if getattr(v, "nodeLabel", None) == int(node):
                d = v.data
                try:
                    out[fname] = [round(float(x), 6) for x in d]
                except TypeError:
                    out[fname] = round(float(d), 6)
                break
    return {"node": int(node), "step": sname, "frame": fr.frameValue, "values": out}


def odb_history(path, step=None, output=None):
    odb = odb_open(path)
    sname = _step_name(odb, step)
    st = odb.steps[sname]
    out = {}
    for rname in st.historyRegions.keys():
        reg = st.historyRegions[rname]
        for oname in reg.historyOutputs.keys():
            if output and oname != output:
                continue
            data = reg.historyOutputs[oname].data
            out[rname + ":" + oname] = [[round(float(a), 8), round(float(b), 8)]
                                        for a, b in data[:500]]
    return {"step": sname, "history": out}


def _array(repo):
    try:
        return [repo[k] for k in repo.keys()]
    except Exception:
        return list(repo)


def odb_surface(path, step=None, frame=-1, field="U", component=None, instance=None):
    """Extract an exterior triangle mesh + nodal values for host-side plotting.

    Solid elements contribute their boundary faces (faces used once); shells
    contribute their own elements. Only node-based values are returned, so
    integration-point output is averaged to nodes by Abaqus.
    """
    odb = odb_open(path)
    sname = step or list(odb.steps.keys())[-1]
    fr = _frame(odb.steps[sname], frame)
    if field not in fr.fieldOutputs:
        raise KeyError("field %r not available" % field)
    fo = fr.fieldOutputs[field]
    try:
        fo = fo.getSubset(position=NODAL)
    except Exception:
        pass
    if component:
        fo = _component(fo, component)

    insts = [n for n in odb.rootAssembly.instances.keys()
             if len(odb.rootAssembly.instances[n].elements)]
    if not insts:
        raise ValueError("ODB has no instance with element data")
    if instance:
        inst_name = instance
    else:
        inst_name = max(insts, key=lambda n: len(odb.rootAssembly.instances[n].elements))
    inst = odb.rootAssembly.instances[inst_name]
    nodes = {}
    for nd in _array(inst.nodes):
        nodes[nd.label] = (float(nd.coordinates[0]), float(nd.coordinates[1]),
                           float(nd.coordinates[2]))
    node_vals = {}
    for v in fo.values:
        nl = getattr(v, "nodeLabel", None)
        if nl is not None:
            node_vals[nl] = _value_magnitude(v)

    # Boundary faces: a face shared by two elements is interior, drop it.
    faces = {}
    tri = []
    for el in _array(inst.elements):
        conn = list(el.connectivity)
        etype = str(el.type).upper()
        if etype.startswith("S") or etype.startswith("M3D"):
            for i in range(1, len(conn) - 1):
                tri.append((conn[0], conn[i], conn[i + 1]))
            continue
        if etype.startswith("C3D8"):
            quads = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
                     (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
        elif etype.startswith("C3D6"):
            quads = [(0, 1, 2), (3, 5, 4), (0, 3, 4, 1), (1, 4, 5, 2), (2, 5, 3, 0)]
        elif etype.startswith("C3D4") or etype.startswith("C3D10"):
            quads = [(0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0)]
        else:
            continue
        for q in quads:
            key = tuple(sorted(conn[i] for i in q))
            if key in faces:
                faces[key] = None
            else:
                faces[key] = [conn[i] for i in q]
    for key, q in faces.items():
        if q is None:
            continue
        for i in range(1, len(q) - 1):
            tri.append((q[0], q[i], q[i + 1]))

    pts = []
    vals = []
    index = {}
    for t in tri:
        for nl in t:
            if nl not in index:
                index[nl] = len(pts)
                pts.append(nodes.get(nl, (0.0, 0.0, 0.0)))
                vals.append(node_vals.get(nl, 0.0))
    tris = [[index[a], index[b], index[c]] for a, b, c in tri]
    return {"points": pts, "values": vals, "triangles": tris,
            "field": field, "component": component, "step": sname,
            "frame": fr.frameValue, "instance": inst_name}


def odb_surface_dump(path, out, step=None, frame=-1, field="U", component=None,
                     instance=None):
    """Write the plotting payload to `out` as JSON; return a small summary."""
    data = odb_surface(path, step, frame, field, component, instance)
    import json
    with open(out, "w") as fh:
        json.dump(data, fh)
    return {"file": out, "points": len(data["points"]),
            "triangles": len(data["triangles"]), "instance": data["instance"],
            "step": data["step"], "frame": data["frame"]}
