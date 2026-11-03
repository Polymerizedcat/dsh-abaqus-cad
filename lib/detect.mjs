// Detect a local Abaqus installation and its version.
//
// Abaqus 2024+ ships CPython 3.10 and is the supported baseline; older releases
// are rejected by default (override with ABAQUS_ALLOW_UNSUPPORTED=1). This file
// is used by the plugin load path and by `bin/dsh-abaqus-cad.mjs doctor`, so it
// must not import anything from the dsh runtime.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const MIN_VERSION = Number(process.env.ABAQUS_MIN_VERSION || 2024);
export const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
export const CONFIG_FILE = path.join(DSH_HOME, "abaqus-cad.json");

function exists(p) {
  try {
    return fs.existsSync(p);
  } catch {
    return false;
  }
}

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
  } catch {
    return {};
  }
}

function whereAbaqus() {
  const res = spawnSync("where", ["abaqus"], { encoding: "utf8", timeout: 5000 });
  if (res.status === 0 && res.stdout.trim()) {
    return res.stdout.trim().split(/\r?\n/)[0].trim();
  }
  return null;
}

// All %SIMULIA%\EstProducts\<year>\win_b64 trees on the fixed drives.
export function listInstallations() {
  const out = [];
  for (const drive of ["C:", "D:", "E:", "F:"]) {
    const root = path.join(drive + "\\", "SIMULIA", "EstProducts");
    if (!exists(root)) continue;
    let names = [];
    try {
      names = fs.readdirSync(root);
    } catch {
      continue;
    }
    for (const name of names) {
      if (!/^\d{4}$/.test(name)) continue;
      const win = path.join(root, name, "win_b64");
      if (!exists(win)) continue;
      const bin = path.join(win, "code", "bin");
      out.push({
        version: Number(name),
        root: win,
        bin,
        solver: exists(path.join(bin, "pre.exe")) && exists(path.join(bin, "standard.exe")),
      });
    }
  }
  out.sort((a, b) => b.version - a.version);
  return out;
}

function embeddedPython(root) {
  const smapy = path.join(root, "tools", "SMApy");
  if (!exists(smapy)) return null;
  let dirs = [];
  try {
    dirs = fs.readdirSync(smapy).filter((d) => d.startsWith("python"));
  } catch {
    return null;
  }
  // Prefer python3.10 (Abaqus 2024/2025), then any python3.x.
  dirs.sort((a, b) => (b === "python3.10" ? 1 : 0) - (a === "python3.10" ? 1 : 0));
  for (const d of dirs) {
    const exe = path.join(smapy, d, "python.exe");
    if (!exists(exe)) continue;
    let version = null;
    const res = spawnSync(exe, ["-c", "import sys;print(sys.version.split()[0])"], {
      encoding: "utf8",
      timeout: 10000,
    });
    if (res.status === 0 && res.stdout.trim()) version = res.stdout.trim().split(/\r?\n/)[0];
    return { path: exe, version, dir: d };
  }
  return null;
}

function parseVersion(text) {
  const m =
    /Official Version:\s*Abaqus\s*(\d{4})/i.exec(text || "") ||
    /\bAbaqus\s+(\d{4})\b/i.exec(text || "");
  return m ? Number(m[1]) : null;
}

function runVersions(command) {
  if (!command) return { text: "", status: null };
  const res = spawnSync("cmd", ["/c", command, "information=versions"], {
    encoding: "utf8",
    timeout: 30000,
    windowsHide: true,
  });
  return { text: `${res.stdout || ""}\n${res.stderr || ""}`, status: res.status };
}

export function detectAbaqus(opts = {}) {
  const cfg = readConfig();
  const candidates = [];
  if (process.env.ABAQUS_CMD) candidates.push(process.env.ABAQUS_CMD);
  if (cfg.abaqusCmd) candidates.push(cfg.abaqusCmd);
  for (const drive of ["C:", "D:", "E:"]) {
    candidates.push(path.join(drive + "\\", "SIMULIA", "Commands", "abaqus.bat"));
  }
  const fromWhere = whereAbaqus();
  if (fromWhere) candidates.push(fromWhere);

  let command = candidates.find((p) => exists(p)) || null;
  if (!command && fromWhere) command = fromWhere;

  const installs = listInstallations();
  let version = null;
  let text = "";
  if (command) {
    const res = runVersions(command);
    text = res.text;
    version = parseVersion(text);
  }
  if (!version && installs.length) version = installs[0].version;

  const install = installs.find((i) => i.version === version) || installs[0] || null;
  const python = install ? embeddedPython(install.root) : null;
  const solver = install ? install.solver : null;
  const min = Number(opts.minVersion || process.env.ABAQUS_MIN_VERSION || cfg.minVersion || MIN_VERSION);
  const supported = Boolean(version && version >= min);

  return {
    found: Boolean(command || install),
    command,
    version,
    minVersion: min,
    supported,
    reason: !command && !install
      ? "Abaqus installation not found (no abaqus.bat, no %SIMULIA%\\EstProducts\\<year>)."
      : !version
        ? "Could not determine the Abaqus version from `abaqus information=versions`."
        : supported
          ? null
          : `Abaqus ${version} is older than the supported baseline ${min}.`,
    python,
    solver,
    installRoot: install ? install.root : null,
    installations: installs,
    rawOutput: (text || "").slice(0, 4000),
  };
}
