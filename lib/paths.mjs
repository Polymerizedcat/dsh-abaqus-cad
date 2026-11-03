// Resolve every path this bundle needs at runtime from its own location plus a
// small JSON file under ~/.dsh. Nothing is pinned to one machine: the Abaqus
// launcher is discovered from PATH or the default SIMULIA layout.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import { detectAbaqus } from "./detect.mjs";

export const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const PKG = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, "package.json"), "utf8"));

export const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
export const CONFIG_FILE = path.join(DSH_HOME, "abaqus-cad.json");

export function exists(p) {
  try {
    return fs.existsSync(p);
  } catch {
    return false;
  }
}

function defaultWorkDir() {
  if (process.env.ABAQUS_MCP_WORKDIR) return process.env.ABAQUS_MCP_WORKDIR;
  for (const drive of ["D:", "E:", "F:"]) {
    if (exists(drive + "\\")) return path.join(drive + "\\", "Abaqus", "work");
  }
  return path.join(os.homedir(), "Abaqus", "work");
}

export function findAbaqus() {
  if (process.env.ABAQUS_CMD && exists(process.env.ABAQUS_CMD)) return process.env.ABAQUS_CMD;
  const cfg = readConfig();
  if (cfg.abaqusCmd && exists(cfg.abaqusCmd)) return cfg.abaqusCmd;
  for (const drive of ["C:", "D:", "E:"]) {
    const cand = path.join(drive + "\\", "SIMULIA", "Commands", "abaqus.bat");
    if (exists(cand)) return cand;
  }
  const detected = detectAbaqus();
  return detected.command || "abaqus";
}

// Host-side Python for the MCP server itself (stdlib only, 3.8+). Abaqus ships
// its own interpreter under tools/SMApy, but that one is only used inside CAE.
export function findPython() {
  const cfg = readConfig();
  const explicit = process.env.ABAQUS_MCP_PYTHON || cfg.python;
  if (explicit && exists(explicit)) return explicit;
  for (const cand of [
    "C:\\Python312\\python.exe",
    "C:\\Python311\\python.exe",
    path.join(os.homedir(), "AppData", "Local", "Programs", "Python", "Python312", "python.exe"),
    path.join(os.homedir(), "AppData", "Local", "Programs", "Python", "Python311", "python.exe"),
  ]) {
    if (exists(cand)) return cand;
  }
  const res = spawnSync("where", ["python"], { encoding: "utf8", timeout: 5000 });
  if (res.status === 0 && res.stdout.trim()) return res.stdout.trim().split(/\r?\n/)[0];
  return "python";
}

export function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
  } catch {
    return {};
  }
}

export function writeConfig(cfg) {
  fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2) + "\n", "utf8");
}

export function resolveLayout(overrides = {}) {
  const cfg = readConfig();
  const detection = overrides.detection || null;
  const workDir = path.resolve(
    overrides.workDir || process.env.ABAQUS_MCP_WORKDIR || cfg.workDir || defaultWorkDir(),
  );
  return {
    workDir,
    detection,
    python: overrides.python || findPython(),
    abaqusCmd: overrides.abaqusCmd || (detection && detection.command) || findAbaqus(),
    serverScript: path.join(PKG_ROOT, "mcp-server", "server.py"),
    skillSrc: path.join(PKG_ROOT, "skills", "abaqus-fea"),
    skillDest: path.join(DSH_HOME, "skills", "abaqus-fea"),
    caeDir: path.join(PKG_ROOT, "cae"),
    cfg,
  };
}

export function ensureConfig(layout) {
  const cfg = readConfig();
  let changed = false;
  if (!cfg.workDir) {
    cfg.workDir = layout.workDir;
    changed = true;
  }
  if (!cfg.abaqusCmd) {
    cfg.abaqusCmd = layout.abaqusCmd;
    changed = true;
  }
  if (!cfg.port) {
    cfg.port = 48901;
    changed = true;
  }
  if (!cfg.mode) {
    cfg.mode = "headless";
    changed = true;
  }
  if (layout.detection && layout.detection.found) {
    const d = layout.detection;
    const snapshot = {
      abaqusVersion: d.version,
      abaqusPython: d.python ? d.python.version : null,
      abaqusSolver: d.solver,
      abaqusInstallRoot: d.installRoot,
      minVersion: d.minVersion,
    };
    for (const [k, v] of Object.entries(snapshot)) {
      if (cfg[k] !== v) {
        cfg[k] = v;
        changed = true;
      }
    }
  }
  if (changed) writeConfig(cfg);
  return cfg;
}
