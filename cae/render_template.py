# -*- coding: utf-8 -*-
"""One-shot CAE renderer used by the MCP server (GUI process, real OpenGL).

The MCP server writes a concrete copy of this file with SRC/OUT/... substituted,
runs `abaqus cae script=<copy>` and waits for the PNG. Everything is wrapped so
a failure never leaves a modal dialog blocking the desktop.
"""
import os
import sys
import time
import traceback

LOG = r"__LOG__"


def log(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write(str(msg) + "\n")
    except Exception:
        pass


try:
    from abaqus import mdb, session, openMdb
    from abaqusConstants import *

    SRC = r"__SRC__"
    OUT = r"__OUT__"
    VIEW = "__VIEW__"
    FIELD = "__FIELD__"
    STEP = "__STEP__"
    FRAME = __FRAME__

    vp = session.viewports["Viewport: 1"]
    vp.setValues(width=1200, height=800)

    if SRC.lower().endswith(".cae"):
        openMdb(pathName=SRC)
        vp.setValues(displayedObject=mdb.models[list(mdb.models.keys())[0]].rootAssembly)
    else:
        odb = session.openOdb(name=SRC, readOnly=True)
        vp.setValues(displayedObject=odb)
        vp.odbDisplay.display.setValues(plotState=(CONTOURS_ON_DEF,))
        steps = list(odb.steps.keys())
        if steps:
            st = odb.steps[STEP or steps[-1]]
            if len(st.frames):
                vp.odbDisplay.setFrame(step=st.name,
                                       frame=FRAME if FRAME >= 0 else len(st.frames) - 1)
        try:
            vp.odbDisplay.setPrimaryVariable(
                variableLabel=FIELD, outputPosition=INTEGRATION_POINT,
                refinement=(COMPONENT, "MISES"))
        except Exception:
            try:
                vp.odbDisplay.setPrimaryVariable(
                    variableLabel=FIELD, outputPosition=NODAL,
                    refinement=(INVARIANT, "Magnitude"))
            except Exception:
                pass

    try:
        vp.view.setValues(session.views[VIEW])
    except Exception:
        vp.view.setValues(session.views["Iso"])
    vp.view.fitView()
    try:
        vp.forceRefresh()
    except Exception:
        pass
    time.sleep(5)
    session.printToFile(fileName=OUT, format=PNG, canvasObjects=(vp,))
    log("printed=%s exists=%s" % (OUT, os.path.exists(OUT)))
except Exception:
    log(traceback.format_exc())

os._exit(0 if os.path.exists(OUT) else 3)
